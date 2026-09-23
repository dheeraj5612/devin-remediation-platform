"""Unit and rendering checks for the dashboard's "is it working" leader strip and throughput view."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.metrics import human_duration, leader_strip, metrics
from app.models import now
from app.report import build_report


def test_human_duration_formats_and_reports_absence():
    """Durations read as short human words; a missing measurement never becomes a fake zero."""
    # ELI5: an absent duration must say so instead of implying an instant zero-second fix.
    assert human_duration(None) == "not recorded"
    assert human_duration(30) == "30 sec"
    assert human_duration(120) == "2 min"
    assert human_duration(7200) == "2.0 hr"
    assert human_duration(48 * 3600) == "2.0 day"


def test_leader_strip_counts_verified_terminal_and_attention(settings, store):
    """Leader tiles reflect terminal/verified/attention counts and show explicit denominators."""
    timestamp = now() - timedelta(minutes=10)
    # ELI5: one verified job, one escalated job, one still-active job.
    verified, _ = store.enqueue("d1", settings.github_repository, 201, "histogram-invalid-column")
    store.change(verified.id, "STARTED", status="DEVIN_RUNNING", created_at=timestamp, started_at=timestamp)
    store.change(verified.id, "PR", status="PR_OPENED", pr_created_at=timestamp + timedelta(seconds=30))
    store.change(verified.id, "VALIDATING", status="VALIDATING")
    store.change(verified.id, "EVALUATED", details={"outcome": "VERIFIED", "correction_count": 0})
    store.change(verified.id, "VERIFIED", status="VERIFIED", completed_at=timestamp + timedelta(seconds=90))

    escalated, _ = store.enqueue("d2", settings.github_repository, 202, "histogram-invalid-column")
    store.change(escalated.id, "STARTED", status="DEVIN_RUNNING", created_at=timestamp, started_at=timestamp)
    store.change(escalated.id, "ESCALATED", status="ESCALATED")

    store.enqueue("d3", settings.github_repository, 203, "histogram-invalid-column")

    values = metrics(store)
    leader = values["leader"]
    # ELI5: two terminal jobs (verified + escalated); the still-queued job stays out of the denominator.
    assert leader["terminal_count"] == 2
    assert leader["verified_count"] == 1
    assert leader["verification_rate_label"] == "1 of 2"
    # ELI5: the persisted completion timestamp yields ninety seconds, formatted as minutes.
    assert leader["median_label_to_verified"] == "2 min"
    # ELI5: only the escalated job needs attention.
    assert leader["attention_count"] == 1
    assert leader["attention_job_ids"] == [escalated.id]
    # ELI5: no event ever recorded a provider ACU figure, so the tile stays honestly unrecorded.
    assert leader["acus_per_verified_fix"]["status"] == "NOT_RECORDED"
    assert leader["acus_per_verified_fix"]["value"] is None
    assert leader["acus_per_verified_fix"]["label"] == "provider-reported"


def test_leader_strip_reports_acus_only_when_the_provider_actually_sends_them(settings, store):
    """A recorded `acu_consumed` figure on a verified job's own events feeds the ACU tile; nothing else invents one."""
    timestamp = now() - timedelta(minutes=5)
    job, _ = store.enqueue("d4", settings.github_repository, 301, "histogram-invalid-column")
    store.change(job.id, "STARTED", status="DEVIN_RUNNING", created_at=timestamp, started_at=timestamp)
    store.change(job.id, "PR", status="PR_OPENED", pr_created_at=timestamp + timedelta(seconds=5))
    store.change(job.id, "VALIDATING", status="VALIDATING")
    store.change(job.id, "EVALUATED", details={"outcome": "VERIFIED", "correction_count": 0})
    # ELI5: simulate a provider poll that happened to carry a consumption figure.
    store.change(job.id, "PROVIDER_POLL", details={"acu_consumed": 1.0})
    # ELI5: polls report a running total, so a later 3.5 replaces 1.0 rather than adding to it.
    store.change(job.id, "PROVIDER_POLL", details={"acu_consumed": 3.5})
    store.change(job.id, "VERIFIED", status="VERIFIED", completed_at=timestamp + timedelta(seconds=10))

    leader = leader_strip(store.jobs(), store.events(), metrics(store))
    assert leader["acus_per_verified_fix"]["status"] == "RECORDED"
    assert leader["acus_per_verified_fix"]["value"] == 3.5


def test_dashboard_renders_leader_strip_and_throughput(settings, store):
    """The rendered dashboard exposes the leader strip and per-job throughput rows for a seeded store."""
    timestamp = now() - timedelta(minutes=15)
    job, _ = store.enqueue("d5", settings.github_repository, 401, "histogram-invalid-column")
    store.change(job.id, "STARTED", status="DEVIN_RUNNING", created_at=timestamp, started_at=timestamp)
    store.change(job.id, "PR", status="PR_OPENED", pr_created_at=timestamp + timedelta(seconds=60))
    store.change(job.id, "VALIDATING", status="VALIDATING")
    store.change(job.id, "EVALUATED", details={"outcome": "VERIFIED", "correction_count": 0})
    store.change(job.id, "VERIFIED", status="VERIFIED", completed_at=timestamp + timedelta(seconds=120))

    with TestClient(create_app(settings)) as client:
        html = client.get("/dashboard").text
    assert 'aria-label="Is it working: leadership summary"' in html
    assert "Verified fixes" in html
    assert "ACUs / verified fix" in html
    assert "Not reported" in html
    assert "1 label + 1 PR review, always" in html
    assert "Run throughput" in html
    assert "Label → PR: 1 min" in html
    assert "Label → verified: 2 min" in html


def test_report_json_includes_throughput_ordered_by_admission(settings, store):
    """`/report.json` exposes ordered throughput rows built from the same durable snapshot."""
    from app.cases import Registry

    first_time = now() - timedelta(minutes=20)
    second_time = now() - timedelta(minutes=5)
    older, _ = store.enqueue("d6", settings.github_repository, 501, "histogram-invalid-column")
    store.change(older.id, "STARTED", status="DEVIN_RUNNING", created_at=first_time, started_at=first_time)
    newer, _ = store.enqueue("d7", settings.github_repository, 502, "histogram-invalid-column")
    store.change(newer.id, "STARTED", status="DEVIN_RUNNING", created_at=second_time, started_at=second_time)

    report = build_report(settings, store, Registry(settings))
    ids = [row["id"] for row in report["throughput"]]
    assert ids == [older.id, newer.id]
    assert report["throughput"][0]["label_to_pr"] == "not recorded"
