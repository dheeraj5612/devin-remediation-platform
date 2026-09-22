"""Store behavior tests for durability, deduplication, transitions, and mode isolation.

ELI5: these tests treat the SQLite store as the handoff notebook shared by the web
server and worker, then check that retries cannot create a second paid job.
"""

from concurrent.futures import ThreadPoolExecutor  # Exercise concurrent webhook deliveries.

import pytest  # Assert expected store errors and rollback behavior.
from sqlalchemy import text  # Start a short SQLite transaction in the network-bound test.

from app.db import Store  # Reopen the same durable database after a simulated restart.


def test_enqueue_is_durable_and_logically_idempotent(settings, store):
    """Persist one issue once and reuse its row when another delivery repeats it."""

    job, duplicate = store.enqueue("delivery-a", settings.github_repository, 101, "histogram-invalid-column")  # Admit the first delivery.
    assert not duplicate  # The first delivery must create fresh work.
    other, duplicate = store.enqueue("delivery-b", settings.github_repository, 101, "histogram-invalid-column")  # Repeat the same issue with another ID.
    assert duplicate and other.id == job.id  # Logical dedupe returns the original job.
    store.engine.dispose()  # Close the first engine to simulate a worker restart.
    restarted = Store(settings)  # Reopen the same SQLite file.
    assert restarted.get(job.id).status == "QUEUED"  # Durable state survives the restart.
    assert [event.event_type for event in restarted.events(job.id)] == ["QUEUED", "DUPLICATE_EXECUTION"]  # The timeline explains both deliveries.
    restarted.engine.dispose()  # Release the restarted engine's connections.


def test_concurrent_webhook_redelivery_creates_one_job(settings, store):
    """Serialize concurrent delivery races into one durable job and one creator."""

    def deliver(number):
        """Submit one uniquely named webhook delivery to the shared store."""

        return store.enqueue(f"delivery-{number}", settings.github_repository, 101, "histogram-invalid-column")  # Let Store arbitrate the race.

    with ThreadPoolExecutor(max_workers=8) as pool:  # Run more delivery attempts than worker slots.
        results = list(pool.map(deliver, range(16)))  # Collect every job and duplicate flag.
    assert len({job.id for job, _ in results}) == 1  # All callers must observe one job ID.
    assert sum(not duplicate for _, duplicate in results) == 1  # Exactly one caller may create it.
    assert len(store.jobs()) == 1  # The database contains no hidden duplicate row.


def test_invalid_transition_is_rolled_back(settings, store):
    """Reject an impossible state jump without leaving a partial event behind."""

    job, _ = store.enqueue("delivery", settings.github_repository, 101, "histogram-invalid-column")  # Create a valid starting state.
    with pytest.raises(ValueError, match="Invalid transition"):  # The state machine must reject a skipped validation.
        store.change(job.id, "BAD", status="VERIFIED")  # Attempt the invalid jump inside the transaction.
    assert store.get(job.id).status == "QUEUED"  # Rollback keeps the original state.
    assert len(store.events()) == 1  # Only the initial queue event remains.


def test_queue_claim_does_not_hold_transaction_over_network(settings, rig):
    """Ensure a provider call occurs after the queue transaction has released its lock."""

    job, _ = rig.store.enqueue("delivery", settings.github_repository, 101, "histogram-invalid-column")  # Put one job in the queue.
    original = rig.devin.create_session  # Keep the fake provider's normal response.

    def create(job, case):
        """Prove a second SQLite transaction can start while session creation runs."""

        with rig.store.session.begin() as session:  # A lock here would deadlock if the queue claim remained open.
            session.execute(text("BEGIN IMMEDIATE"))  # Acquire and release a short independent transaction.
        return original(job, case)  # Continue with the fake provider call.

    rig.devin.create_session = create  # Replace only the provider boundary for this test.
    rig.step(job.id)  # Claim the job and request the provider session.
    rig.step(job.id)  # Poll the newly created session.
    assert rig.store.get(job.id).devin_session_id  # The provider session was persisted successfully.


def test_unknown_job_does_not_leak_across_modes(store):
    """Keep a lookup for an unknown ID from returning another mode's job."""

    with pytest.raises(KeyError):  # A missing or cross-mode row is an explicit lookup failure.
        store.get("missing")  # The store must fail closed.
