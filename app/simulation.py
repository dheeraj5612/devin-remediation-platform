"""Credential-free demo: real webhook, database and orchestrator; fake Devin, GitHub and validator.

ELI5: `make demo` runs the whole pipeline in a sandbox so a reviewer can watch
every state transition without an API key or a Superset checkout. The fakes
are scripted: issue 101 succeeds first try, 102 needs the one correction,
103 fails twice and escalates, 104 survives a worker restart, and 105/106
exercise application acceptance and rejection. The demo also sends one
duplicate webhook to show deduplication.
"""

import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient

from app.cases import Case
from app.config import ROOT, Settings
from app.models import TERMINAL, Job
from app.db import Store
from app.devin import RemoteError, SessionState
from app.github import Candidate
from app.main import create_app
from app.metrics import metrics
from app.orchestrator import Orchestrator
from app.validator import Evaluation

# ELI5: these six issue numbers cover success, correction, escalation, restart, and app outcomes.
SCENARIOS = {101: "First-pass verification", 102: "Correction recovery", 103: "Escalation", 104: "Worker restart",
             105: "Application acceptance", 106: "Application rejection"}
# ELI5: keep the expected terminal status for every scripted issue next to its scenario name.
EXPECTED = {101: "VERIFIED", 102: "VERIFIED", 103: "ESCALATED", 104: "VERIFIED", 105: "VERIFIED", 106: "ESCALATED"}


def simulation_settings(data_dir: Path | None = None) -> Settings:
    """Create credential-free settings that isolate the demo from live storage and APIs."""
    # ELI5: honor DATA_DIR so the demo writes to the container's /data volume, not the read-only image.
    data_dir = data_dir or Path(os.environ.get("DATA_DIR", "data"))
    # `_env_file=None`: never pick up real credentials from `.env` in a demo.
    # ELI5: return isolated fake-provider settings with live spending explicitly disabled.
    return Settings(_env_file=None, mode="SIMULATION", data_dir=data_dir, github_repository="demo/superset",
                    github_repository_id=42, github_webhook_secret="simulation-only", poll_seconds=0.001,
                    enable_live=False, allow_local_validation=False, case_issues={}, cases_file=ROOT / "evals/cases.yaml",
                    devin_api_key="", devin_org_id="", github_token="")


# ELI5: this fake provider records sessions and corrections without making a network call.
class FakeDevin:
    """In-memory Devin: one session per job; each correction bumps a 'revision' that changes the fake PR SHA."""

    def __init__(self) -> None:
        """Start an empty in-memory provider model with no sessions or correction messages."""
        # ELI5: map fake session IDs to the jobs that own them.
        self.sessions: dict[str, dict] = {}
        # ELI5: count session creation to catch accidental duplicate launches.
        self.create_count = 0
        # ELI5: retain correction messages so the demo can prove its retry count.
        self.messages: list[tuple[str, str]] = []
        # ELI5: the real adapter and this fake share a short-lived safe event callback.
        self._operation_recorder = None
        # ELI5: the restart scenario can model a lost create reply without a second session.
        self.drop_next_create_reply_for: set[int] = set()

    @contextmanager
    def operation_trace(self, recorder) -> Iterator[None]:
        """Route deterministic, body-free operation summaries to the simulation store."""
        # ELI5: restore any outer callback after this one job operation completes.
        previous = self._operation_recorder
        self._operation_recorder = recorder
        try:
            # ELI5: provider-shaped fake actions below can emit their matching trace rows.
            yield
        finally:
            # ELI5: avoid carrying one job's recorder into the next simulated operation.
            self._operation_recorder = previous

    def _record_operation(self, operation: str, outcome: str, **details: Any) -> None:
        """Emit deterministic zero-latency facts without storing prompts or message text."""
        # ELI5: direct unit tests can use the fake without installing a recorder.
        if self._operation_recorder is None:
            return
        # ELI5: simulation latency is fixed at zero so repeated demos produce stable traces.
        self._operation_recorder(operation, outcome, {"latency_ms": 0, **details})

    def create_session(self, job: Job, case: Case) -> SessionState:
        """Create one deterministic fake session and remember its owning job."""
        # ELI5: increment the launch counter before assigning a deterministic provider ID.
        self.create_count += 1
        # ELI5: make the fake session ID stable across polling and restart reconciliation.
        session_id = f"sim-{job.id}"
        # ELI5: record issue and revision so fake PR heads can change after correction.
        self.sessions[session_id] = {"job_id": job.id, "issue": job.issue_number, "revision": 0}
        # ELI5: mirror the real launch breadth with a deterministic evidence-upload operation.
        self._record_operation("attachment_upload", "SUCCEEDED", attachment_url_present=True)
        # ELI5: issue 104 can model a lost provider reply while retaining its durable session.
        if job.issue_number in self.drop_next_create_reply_for:
            self.drop_next_create_reply_for.remove(job.issue_number)
            self._record_operation("session_create", "RETRYABLE_ERROR", session_id=session_id)
            raise RemoteError("Simulated lost create response", retryable=True, retry_after=0)
        # ELI5: record the successful session creation without adding a synthetic poll.
        self._record_operation("session_create", "SUCCEEDED", session_id=session_id)
        # ELI5: return the same finished-looking state the real adapter would parse.
        return SessionState(session_id=session_id, status="running", status_detail="finished")

    def get_session(self, session_id: str) -> SessionState:
        """Return a finished-looking state for a known fake session."""
        # ELI5: expose a valid provider-shaped state while marking the fake as finished.
        state = SessionState(session_id=session_id, status="running", status_detail="finished")
        # ELI5: each simulated poll is observable with fixed latency and the safe session ID.
        self._record_operation("session_poll", "SUCCEEDED", session_id=session_id)
        return state

    def find_session(self, job_id: str) -> SessionState | None:
        """Find a prior fake session by job ownership to model restart reconciliation."""
        # ELI5: search saved fake records by the durable job ID.
        session_id = next((sid for sid, record in self.sessions.items() if record["job_id"] == job_id), None)
        # ELI5: return the existing session or no result without creating another one.
        if session_id:
            state = SessionState(session_id=session_id, status="running", status_detail="finished")
            # ELI5: recovery records a found tagged session without copying provider records.
            self._record_operation("list_reconcile", "FOUND", session_id=session_id)
            return state
        # ELI5: a missing tagged session is a visible safe reconciliation outcome.
        self._record_operation("list_reconcile", "NOT_FOUND")
        return None

    def send_correction(self, session_id: str, message: str) -> None:
        """Record one correction and advance that fake session's revision."""
        # ELI5: remember the message so the demo can count bounded corrections.
        self.messages.append((session_id, message))
        # ELI5: incrementing revision changes the fake PR SHA after a correction.
        self.sessions[session_id]["revision"] += 1
        # ELI5: record only the correction outcome and session identity, never its message text.
        self._record_operation("correction_message", "SUCCEEDED", session_id=session_id)


