import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.cases import Registry, detect
from app.db import Store
from app.main import create_app
from app.metrics import metrics
from app.models import now
from app.simulation import run_demo


def test_detector_is_only_a_static_finding():
    source = """def test_weak():
    try:
        operation()
    except ValueError:
        assert True

def test_strong():
    assert operation() == 1
"""
    findings = detect(source, "tests/example.py")
    assert len(findings) == 1
    assert findings[0]["test"] == "test_weak"
    assert findings[0]["status"] == "SUSPICIOUS_NOT_CONFIRMED"


@pytest.mark.parametrize("field,value", [("outcome", "REJECTED"), ("mode", "SIMULATION"),
    ("sha", "0" * 40), ("case_fingerprint", "stale"), ("harness_fingerprint", "stale"),
    ("normal", "INFRA_ERROR"), ("mutant", "NOT_RUN"), ("case_id", "other-case")])
def test_live_evidence_must_be_current_and_confirmed(live, field, value):
    registry = Registry(live)
    case = registry.cases["histogram-invalid-column"]
    assert registry.evidence(case)["outcome"] == "CONFIRMED"
    path = live.storage / "baselines" / f"{case.id}.json"
    proof = json.loads(path.read_text())
    proof[field] = value
    path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="stale"):
        registry.evidence(case)


def test_duplicate_issue_bindings_are_rejected(settings):
    settings.case_issues = {"histogram-invalid-column": 1, "schema-missing-engine": 1}
    with pytest.raises(ValueError, match="unique"):
        Registry(settings)


def test_simulation_exercises_real_state_machine_with_separate_storage(settings):
    result = run_demo(settings)
    assert result["mode"] == "SIMULATION"
    assert result["sessions_created"] == 4
    assert result["first_pass"] == 2 and result["first_pass_denominator"] == 4
    assert result["correction_recovery"] == 1 and result["correction_denominator"] == 2
    assert result["duplicate_deliveries"] == 1
    store = Store(settings)
    assert "WORKER_RESUMED" in [event.event_type for event in store.events()]
    with TestClient(create_app(settings)) as client:
        page = client.get("/").text
        assert "SIMULATION" in page
        assert "DETECTED" in page
        job = store.jobs()[0]
        detail = client.get(f"/jobs/{job.id}")
        assert detail.status_code == 200 and "VERIFIED" in detail.text
    live = settings.model_copy(update={"mode": "LIVE"})
    live_store = Store(live)
    assert not live_store.jobs()
    assert metrics(live_store)["attempted"] == 0
    assert settings.storage != live.storage
    with pytest.raises(ValueError, match="never"):
        run_demo(live)
    store.engine.dispose()
    live_store.engine.dispose()


def test_latencies_and_denominators_exclude_infrastructure(settings, store):
    timestamp = now() - timedelta(minutes=5)
    for issue in [101, 102]:
        job, _ = store.enqueue(str(issue), settings.github_repository, issue, "histogram-invalid-column")
        store.change(job.id, "STARTED", status="DEVIN_RUNNING", created_at=timestamp, started_at=timestamp)
        store.change(job.id, "PR", status="PR_OPENED", pr_created_at=timestamp + timedelta(seconds=10))
        store.change(job.id, "VALIDATING", status="VALIDATING")
        outcome = "VERIFIED" if issue == 101 else "INFRA_ERROR"
        store.change(job.id, "EVALUATED", details={"outcome": outcome, "correction_count": 0})
        store.change(job.id, outcome if issue == 101 else "FAILED", status=outcome if issue == 101 else "FAILED",
                     completed_at=timestamp + timedelta(seconds=20))
    values = metrics(store)
    assert values["first_pass_denominator"] == 1
    assert values["first_pass"] == 1
    assert values["correction_denominator"] == 0
    assert values["median_event_to_pr_seconds"] == 10
    assert values["median_event_to_verified_seconds"] == 20
