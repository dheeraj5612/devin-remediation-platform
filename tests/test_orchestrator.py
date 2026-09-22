"""End-to-end state-machine tests using fake Devin, GitHub, and validator clients.

ELI5: each test advances one durable job through the same states the worker uses,
then checks that retries, restarts, and changing pull requests stay bounded.
"""

from datetime import timedelta  # Create an old job for the deadline test.

import pytest  # Parameterize representative state-machine outcomes.

from app.db import Store  # Reopen durable state during restart recovery.
from app.devin import RemoteError, SessionState  # Model provider failures and terminal sessions.
from app.github import Candidate  # Build changed pull-request heads for stale-SHA tests.
from app.models import now  # Timestamp synthetic state transitions.
from app.orchestrator import Orchestrator  # Exercise the production state machine.
from app.validator import Evaluation  # Return controlled validation results.


def queue(rig, issue=101):
    """Queue the approved case bound to an issue and return its durable job."""

    case = rig.registry.by_issue[issue]  # Resolve the issue to the trusted case contract.
    return rig.store.enqueue(f"delivery-{issue}", rig.settings.github_repository, issue, case.id)[0]  # Return the admitted job row.


def advance(rig, job, target=None):
    """Step a job until it reaches the requested or terminal state."""

    for _ in range(20):  # Bound the fake worker loop so a regression cannot hang the test suite.
        current = rig.store.get(job.id)  # Read the newest durable state before each step.
        if current.status == target or current.status in {"VERIFIED", "FAILED", "ESCALATED"}:
            return current  # Stop when the requested or terminal state is visible.
        rig.step(job.id)  # Advance the production orchestrator once.
    raise AssertionError("Orchestration did not settle")  # Fail rather than spin forever.


@pytest.mark.parametrize("issue,status,corrections", [(101, "VERIFIED", 0), (102, "VERIFIED", 1), (103, "ESCALATED", 1)])
def test_bounded_outcomes(rig, issue, status, corrections):
    """Reach each scripted terminal outcome with at most the expected correction count."""

    job = advance(rig, queue(rig, issue))  # Run the issue through the fake provider workflow.
    assert job.status == status  # Match the expected verified or escalated terminal state.
    assert job.correction_count == corrections  # Enforce the correction bound.
    assert len(rig.devin.messages) == corrections  # Each correction corresponds to one provider message.
    assert rig.devin.create_count == 1  # One issue may create only one provider session.
    if status == "VERIFIED":
        assert job.validated_sha == job.candidate_sha  # Verification must bind to the observed candidate head.
        assert job.validation["normal"] == "PASS"  # Correct behavior must pass independently.
        assert job.validation["mutant"] == "ASSERTION_FAILED"  # The controlled regression must be detected.


def test_restart_resumes_saved_session_without_relaunch(rig):
    """Resume a saved provider session after a worker restart without relaunching it."""

    job = queue(rig)  # Create one queued job.
    rig.step(job.id)  # Launch the fake provider session.
    rig.step(job.id)  # Observe its pull request.
    session_id = rig.store.get(job.id).devin_session_id  # Remember the original provider session.
    rig.store.defer(job.id, 0)  # Make the job immediately eligible after restart.
    rig.store.engine.dispose()  # Close the original worker's connections.
    store = Store(rig.settings)  # Reopen the same durable database.
    restarted = Orchestrator(rig.settings, store, rig.devin, rig.github, rig.validator)  # Build a fresh orchestrator.
    restarted.resume()  # Record recovery without creating a new provider session.
    assert advance(restarted, job).status == "VERIFIED"  # Finish the resumed validation path.
    assert store.get(job.id).devin_session_id == session_id  # Preserve the original provider identity.
    assert rig.devin.create_count == 1  # Restart recovery must not spend a second launch.
    assert "WORKER_RESUMED" in [event.event_type for event in store.events()]  # Leave an audit event for operators.
    store.engine.dispose()  # Release the restarted worker's connections.


def test_lost_launch_response_reconciles_by_tag(rig):
    """Reconcile a lost create response by finding the provider session tag."""

    original = rig.devin.create_session  # Preserve the fake provider's successful creation behavior.

    def lost_response(job, case):
        """Create the session but make the response look lost to the orchestrator."""

        original(job, case)  # The provider did create exactly one session.
        raise RemoteError("Network failure or timeout", retryable=True)  # Simulate a response lost in transit.

    rig.devin.create_session = lost_response  # Replace only the launch boundary.
    job = advance(rig, queue(rig))  # Let reconciliation discover the already-created session.
    assert job.status == "VERIFIED"  # The recovered session can still validate successfully.
    assert rig.devin.create_count == 1  # Reconciliation did not create a duplicate session.
    assert "LAUNCH_UNCERTAIN" in [event.event_type for event in rig.store.events()]  # Record the uncertainty.


def test_crash_before_post_does_not_risk_duplicate_spend(rig):
    """Escalate an intent left before the provider request instead of relaunching it."""

    job = queue(rig)  # Admit the job before recording launch intent.
    rig.store.change(job.id, "INTENT", status="DEVIN_RUNNING", started_at=now(), launch_requested=True)  # Simulate a crash after intent persistence.
    assert advance(rig, job).status == "ESCALATED"  # Ambiguous intent requires human review.
    assert rig.devin.create_count == 0  # No provider request is repeated.