# ELI5: this fake GitHub adapter derives one deterministic PR head per fake session revision.
class FakeGitHub:
    """Fake PR per issue (number = issue + 1000); its SHA is derived from the session's revision."""

    def __init__(self, devin: FakeDevin) -> None:
        """Attach fake PR discovery to the provider's in-memory sessions."""
        # ELI5: share the session records so PR discovery sees corrections immediately.
        self.devin = devin

    def discover(self, session: SessionState, _target_branch: str | None = None) -> Candidate:
        """Return the deterministic PR for one session regardless of its simulated base branch."""
        # ELI5: read the session's issue number and ask candidate() for its current head.
        record = self.devin.sessions[session.session_id]
        # ELI5: return a PR-shaped record with a stable revision-derived SHA.
        return self.candidate(record["issue"] + 1000)

    def candidate(self, number: int, _target_branch: str | None = None) -> Candidate:
        """Derive a stable candidate SHA from the issue number and correction revision."""
        # ELI5: find the fake session represented by this deterministic PR number.
        record = next(record for record in self.devin.sessions.values() if record["issue"] == number - 1000)
        # ELI5: hash issue and revision to model a new immutable commit after correction.
        sha = hashlib.sha1(f"simulation:{number}:{record['revision']}".encode(), usedforsecurity=False).hexdigest()
        # ELI5: return the fake PR number and exact head SHA to the orchestrator.
        return Candidate(number, sha)


# ELI5: this fake validator demonstrates orchestration outcomes without claiming real source evidence.
class FakeValidator:
    """Scripted verdicts for test-quality and application cases without claiming live evidence."""

    def validate(self, job: Job, case: Case, candidate: Candidate) -> Evaluation:
        """Return a deterministic application PASS or REGRESSION while exercising correction recovery."""
        # ELI5: route the third configured case through the application contract branches.
        if case.kind == "application":
            # ELI5: issue 105 models a fix, while 106 models a failed fix that earns one retry.
            if job.issue_number == 106:
                # ELI5: issue 106 keeps failing so the demo visibly escalates after one correction.
                return Evaluation("APPLICATION_FAILED", "SIMULATION: application regression survived",
                                  application="REGRESSION")
            # ELI5: issue 105 passes the application oracle on the first try.
            return Evaluation("VERIFIED", "SIMULATION: application contract passed", application="PASS")
        # ELI5: issue 103 always survives, while issue 102 survives only before correction.
        if job.issue_number == 103 or (job.issue_number == 102 and job.correction_count == 0):
            # ELI5: return a failed repair verdict that triggers exactly one correction.
            return Evaluation("REGRESSION_SURVIVED", "SIMULATION: regression escaped", "PASS", "PASS")
        # ELI5: every other test-quality scenario detects the mutant and verifies.
        return Evaluation("VERIFIED", "SIMULATION: regression detected", "PASS", "ASSERTION_FAILED")


