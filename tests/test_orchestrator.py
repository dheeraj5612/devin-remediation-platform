"""State machine end to end with fakes: launch-once guard, retries, correction bound, stale SHA, timeouts, restart."""

from datetime import timedelta

import pytest

from app.db import Store
from app.devin import RemoteError, SessionState
from app.github import Candidate
from app.models import now
from app.orchestrator import Orchestrator
from app.validator import Evaluation


def queue(rig, issue=101):
    case = rig.registry.by_issue[issue]
    return rig.store.enqueue(f"delivery-{issue}", rig.settings.github_repository, issue, case.id)[0]


def advance(rig, job, target=None):
    for _ in range(20):
        current = rig.store.get(job.id)
        if current.status == target or current.status in {"VERIFIED", "FAILED", "ESCALATED"}:
            return current
        rig.step(job.id)
    raise AssertionError("Orchestration did not settle")


@pytest.mark.parametrize("issue,status,corrections", [(101, "VERIFIED", 0), (102, "VERIFIED", 1), (103, "ESCALATED", 1)])
def test_bounded_outcomes(rig, issue, status, corrections):
    job = advance(rig, queue(rig, issue))
    assert job.status == status
    assert job.correction_count == corrections
    assert len(rig.devin.messages) == corrections
    assert rig.devin.create_count == 1
    if status == "VERIFIED":
        assert job.validated_sha == job.candidate_sha
        assert job.validation["normal"] == "PASS"
        assert job.validation["mutant"] == "ASSERTION_FAILED"


def test_restart_resumes_saved_session_without_relaunch(rig):
    job = queue(rig)
    rig.step(job.id)
    rig.step(job.id)
    session_id = rig.store.get(job.id).devin_session_id
    rig.store.defer(job.id, 0)
    rig.store.engine.dispose()
    store = Store(rig.settings)
    restarted = Orchestrator(rig.settings, store, rig.devin, rig.github, rig.validator)
    restarted.resume()
    assert advance(restarted, job).status == "VERIFIED"
    assert store.get(job.id).devin_session_id == session_id
    assert rig.devin.create_count == 1
    assert "WORKER_RESUMED" in [event.event_type for event in store.events()]
    store.engine.dispose()


def test_lost_launch_response_reconciles_by_tag(rig):
    original = rig.devin.create_session
    def lost_response(job, case):
        original(job, case)
        raise RemoteError("Network failure or timeout", retryable=True)
    rig.devin.create_session = lost_response
    job = advance(rig, queue(rig))
    assert job.status == "VERIFIED"
    assert rig.devin.create_count == 1
    assert "LAUNCH_UNCERTAIN" in [event.event_type for event in rig.store.events()]


def test_crash_before_post_does_not_risk_duplicate_spend(rig):
    job = queue(rig)
    rig.store.change(job.id, "INTENT", status="DEVIN_RUNNING", started_at=now(), launch_requested=True)
    assert advance(rig, job).status == "ESCALATED"
    assert rig.devin.create_count == 0


def test_reconciliation_network_errors_are_bounded(rig):
    job = queue(rig)
    rig.store.change(job.id, "INTENT", status="DEVIN_RUNNING", started_at=now(), launch_requested=True)
    def unavailable(job_id):
        raise RemoteError("Network failure or timeout", retryable=True)
    rig.devin.find_session = unavailable
    assert advance(rig, job).status == "FAILED"
    assert rig.store.get(job.id).api_failures == 4
    assert rig.devin.create_count == 0


def test_ambiguous_correction_ack_is_not_resent(rig):
    job = queue(rig, 102)
    original = rig.devin.send_correction
    def lost_response(session, message):
        original(session, message)
        raise RemoteError("Network failure or timeout", retryable=True)
    rig.devin.send_correction = lost_response
    assert advance(rig, job).status == "ESCALATED"
    assert len(rig.devin.messages) == 1
    assert rig.devin.create_count == 1


@pytest.mark.parametrize("outcome,status", [("INFRA_ERROR", "FAILED"), ("SCOPE_REJECTED", "ESCALATED")])
def test_non_repair_failures_are_not_sent_to_devin(rig, outcome, status):
    rig.validator.validate = lambda *args: Evaluation(outcome, "evaluation unavailable")
    assert advance(rig, queue(rig)).status == status
    assert not rig.devin.messages


def test_finished_session_without_pr_is_not_success(rig):
    rig.github.discover = lambda state: None
    job = advance(rig, queue(rig))
    assert job.status == "ESCALATED"
    assert not job.validated_sha


def test_agent_error_without_pr(rig):
    rig.github.discover = lambda state: None
    rig.devin.get_session = lambda session: SessionState(session_id=session, status="error")
    assert advance(rig, queue(rig)).status == "ESCALATED"


def test_sha_change_after_evaluation_cannot_be_verified(rig):
    job = queue(rig)
    advance(rig, job, "VALIDATING")
    original = rig.github.candidate
    calls = []
    def candidate(number):
        calls.append(number)
        previous = original(number)
        return previous if len(calls) == 1 else Candidate(number, "f" * 40)
    rig.github.candidate = candidate
    rig.step(job.id)
    current = rig.store.get(job.id)
    assert current.status == "PR_OPENED"
    assert current.validation_status == "STALE_SHA"
    assert not current.validated_sha
    assert current.candidate_sha == "f" * 40


def test_sha_change_before_execution_skips_validator(rig):
    job = queue(rig)
    advance(rig, job, "VALIDATING")
    rig.github.candidate = lambda number: Candidate(number, "f" * 40)
    def must_not_run(*args):
        raise AssertionError("Stale candidate executed")
    rig.validator.validate = must_not_run
    rig.step(job.id)
    assert rig.store.get(job.id).status == "PR_OPENED"


def test_deadline_and_terminal_job_are_bounded(rig):
    job = queue(rig)
    rig.store.change(job.id, "STARTED", status="DEVIN_RUNNING", started_at=now() - timedelta(hours=5))
    rig.step(job.id)
    assert rig.store.get(job.id).status == "ESCALATED"
    count = len(rig.store.events())
    rig.step(job.id)
    assert len(rig.store.events()) == count
    assert rig.devin.create_count == 0
