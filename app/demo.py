import hashlib
import hmac
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.cases import load_cases
from app.config import Settings
from app.db import Store
from app.devin import Session
from app.github import Candidate
from app.main import create_app
from app.models import TERMINAL
from app.orchestrator import Orchestrator
from app.validator import Verdict


class SimulatedDevin:
    def __init__(self):
        self.sessions = {}
        self.corrections = {}
        self.created = 0

    def create(self, job, case, evidence):
        self.created += 1
        session = Session(session_id=f"simulation-{job.id}", status="running", status_detail="finished",
                          tags=[f"remediation:{job.id}"])
        self.sessions[session.session_id] = session
        self.corrections[session.session_id] = 0
        return session

    def get(self, session_id):
        return self.sessions[session_id]

    def find(self, job):
        return next((session for session in self.sessions.values() if f"remediation:{job.id}" in session.tags), None)

    def correct(self, session_id, reason):
        self.corrections[session_id] += 1


class SimulatedGitHub:
    def __init__(self, devin):
        self.devin = devin

    def discover(self, job, session, case):
        attempt = self.devin.corrections[session.session_id]
        sha = hashlib.sha1(f"simulation:{job.id}:{attempt}".encode()).hexdigest()
        return Candidate(job.issue_number, None, sha, {"scenario": str(job.issue_number), "attempt": str(attempt)})

    def unchanged(self, candidate):
        return True


class SimulatedValidator:
    def evaluate(self, case, candidate):
        scenario, attempt = int(candidate.files["scenario"]), int(candidate.files["attempt"])
        recovered = scenario != 103 and (scenario != 102 or attempt == 1)
        if recovered:
            return Verdict("VERIFIED", "Simulated regression detection", {"normal": "PASS", "mutant": "DETECTED"})
        return Verdict("REPAIR", "Simulated regression escape", {"normal": "PASS", "mutant": "ESCAPED"})


def signed_event(settings: Settings, issue: int, delivery: str) -> tuple[bytes, dict]:
    body = json.dumps({"action": "labeled", "label": {"name": "devin-remediate"}, "issue": {"number": issue},
                       "repository": {"id": settings.github_repository_id, "full_name": settings.github_repository}}).encode()
    signature = hmac.new(settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery}


def seed(directory: Path) -> tuple[Settings, Store, dict]:
    settings = Settings(_env_file=None, mode="SIMULATION", run_live=False, data_dir=directory, poll_seconds=0.1,
                        github_repository="example/superset", github_repository_id=1,
                        github_webhook_secret="local-simulation-only", case_issues={})
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.database_path) + suffix).unlink(missing_ok=True)
    store, cases, devin = Store(settings.database_path), load_cases(), SimulatedDevin()
    github, validator = SimulatedGitHub(devin), SimulatedValidator()
    job_ids = []
    for issue, case_id in [(101, "histogram-invalid"), (102, "schema-no-engine"), (103, "schema-no-engine"), (104, "histogram-invalid")]:
        scenario_settings = settings.model_copy(update={"case_issues": {case_id: issue}})
        with TestClient(create_app(scenario_settings, store)) as client:
            body, headers = signed_event(scenario_settings, issue, f"simulation-delivery-{issue}")
            response = client.post("/webhooks/github", content=body, headers=headers)
            response.raise_for_status()
            job_ids.append(response.json()["job_id"])
            if issue == 101:
                duplicate = client.post("/webhooks/github", content=body, headers=headers)
                assert duplicate.json()["duplicate"]
    worker = Orchestrator(settings, store, cases, devin, github, validator)
    for job_id in job_ids:
        worker.tick(job_id)
    assert devin.created == 4
    # Recreate both the store and orchestrator with already-persisted session IDs.
    store.engine.dispose()
    store = Store(settings.database_path)
    worker = Orchestrator(settings, store, cases, devin, github, validator)
    for _ in range(8):
        for job_id in job_ids:
            worker.tick(job_id)
        if all(store.get(job_id).status in TERMINAL for job_id in job_ids):
            break
        time.sleep(settings.poll_seconds + 0.01)
    metrics = store.metrics("SIMULATION")
    assert devin.created == 4, "Restart launched a duplicate session"
    assert metrics["verified"] == 3 and metrics["failed"] == 1 and metrics["duplicates"] == 1
    return settings, store, metrics


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Exercise all scenarios and exit")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    settings, store, metrics = seed(args.data_dir)
    print(json.dumps({"mode": "SIMULATION", **metrics}, indent=2))
    if not args.check:
        uvicorn.run(create_app(settings, store), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
