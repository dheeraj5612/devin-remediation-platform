"""Issue status comment: renderer content and the orchestrator's create/edit/best-effort behavior."""

import json

from app.cases import Registry, harness_fingerprint
from app.db import Store
from app.issue_status import marker, render_card
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


def make_job(store, mode="SIMULATION", case_id="histogram-invalid-column", issue=101):
    """Enqueue a bare job and return the durable row, so events can be attached with a valid FK."""
    job, _ = store.enqueue(f"delivery-{issue}", "demo/superset", issue, case_id)
    return job


def api_event(store, job_id, operation, outcome="SUCCEEDED", mode="SIMULATION"):
    """Append one redacted DEVIN_API_OPERATION event, matching the adapter's persisted shape."""
    store.change(job_id, "DEVIN_API_OPERATION",
                 details={"operation": operation, "operation_key": operation, "outcome": outcome,
                          "mode": mode, "latency_ms": 5})


def test_verified_job_renders_expected_card_content(settings, store):
    """A verified test-quality job shows the plain-English state, story bullets, and job id."""
    job = make_job(store)
    store.change(job.id, "DEVIN_RUNNING", status="DEVIN_RUNNING")
    store.change(job.id, "SESSION_ATTACHED", devin_session_url="https://app.devin.ai/sessions/abc")
    store.change(job.id, "PR_OPENED", status="PR_OPENED",
                 candidate_pr_url="https://github.com/demo/superset/pull/7", candidate_sha="a" * 40)
    store.change(job.id, "VALIDATING", status="VALIDATING")
    store.change(job.id, "VERIFIED", status="VERIFIED", validated_sha="a" * 40,
                 validation_status="VERIFIED",
                 validation={"outcome": "VERIFIED", "normal": "PASS", "mutant": "ASSERTION_FAILED", "evidence": True})
    job = store.get(job.id)
    registry = Registry(settings)
    card = render_card(job, store, registry)
    assert card.startswith(f"<!-- devintrace-status job={job.id} -->")
    assert "### DevinTrace status" in card
    assert "Verified" in card
    assert "https://app.devin.ai/sessions/abc" in card
    assert f"@ `{'a' * 12}`" in card
    assert "test repair" in card
    assert "Injected regression caught: yes" in card
    assert "Oracle ran on the PR's exact commit" in card
    assert "Candidate SHA matches validated SHA" in card
    assert "job-id" not in card  # sanity: no stray placeholder text
    assert job.id in card
    assert "Human review is the final gate" in card
    assert "localhost" not in card


def test_failed_application_job_renders_contract_result_and_corrections(settings, store):
    """A failed application-contract job shows the baseline/candidate story, not test verdict wording."""
    job = make_job(store, case_id="import-unparseable-yaml", issue=201)
    store.change(job.id, "DEVIN_RUNNING", status="DEVIN_RUNNING")
    store.change(job.id, "CORRECTING", status="FAILED", correction_count=1,
                 candidate_pr_url="https://github.com/demo/superset/pull/9", candidate_sha="b" * 40,
                 validation_status="APPLICATION_FAILED",
                 validation={"outcome": "APPLICATION_FAILED", "application": "REGRESSION", "evidence": True})
    job = store.get(job.id)
    registry = Registry(settings)
    case = registry.cases["import-unparseable-yaml"]
    directory = settings.storage / "baselines"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{case.id}.json").write_text(json.dumps({
        "case_id": case.id, "mode": "LIVE", "sha": case.baseline_sha, "outcome": "CONFIRMED",
        "case_fingerprint": case.fingerprint, "harness_fingerprint": harness_fingerprint(),
        "application": "REGRESSION", "provenance": True,
    }))
    card = render_card(job, store, registry)
    assert "Needs human attention" in card
    assert "application contract" in card
    assert "Baseline `REGRESSION` -> candidate `REGRESSION`" in card
    assert "Corrections sent:** 1" in card
    assert "Injected regression caught" not in card


def test_case_line_omits_allowed_files_without_a_registry(settings, store):
    """No registry means the case line names only the id, never invented scope."""
    job = make_job(store)
    card = render_card(job, store, registry=None)
    assert f"**Case:** `{job.case_id}`" in card
    assert "allowed files" not in card


def test_in_progress_job_omits_sections_with_no_persisted_data(settings, store):
    """A freshly queued job has no session, PR, api usage, or duration; those lines are simply absent."""
    job = make_job(store)
    registry = Registry(settings)
    card = render_card(job, store, registry)
    assert "Devin session" not in card
    assert "Pull request" not in card
    assert "Label to verified" not in card
    assert "Independent proof" not in card
    assert "Attachments (0 uploads): baseline proof" in card
    assert "Sessions (0 created, 0 polls, corrections 0 of 1)" in card
    assert "Corrections sent:** 0" in card


def test_how_it_ran_counts_devin_api_operations(settings, store):
    """Attachment, session, poll, and recovery counts come only from persisted operation events."""
    job = make_job(store)
    api_event(store, job.id, "attachment_upload")
    api_event(store, job.id, "session_create")
    api_event(store, job.id, "session_poll")
    api_event(store, job.id, "session_poll")
    api_event(store, job.id, "list_reconcile", outcome="FOUND")
    job = store.get(job.id)
    card = render_card(job, store, Registry(settings))
    assert "Attachments (1 upload): baseline proof" in card
    assert "Sessions (1 created, 2 polls, corrections 0 of 1), list-by-tag recovery used" in card


def test_label_to_verified_present_only_when_both_milestones_exist(settings, store):
    """The duration line appears only once QUEUED and VERIFIED are both on the timeline."""
    job = make_job(store)
    card = render_card(job, store, Registry(settings))
    assert "Label to verified" not in card
    store.change(job.id, "VERIFIED")  # Record the milestone event without changing job.status.
    job = store.get(job.id)
    card = render_card(job, store, Registry(settings))
    assert "Label to verified:**" in card


def test_marker_helper_matches_rendered_first_line(settings, store):
    """The exported marker helper produces exactly the hidden first line of the card."""
    job = make_job(store)
    card = render_card(job, store, Registry(settings))
    assert card.splitlines()[0] == marker(job.id)


def test_pr_status_header_can_be_overridden(settings, store):
    """The PR-facing card uses the DevinTrace verification header instead of the issue header."""
    job = make_job(store)
    card = render_card(job, store, Registry(settings), header="### DevinTrace verification")
    assert "### DevinTrace verification" in card
    assert "### DevinTrace status" not in card


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
