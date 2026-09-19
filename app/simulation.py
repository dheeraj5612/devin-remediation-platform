import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.cases import Case
from app.config import ROOT, Settings
from app.models import Job
from app.db import Store
from app.devin import SessionState
from app.github import Candidate
from app.main import create_app
from app.metrics import metrics
from app.orchestrator import Orchestrator
from app.validator import Evaluation

SCENARIOS = {101: "First-pass verification", 102: "Correction recovery", 103: "Escalation", 104: "Worker restart"}


def simulation_settings(data_dir: Path = Path("data")) -> Settings:
    return Settings(_env_file=None, mode="SIMULATION", data_dir=data_dir, github_repository="demo/superset",
                    github_repository_id=42, github_webhook_secret="simulation-only", poll_seconds=0.001,
                    enable_live=False, allow_local_validation=False, case_issues={}, cases_file=ROOT / "evals/cases.yaml",
                    devin_api_key="", devin_org_id="", github_token="")


class FakeDevin:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.create_count = 0
        self.messages: list[tuple[str, str]] = []

    def create_session(self, job: Job, case: Case) -> SessionState:
        self.create_count += 1
        session_id = f"sim-{job.id}"
        self.sessions[session_id] = {"job_id": job.id, "issue": job.issue_number, "revision": 0}
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionState:
        return SessionState(session_id=session_id, status="running", status_detail="finished")

    def find_session(self, job_id: str) -> SessionState | None:
        matches = [sid for sid, record in self.sessions.items() if record["job_id"] == job_id]
        return self.get_session(matches[0]) if matches else None

    def send_correction(self, session_id: str, message: str) -> None:
        self.messages.append((session_id, message))
        self.sessions[session_id]["revision"] += 1


class FakeGitHub:
    def __init__(self, devin: FakeDevin) -> None:
        self.devin = devin

    def discover(self, session: SessionState) -> Candidate:
        record = self.devin.sessions[session.session_id]
        return self.candidate(record["issue"] + 1000)

    def candidate(self, number: int) -> Candidate:
        record = next(record for record in self.devin.sessions.values() if record["issue"] == number - 1000)
        sha = hashlib.sha1(f"simulation:{number}:{record['revision']}".encode(), usedforsecurity=False).hexdigest()
        return Candidate(number, sha)


class FakeValidator:
    def validate(self, job: Job, case: Case, candidate: Candidate) -> Evaluation:
        if job.issue_number == 103 or (job.issue_number == 102 and job.correction_count == 0):
            return Evaluation("REGRESSION_SURVIVED", "SIMULATION: regression escaped", "PASS", "PASS")
        return Evaluation("VERIFIED", "SIMULATION: regression detected", "PASS", "ASSERTION_FAILED")


def signed_event(settings: Settings, issue: int, delivery: str) -> tuple[bytes, dict]:
    payload = {"action": "labeled", "repository": {"id": settings.github_repository_id,
               "full_name": settings.github_repository}, "issue": {"number": issue},
               "label": {"name": "devin-remediate"}}
    body = json.dumps(payload).encode()
    signature = hmac.new(settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "issues",
                  "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}


def run_demo(settings: Settings) -> dict:
    if settings.mode != "SIMULATION":
        raise ValueError("Demo must never write live storage")
    app = create_app(settings)
    store = app.state.store
    if store.jobs():
        raise ValueError("Simulation storage is not empty; use the demo command's --reset option")
    devin, validator = FakeDevin(), FakeValidator()
    github = FakeGitHub(devin)
    orchestrator = Orchestrator(settings, store, devin, github, validator)
    with TestClient(app) as client:
        for issue in SCENARIOS:
            body, headers = signed_event(settings, issue, f"demo-{issue}")
            response = client.post("/webhooks/github", content=body, headers=headers)
            response.raise_for_status()
            if issue == 101:
                assert client.post("/webhooks/github", content=body, headers=headers).json()["duplicate"]
        restarted = False
        for _ in range(30):
            for job in store.jobs():
                orchestrator.step(job.id)
                if job.issue_number == 104 and store.get(job.id).devin_session_id and not restarted:
                    store.engine.dispose()
                    store = Store(settings)
                    orchestrator = Orchestrator(settings, store, devin, github, validator)
                    orchestrator.resume()
                    restarted = True
            if all(job.status in {"VERIFIED", "ESCALATED", "FAILED"} for job in store.jobs()):
                break
        else:
            raise RuntimeError("Simulation failed to settle")
        actual = {job.issue_number: job.status for job in store.jobs()}
        expected = {101: "VERIFIED", 102: "VERIFIED", 103: "ESCALATED", 104: "VERIFIED"}
        if actual != expected or devin.create_count != 4 or len(devin.messages) != 2:
            raise RuntimeError(f"Unexpected simulation outcome: {actual}")
    return {"mode": settings.mode, "scenarios": actual, "sessions_created": devin.create_count, **metrics(store)}
