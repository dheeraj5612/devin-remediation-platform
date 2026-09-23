"""An infrastructure-only retry can repair a verdict without creating another job."""

import pytest

from app.db import Store
from app.metrics import metrics


def test_exact_sha_infra_retry_records_verified_without_new_job(live):
    """Promote only the original failed candidate when fresh independent proof passes."""
    store = Store(live)
    job, duplicate = store.enqueue("delivery-retry", live.github_repository, 101, "histogram-invalid-column")
    assert not duplicate
    store.change(job.id, "FAILED", status="FAILED", validation_status="INFRA_ERROR",
                 candidate_sha="a" * 40, candidate_pr_number=10, devin_session_id="session-original")

    result = store.record_infra_revalidation(job.id, "a" * 40,
                                              {"outcome": "VERIFIED", "summary": "Independent checks passed",
                                               "evidence": {"sha": "a" * 40}})

    assert result.status == "VERIFIED"
    assert result.validated_sha == "a" * 40
    assert result.failure_reason is None
    assert len(store.jobs()) == 1
    assert store.events(job.id)[-1].event_type == "INFRA_REVALIDATED"
    assert metrics(store)["first_pass_denominator"] == 1
    assert metrics(store)["first_pass"] == 1
    with pytest.raises(ValueError, match="Only the same failed"):
        store.record_infra_revalidation(job.id, "a" * 40, {"outcome": "VERIFIED"})


def test_infra_retry_rejects_changed_sha_and_retains_failure(live):
    """A wrong commit cannot borrow the earlier PR's failed attempt or later verdict."""
    store = Store(live)
    job, _ = store.enqueue("delivery-retry-2", live.github_repository, 102, "schema-missing-engine")
    store.change(job.id, "FAILED", status="FAILED", validation_status="INFRA_ERROR",
                 candidate_sha="b" * 40)

    with pytest.raises(ValueError, match="Only the same failed"):
        store.record_infra_revalidation(job.id, "c" * 40, {"outcome": "VERIFIED"})
    with pytest.raises(ValueError, match="exact candidate SHA"):
        store.record_infra_revalidation(job.id, "b" * 40,
                                        {"outcome": "VERIFIED", "evidence": {"sha": "c" * 40}})

    unchanged = store.get(job.id)
    assert unchanged.status == "FAILED"
    assert unchanged.validated_sha is None
