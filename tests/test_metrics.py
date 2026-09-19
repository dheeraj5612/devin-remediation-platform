from fastapi.testclient import TestClient

from app.demo import seed
from app.main import create_app
from conftest import enqueue


def finish(store, job_id, corrections=0, outcome="VERIFIED"):
    store.change(job_id, "START", status="DEVIN_RUNNING", created_at=100)
    store.change(job_id, "PR", status="PR_OPENED", pr_created_at=120)
    store.change(job_id, "EVAL", status="VALIDATING", validation_status="VERIFIED", correction_count=corrections)
    store.change(job_id, "END", status=outcome, completed_at=140)


def test_metrics_denominators_latencies_and_modes(store):
    first = enqueue(store)
    finish(store, first)
    recovered = enqueue(store, 102)
    finish(store, recovered, 1)
    escalated = enqueue(store, 103)
    finish(store, escalated, 1, "ESCALATED")
    enqueue(store, 104)
    store.enqueue("live", "LIVE", "example/superset", 105, "histogram-invalid")
    metrics = store.metrics("SIMULATION")
    assert metrics["attempted"] == 4 and metrics["active"] == 1
    assert metrics["first_pass"] == [1, 3]
    assert metrics["recovery"] == [1, 2]
    assert metrics["event_to_pr"] == 20 and metrics["event_to_verified"] == 40
    assert store.metrics("LIVE")["attempted"] == 1
    assert store.metrics("LIVE")["first_pass"] == [0, 0]


def test_full_demo_dashboard_timeline_and_health(tmp_path):
    settings, store, metrics = seed(tmp_path)
    assert metrics["first_pass"] == [2, 4]
    assert metrics["recovery"] == [1, 2]
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/health").json()["mode"] == "SIMULATION"
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "These are not Superset repair results" in dashboard.text
        assert "DETECTED" in dashboard.text and "ESCALATED" in dashboard.text
        job = store.jobs("SIMULATION")[0]
        assert "SESSION_ATTACHED" in client.get(f"/jobs/{job.id}").text
        assert client.get("/jobs/nonexistent").status_code == 404


def test_dashboard_escapes_untrusted_values(settings, store):
    job_id = enqueue(store)
    store.change(job_id, "FAIL", status="FAILED", failure_reason="<script>alert(1)</script>")
    with TestClient(create_app(settings, store)) as client:
        content = client.get("/").text
        assert "<script>" not in content
        assert "&lt;script&gt;" in content