def test_reconciliation_network_errors_are_bounded(rig):
    """Convert repeated reconciliation network errors into one bounded failure."""

    job = queue(rig)  # Admit a job whose launch intent is already durable.
    rig.store.change(job.id, "INTENT", status="DEVIN_RUNNING", started_at=now(), launch_requested=True)  # Skip directly to reconciliation.

    def unavailable(job_id):
        """Make every reconciliation lookup fail with a retryable provider error."""

        raise RemoteError("Network failure or timeout", retryable=True)  # Return the same bounded failure each time.

    rig.devin.find_session = unavailable  # Replace only the reconciliation lookup.
    assert advance(rig, job).status == "FAILED"  # The retry budget eventually closes the job.
    assert rig.store.get(job.id).api_failures == 4  # Four failures are recorded before terminal failure.
    assert rig.devin.create_count == 0  # Lookup retries never relaunch the session.


def test_ambiguous_correction_ack_is_not_resent(rig):
    """Escalate after an ambiguous correction response without sending it twice."""

    job = queue(rig, 102)  # Use the scripted case that needs one correction.
    original = rig.devin.send_correction  # Preserve the fake provider's send behavior.

    def lost_response(session, message):
        """Accept the correction remotely but hide its response from the caller."""

        original(session, message)  # The provider received the correction once.
        raise RemoteError("Network failure or timeout", retryable=True)  # Make acknowledgement ambiguous.

    rig.devin.send_correction = lost_response  # Replace the correction boundary.
    assert advance(rig, job).status == "ESCALATED"  # Ambiguous correction acknowledgement requires review.
    assert len(rig.devin.messages) == 1  # The correction is never resent.
    assert rig.devin.create_count == 1  # The original session remains the only session.


@pytest.mark.parametrize("outcome,status", [("INFRA_ERROR", "FAILED"), ("SCOPE_REJECTED", "ESCALATED")])
def test_non_repair_failures_are_not_sent_to_devin(rig, outcome, status):
    """Keep infrastructure or scope failures out of provider correction messages."""

    rig.validator.validate = lambda *args: Evaluation(outcome, "evaluation unavailable")  # Force a nonrepair verdict.
    assert advance(rig, queue(rig)).status == status  # Map it to the expected failure or escalation.
    assert not rig.devin.messages  # No correction is sent for a nonrepair outcome.


def test_finished_session_without_pr_is_not_success(rig):
    """Escalate a finished provider session that never produced a candidate PR."""

    rig.github.discover = lambda state: None  # Hide candidate discovery from the fake GitHub client.
    job = advance(rig, queue(rig))  # Run the session to its terminal provider state.
    assert job.status == "ESCALATED"  # A missing PR needs human investigation.
    assert not job.validated_sha  # No candidate can be verified without a PR head.


def test_agent_error_without_pr(rig):
    """Escalate a provider error when no candidate PR exists to validate."""

    rig.github.discover = lambda state: None  # Keep candidate discovery empty.
    rig.devin.get_session = lambda session: SessionState(session_id=session, status="error")  # Return an agent error state.
    assert advance(rig, queue(rig)).status == "ESCALATED"  # A provider error without a PR is not verified.


def test_sha_change_after_evaluation_cannot_be_verified(rig):
    """Reject verification when the pull-request head changes after evaluation."""

    job = queue(rig)  # Create the job under test.
    advance(rig, job, "VALIDATING")  # Drive it to the validation state.
    original = rig.github.candidate  # Preserve the original candidate lookup.
    calls = []  # Count candidate lookups so the second one can change the SHA.

    def candidate(number):
        """Return the original head once, then expose a changed head."""

        calls.append(number)  # Record each candidate lookup.
        previous = original(number)  # Ask the fake GitHub client for its normal candidate.
        return previous if len(calls) == 1 else Candidate(number, "f" * 40)  # Change the head on the second read.

    rig.github.candidate = candidate  # Replace candidate lookup with the changing-head fixture.
    rig.step(job.id)  # Let the orchestrator detect the stale result.
    current = rig.store.get(job.id)  # Read the persisted state after the check.
    assert current.status == "PR_OPENED"  # The job returns to PR discovery for a fresh head.
    assert current.validation_status == "STALE_SHA"  # The stale evaluation is recorded explicitly.
    assert current.validation is None  # Old validation results cannot describe the new candidate head.
    assert not current.validated_sha  # No stale SHA can be marked verified.
    assert current.candidate_sha == "f" * 40  # The new candidate head is now the active one.


def test_sha_change_before_execution_skips_validator(rig):
    """Skip validator execution when the candidate head changes before the run starts."""

    job = queue(rig)  # Create the job under test.
    advance(rig, job, "VALIDATING")  # Drive it to the validation boundary.
    rig.github.candidate = lambda number: Candidate(number, "f" * 40)  # Return a different head immediately.

    def must_not_run(*args):
        """Fail if stale candidate code reaches the validator."""

        raise AssertionError("Stale candidate executed")  # The orchestrator should check SHA first.

    rig.validator.validate = must_not_run  # Replace the validator with a sentinel.
    rig.step(job.id)  # Process the changed head.
    assert rig.store.get(job.id).status == "PR_OPENED"  # Return to PR discovery without executing it.


def test_deadline_and_terminal_job_are_bounded(rig):
    """Escalate an overdue job and make later terminal steps a no-op."""

    job = queue(rig)  # Create the job under test.
    rig.store.change(job.id, "STARTED", status="DEVIN_RUNNING", started_at=now() - timedelta(hours=5))  # Set a time beyond the configured deadline.
    rig.step(job.id)  # Process the overdue job.
    assert rig.store.get(job.id).status == "ESCALATED"  # Deadline handling requires human review.
    count = len(rig.store.events())  # Record the terminal timeline length.
    rig.step(job.id)  # A terminal job should not be processed again.
    assert len(rig.store.events()) == count  # No duplicate events are appended.
    assert rig.devin.create_count == 0  # Deadline handling never launches a provider session.
