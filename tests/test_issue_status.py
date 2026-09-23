"""Issue status comment: renderer content and the orchestrator's create/edit/best-effort behavior."""

from app.db import Store
from app.issue_status import render_card
from app.models import Job
from app.orchestrator import Orchestrator
from app.simulation import FakeDevin, FakeGitHub, FakeValidator
from tests.test_orchestrator import advance, queue


def live_rig(live, upsert):
    """Wire a live-shaped Orchestrator with fake Devin/GitHub, capturing every comment upsert call."""
    store = Store(live)
    devin = FakeDevin()
    github = FakeGitHub(devin)
    github.upsert_issue_comment = upsert
    return Orchestrator(live, store, devin, github, FakeValidator())


def test_verified_job_renders_expected_card_content():
    """A verified test-quality job shows the plain-English state and normal/mutant verdict."""
    job = Job(id="job-1", repository="demo/superset", issue_number=101, status="VERIFIED",
               devin_session_url="https://app.devin.ai/sessions/abc", candidate_pr_url="https://github.com/demo/superset/pull/7",
               candidate_sha="a" * 40, validated_sha="a" * 40, validation_status="VERIFIED",
               validation={"outcome": "VERIFIED", "normal": "PASS", "mutant": "ASSERTION_FAILED"}, correction_count=0)
    card = render_card(job)
    assert "DevinTrace status" in card
    assert "Verified" in card
    assert "https://app.devin.ai/sessions/abc" in card
    assert f"@ `{'a' * 12}`" in card
    assert "original behavior PASS, injected regression caught: ASSERTION_FAILED" in card
    assert "job-1" in card
    assert "Human review is the final gate" in card
    assert "localhost" not in card


def test_failed_application_job_renders_needs_human_attention_and_contract_result():
    """A failed application-contract job shows the application outcome, not normal/mutant labels."""
    job = Job(id="job-2", repository="demo/superset", issue_number=105, status="FAILED",
               candidate_pr_url="https://github.com/demo/superset/pull/9", candidate_sha="b" * 40,
               validation_status="INFRA_ERROR", validation={"outcome": "INFRA_ERROR", "application": "INFRA_ERROR"},
               correction_count=1)
    card = render_card(job)
    assert "Needs human attention" in card
    assert "application contract `INFRA_ERROR`" in card
    assert "Corrections sent:** 1" in card


def test_comment_is_created_once_then_edited_in_place(live):
    """Each visible change reuses the same saved comment id after the first POST."""
    calls = []

    def upsert(issue_number, body, comment_id):
        calls.append((issue_number, body, comment_id))
        return 555 if comment_id is None else comment_id

    rig = live_rig(live, upsert)
    job = advance(rig, queue(rig, 101))
    assert job.status == "VERIFIED"
    assert len(calls) >= 2  # At least the queued transition and the final verdict were commented.
    assert calls[0][2] is None  # First call has no saved comment id yet: it must POST.
    assert all(comment_id == 555 for _, _, comment_id in calls[1:])  # Every later call edits the same comment.
    events = [event.event_type for event in rig.store.events(job.id)]
    assert events.count("ISSUE_STATUS_COMMENTED") == len(calls)
    assert "ISSUE_COMMENT_FAILED" not in events


def test_comment_failure_never_changes_job_status_or_api_failures(live):
    """A broken GitHub comment call is recorded but never breaks or retries the step."""
    def boom(issue_number, body, comment_id):
        raise RuntimeError("simulated GitHub outage")

    rig = live_rig(live, boom)
    job = queue(rig, 101)
    rig.step(job.id)
    current = rig.store.get(job.id)
    assert current.status == "DEVIN_RUNNING"  # The normal transition still happened.
    assert current.api_failures == 0  # A comment failure must never spend the provider retry budget.
    events = [event.event_type for event in rig.store.events(job.id)]
    assert "ISSUE_COMMENT_FAILED" in events
    assert "ISSUE_STATUS_COMMENTED" not in events


def test_simulation_mode_never_comments(rig):
    """The credential-free demo never attempts an issue comment, even without a fake method."""
    job = advance(rig, queue(rig, 101))
    assert job.status == "VERIFIED"
    events = [event.event_type for event in rig.store.events(job.id)]
    assert "ISSUE_STATUS_COMMENTED" not in events
    assert "ISSUE_COMMENT_FAILED" not in events
