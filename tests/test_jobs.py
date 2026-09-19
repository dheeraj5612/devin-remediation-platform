from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.db import Store


def test_enqueue_is_durable_and_logically_idempotent(settings, store):
    job, duplicate = store.enqueue("delivery-a", settings.github_repository, 101, "histogram-invalid-column")
    assert not duplicate
    other, duplicate = store.enqueue("delivery-b", settings.github_repository, 101, "histogram-invalid-column")
    assert duplicate and other.id == job.id
    store.engine.dispose()
    restarted = Store(settings)
    assert restarted.get(job.id).status == "QUEUED"
    assert [event.event_type for event in restarted.events(job.id)] == ["QUEUED", "DUPLICATE_EXECUTION"]
    restarted.engine.dispose()


def test_concurrent_webhook_redelivery_creates_one_job(settings, store):
    def deliver(number):
        return store.enqueue(f"delivery-{number}", settings.github_repository, 101, "histogram-invalid-column")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(deliver, range(16)))
    assert len({job.id for job, _ in results}) == 1
    assert sum(not duplicate for _, duplicate in results) == 1
    assert len(store.jobs()) == 1


def test_invalid_transition_is_rolled_back(settings, store):
    job, _ = store.enqueue("delivery", settings.github_repository, 101, "histogram-invalid-column")
    with pytest.raises(ValueError, match="Invalid transition"):
        store.change(job.id, "BAD", status="VERIFIED")
    assert store.get(job.id).status == "QUEUED"
    assert len(store.events()) == 1


def test_queue_claim_does_not_hold_transaction_over_network(settings, rig):
    job, _ = rig.store.enqueue("delivery", settings.github_repository, 101, "histogram-invalid-column")
    original = rig.devin.create_session
    def create(job, case):
        with rig.store.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
        return original(job, case)
    rig.devin.create_session = create
    rig.step(job.id)
    rig.step(job.id)
    assert rig.store.get(job.id).devin_session_id


def test_unknown_job_does_not_leak_across_modes(store):
    with pytest.raises(KeyError):
        store.get("missing")
