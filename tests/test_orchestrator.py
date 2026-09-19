import pytest

from app.db import Store
from app.devin import ProviderError
from app.orchestrator import Orchestrator
from app.validator import Verdict
from conftest import enqueue, tick


def test_first_pass_and_restart(harness, store, settings):
    job_id = enqueue(store)
    tick(harness, job_id)
    assert store.get(job_id).status == "DEVIN_RUNNING"
    new_store = Store(settings.database_path)
    restarted = Orchestrator(settings, new_store, harness.cases, harness.devin, harness.github, harness.validator)
    tick(restarted, job_id)
    assert new_store.get(job_id).status == "VERIFIED"
    assert harness.devin.created == 1
    assert new_store.get(job_id).candidate_sha
    new_store.engine.dispose()


@pytest.mark.parametrize("issue,expected", [(102, "VERIFIED"), (103, "ESCALATED")])
def test_single_correction(harness, store, issue, expected):
    job_id = enqueue(store, issue, "schema-no-engine")
    for _ in range(3):
        tick(harness, job_id)
    job = store.get(job_id)
    assert job.status == expected
    assert job.correction_count == 1
    assert harness.devin.created == 1
    assert harness.devin.corrections[job.devin_session_id] == 1
    tick(harness, job_id)
    assert harness.devin.corrections[job.devin_session_id] == 1


def test_ambiguous_create_reconciles_without_new_session(harness, store, monkeypatch):
    create = harness.devin.create

    def lost_response(*args):
        create(*args)
        raise ProviderError("Timeout after provider accepted request", retryable=True)

    monkeypatch.setattr(harness.devin, "create", lost_response)
    job_id = enqueue(store)
    tick(harness, job_id)
    assert store.get(job_id).launch_intent and store.get(job_id).devin_session_id is None
    tick(harness, job_id)
    tick(harness, job_id)
    assert store.get(job_id).status == "VERIFIED"
    assert harness.devin.created == 1


def test_unknown_create_never_retries_paid_post(harness, store, monkeypatch):
    calls = []

    def timeout(*_args):
        calls.append(1)
        raise ProviderError("Unknown outcome", retryable=True)

    monkeypatch.setattr(harness.devin, "create", timeout)
    job_id = enqueue(store)
    for _ in range(5):
        tick(harness, job_id)
    assert store.get(job_id).status == "FAILED"
    assert len(calls) == 1


def test_infrastructure_is_not_sent_as_correction(harness, store, monkeypatch):
    monkeypatch.setattr(harness.validator, "evaluate", lambda *_: Verdict("INFRA", "Import failed"))
    job_id = enqueue(store)
    tick(harness, job_id)
    tick(harness, job_id)
    assert store.get(job_id).status == "FAILED"
    assert store.get(job_id).correction_count == 0


def test_stale_sha_cannot_be_verified(harness, store, monkeypatch):
    monkeypatch.setattr(harness.github, "unchanged", lambda _: False)
    job_id = enqueue(store)
    tick(harness, job_id)
    tick(harness, job_id)
    assert store.get(job_id).status == "FAILED"
    assert "head changed" in store.get(job_id).failure_reason


def test_provider_completion_without_pr_is_not_verified(harness, store, monkeypatch):
    monkeypatch.setattr(harness.github, "discover", lambda *_: None)
    job_id = enqueue(store)
    tick(harness, job_id)
    tick(harness, job_id)
    assert store.get(job_id).status == "DEVIN_RUNNING"
    store.change(job_id, "EXPIRE", created_at=0)
    tick(harness, job_id)
    assert store.get(job_id).status == "FAILED"


def test_session_error(harness, store):
    job_id = enqueue(store)
    tick(harness, job_id)
    harness.devin.sessions[store.get(job_id).devin_session_id].status = "error"
    tick(harness, job_id)
    assert store.get(job_id).status == "FAILED"


def test_unknown_correction_is_not_resent(harness, store, monkeypatch):
    def timeout(*_):
        raise ProviderError("Lost correction response", retryable=True)

    monkeypatch.setattr(harness.devin, "correct", timeout)
    job_id = enqueue(store, 102, "schema-no-engine")
    for _ in range(3):
        tick(harness, job_id)
    assert store.get(job_id).status == "ESCALATED"
    assert store.get(job_id).correction_count == 1
