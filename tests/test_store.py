from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Store
from conftest import enqueue


def test_persistence_and_transitions(settings, store):
    job_id = enqueue(store)
    store.change(job_id, "START", status="DEVIN_RUNNING", devin_session_id="existing")
    reloaded = Store(settings.database_path)
    assert reloaded.get(job_id).devin_session_id == "existing"
    assert [event.event_type for event in reloaded.events(job_id)] == ["QUEUED", "START"]
    with pytest.raises(ValueError, match="Invalid transition"):
        reloaded.change(job_id, "BAD", status="VERIFIED")
    reloaded.engine.dispose()


def test_concurrent_delivery_deduplication(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.enqueue("same", "SIMULATION", "example/superset", 101, "histogram-invalid"), range(8)))
    assert len({result[0] for result in results}) == 1
    assert sum(not result[1] for result in results) == 1
    assert store.metrics("SIMULATION")["duplicates"] == 7


def test_concurrent_logical_deduplication(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: store.enqueue(str(index), "SIMULATION", "example/superset", 101, "histogram-invalid"), range(8)))
    assert len({result[0] for result in results}) == 1
    assert len(store.jobs("SIMULATION")) == 1