def signed_event(settings: Settings, issue: int, delivery: str) -> tuple[bytes, dict]:
    """Build a GitHub-style `issues/labeled` webhook body and headers, correctly HMAC-signed."""
    # ELI5: construct only the signed fields the local webhook handler expects.
    payload = {"action": "labeled", "repository": {"id": settings.github_repository_id,
               "full_name": settings.github_repository}, "issue": {"number": issue},
               "label": {"name": "devin-remediate"}}
    # ELI5: serialize the payload exactly once before signing and sending it.
    body = json.dumps(payload).encode()
    # ELI5: compute the same SHA-256 HMAC a configured GitHub webhook would send.
    signature = hmac.new(settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    # ELI5: return the raw body and headers used by the TestClient request.
    return body, {"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "issues",
                  "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}


def run_demo(settings: Settings) -> dict:
    """Drive all six scenarios to completion and assert the expected outcomes."""
    # ELI5: refuse to run the demo if settings could point at live storage or APIs.
    if settings.mode != "SIMULATION":
        # ELI5: fail before creating an app or writing any data under live mode.
        raise ValueError("Demo must never write live storage")
    # ELI5: build the real FastAPI webhook and database wiring with fake adapters supplied below.
    app = create_app(settings)
    # ELI5: use the mode-specific store created by the application factory.
    store = app.state.store
    # ELI5: insist callers reset demo storage so old jobs cannot change the story.
    if store.jobs():
        # ELI5: stop instead of mixing old jobs into this deterministic run.
        raise ValueError("Simulation storage is not empty; use the demo command's --reset option")
    # ELI5: install the deterministic fake provider and validator used by this credential-free run.
    devin, validator = FakeDevin(), FakeValidator()
    # ELI5: issue 104 loses its first create reply so launch reconciliation is exercised safely.
    devin.drop_next_create_reply_for.add(104)
    # ELI5: make fake PR discovery share the fake Devin session revisions.
    github = FakeGitHub(devin)
    # ELI5: use the production orchestrator so real state transitions are exercised.
    orchestrator = Orchestrator(settings, store, devin, github, validator)
    # ELI5: drive signed webhook requests through the same FastAPI route a live event uses.
    with TestClient(app) as client:
        # ELI5: admit every scripted issue and replay issue 101 to demonstrate deduplication.
        for issue in SCENARIOS:
            # ELI5: sign each event with its deterministic delivery ID.
            body, headers = signed_event(settings, issue, f"demo-{issue}")
            # ELI5: the route must accept every allow-listed demo issue.
            client.post("/webhooks/github", content=body, headers=headers).raise_for_status()
            if issue == 101:  # replay the exact same delivery: must be flagged duplicate, not enqueued twice
                # ELI5: the duplicate response proves the delivery key prevented a second job.
                assert client.post("/webhooks/github", content=body, headers=headers).json()["duplicate"]
        # ELI5: remember whether the restart scenario already replaced its worker objects.
        restarted = False
        # ELI5: bounded polling prevents a broken fake state machine from looping forever.
        for _ in range(30):
            # ELI5: advance each persisted job exactly one state-machine step per pass.
            for job in store.jobs():
                # ELI5: use the current job ID so the orchestrator reloads durable state itself.
                orchestrator.step(job.id)
                # ELI5: make the simulated lost-reply retry immediately eligible while keeping live backoff intact.
                current = store.get(job.id)
                if (current.issue_number == 104 and current.status == "DEVIN_RUNNING"
                        and current.launch_requested and not current.devin_session_id):
                    store.defer(job.id, 0)
                # ELI5: trigger the one restart after issue 104 has a durable session identity.
                if job.issue_number == 104 and store.get(job.id).devin_session_id and not restarted:
                    # Simulate a worker crash after the session exists: new Store + Orchestrator, same DB.
                    # ELI5: close the old database engine as a worker crash would.
                    store.engine.dispose()
                    # ELI5: open a fresh store over the same durable SQLite file.
                    store = Store(settings)
                    # ELI5: create a new orchestrator while retaining provider session identity.
                    orchestrator = Orchestrator(settings, store, devin, github, validator)
                    # ELI5: record the worker-resume event before continuing polls.
                    orchestrator.resume()
                    # ELI5: ensure this restart branch is exercised only once.
                    restarted = True
            # ELI5: stop once every scripted job reaches a terminal status.
            if all(job.status in TERMINAL for job in store.jobs()):
                # ELI5: leave the bounded loop once every job is done.
                break
        else:
            # ELI5: failure to settle inside the bound means the simulation is invalid.
            raise RuntimeError("Simulation failed to settle")
        # ELI5: collect final statuses by the issue number used in the demo report.
        actual = {job.issue_number: job.status for job in store.jobs()}
        # 6 sessions (one per job, none recreated after the restart) and 3 corrections (102, 103, and 106).
        if actual != EXPECTED or devin.create_count != 6 or len(devin.messages) != 3 or not restarted:
            # ELI5: fail loudly if deduplication, retries, or application scenarios drift.
            raise RuntimeError(f"Unexpected simulation outcome: {actual}")
    # ELI5: return report metrics after the TestClient has finished all webhook activity.
    return {"mode": settings.mode, "scenarios": actual, "sessions_created": devin.create_count, **metrics(store)}
