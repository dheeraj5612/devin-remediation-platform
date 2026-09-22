"""Focused regression tests for the hardened core store, metrics, and report helpers."""

from types import SimpleNamespace

import pytest

from app.metrics import metrics_from_records
from app.models import now
from app.report import _case_proof, _safe_event_details, _validation


def test_events_empty_job_id_is_a_filter(settings, store):
    """An explicitly empty job ID must not widen an event query to every job."""

    store.enqueue("delivery", settings.github_repository, 101, "histogram-invalid-column")

    assert store.events("") == []


def test_defer_unknown_job_raises_key_error(store):
    """Deferring a missing job uses the store's normal lookup error boundary."""

    with pytest.raises(KeyError):
        store.defer("missing", 1)


def test_metrics_ignore_unmatched_events_and_incomplete_timestamps():
    """Metrics tolerate incomplete rows and do not count events outside the job snapshot."""

    job = SimpleNamespace(
        id="job",
        created_at=now(),
        pr_created_at=None,
        completed_at=None,
        started_at=None,
        status="VERIFIED",
        correction_count=0,
        validation_status=None,
    )
    event = SimpleNamespace(
        job_id="other",
        event_type="EVALUATED",
        details={"outcome": "VERIFIED", "correction_count": 0},
    )

    result = metrics_from_records([job], [event])

    assert result["first_pass_denominator"] == 0
    assert result["median_event_to_verified_seconds"] is None


def test_report_downgrades_malformed_validation_and_event_statuses():
    """Malformed persisted JSON becomes visible uncertainty instead of a report exception."""

    job = SimpleNamespace(
        validation=["untrusted"],
        validation_status=["untrusted"],
        candidate_sha=None,
        validated_sha=None,
    )
    event = SimpleNamespace(details={"outcome": {"untrusted": True}, "sha": "abc"})

    validation = _validation(job)

    assert validation["outcome"] == "UNCLASSIFIED"
    assert validation["summary"] is None
    assert _safe_event_details(event) == {"sha": "abc"}


def test_case_proof_rejects_json_scalars():
    """A JSON scalar returned by a corrupted proof file is reported as pending evidence."""

    settings = SimpleNamespace(mode="LIVE")
    registry = SimpleNamespace(evidence=lambda _case: ["not", "a", "proof"])

    result = _case_proof(settings, registry, SimpleNamespace(kind="test_quality"))

    assert result["evidence_level"] == "BASELINE_PENDING"
