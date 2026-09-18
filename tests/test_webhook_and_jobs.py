from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from drp import db
from drp.config import Settings
from drp.github.client import finding_marker
from drp.github.webhook import verify_signature
from drp.jobs import (
    InvalidTransitionError,
    claim_next,
    create_job,
    record_error,
    transition,
)
from drp.models import BaselineStatus, Finding, JobState, RemediationJob, WebhookDelivery, utcnow
from drp.web.app import app


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(finding: Finding, *, label: str = "devin-remediate", action: str = "labeled") -> bytes:
    return json.dumps(
        {
            "action": action,
            "label": {"name": label},
            "repository": {"full_name": finding.repo},
            "sender": {"login": "dheeraj5612"},
            "issue": {
                "number": 7,
                "state": "open",
                "html_url": "https://github.com/example/mathlib/issues/7",
                "body": "weak test\n\n" + finding_marker(finding.id),
            },
        }
    ).encode()


def _post(client: TestClient, settings: Settings, body: bytes, delivery: str, **hdr: str) -> Any:
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": _sign(settings.github_webhook_secret, body),
    }
    headers.update(hdr)
    return client.post("/webhooks/github", content=body, headers=headers)


@pytest.fixture
def confirmed(finding: Finding, db_session: Session) -> Finding:
    finding.baseline_status = BaselineStatus.CONFIRMED
    db_session.commit()
    return finding


def test_verify_signature() -> None:
    body = b'{"a":1}'
    assert verify_signature("k", body, _sign("k", body))
    assert not verify_signature("k", body, _sign("other", body))
    assert not verify_signature("k", body, None)
    assert not verify_signature("k", body, "sha1=abc")
    assert verify_signature("", body, None)  # secret not configured -> not enforced


def test_webhook_rejects_bad_signature(confirmed: Finding, settings: Settings) -> None:
    client = TestClient(app)
    body = _payload(confirmed)
    r = _post(client, settings, body, "d1", **{"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 401
    with db.session_scope() as s:
        assert s.scalars(select(RemediationJob)).first() is None


def test_webhook_label_gate_repo_gate_and_marker(confirmed: Finding, settings: Settings) -> None:
    client = TestClient(app)
    r = _post(client, settings, _payload(confirmed, label="bug"), "d-label")
    assert r.json()["outcome"].startswith("ignored: label=")
    r = _post(client, settings, _payload(confirmed, action="opened"), "d-action")
    assert r.json()["outcome"].startswith("ignored: action=")
    body = _payload(confirmed).replace(confirmed.repo.encode(), b"someone/else")
    r = _post(client, settings, body, "d-repo")
    assert r.json()["outcome"].startswith("ignored: repository=")
    body = _payload(confirmed).replace(confirmed.id.encode(), b"unknown-finding")
    r = _post(client, settings, body, "d-marker")
    assert "no known finding marker" in r.json()["outcome"]
    with db.session_scope() as s:
        assert s.scalars(select(RemediationJob)).first() is None
        assert len(s.scalars(select(WebhookDelivery)).all()) == 4


def test_webhook_requires_confirmed_baseline(finding: Finding, settings: Settings) -> None:
    client = TestClient(app)
    r = _post(client, settings, _payload(finding), "d-unconfirmed")
    assert "baseline is unregistered" in r.json()["outcome"]


def test_webhook_creates_job_once_and_is_idempotent(confirmed: Finding, settings: Settings) -> None:
    client = TestClient(app)
    body = _payload(confirmed)
    r1 = _post(client, settings, body, "d-1")
    assert r1.json()["outcome"] == "job created"
    job_id = r1.json()["job_id"]
    # redelivery of the same delivery id
    r2 = _post(client, settings, body, "d-1")
    assert r2.json() == {"ok": True, "outcome": "duplicate delivery", "job_id": job_id}
    # new delivery (label removed and re-added) while job is active
    r3 = _post(client, settings, body, "d-2")
    assert r3.json()["outcome"] == "duplicate: job already active"
    assert r3.json()["job_id"] == job_id
    with db.session_scope() as s:
        jobs = s.scalars(select(RemediationJob)).all()
        assert len(jobs) == 1
        assert jobs[0].state == JobState.QUEUED
        assert jobs[0].triggered_by == "dheeraj5612"
        assert jobs[0].trigger_delivery_id == "d-1"
        assert s.get(Finding, confirmed.id).github_issue_number == 7  # type: ignore[union-attr]


def test_transitions_are_validated_and_audited(confirmed: Finding, db_session: Session) -> None:
    job, created = create_job(
        db_session, confirmed, issue_number=1, delivery_id="x", triggered_by="u"
    )
    assert created
    with pytest.raises(InvalidTransitionError):
        transition(db_session, job, JobState.VERIFIED)
    transition(db_session, job, JobState.SESSION_REQUESTED)
    transition(db_session, job, JobState.SESSION_RUNNING)
    assert job.session_created_at is not None
    transition(db_session, job, JobState.PR_READY)
    transition(db_session, job, JobState.VALIDATING)
    transition(db_session, job, JobState.VERIFIED, "clean pass + mutant detected")
    assert job.finished_at is not None and job.outcome_reason == "clean pass + mutant detected"
    with pytest.raises(InvalidTransitionError):
        transition(db_session, job, JobState.QUEUED)
    db_session.flush()
    assert [e.to_state for e in job.events] == [
        "queued",
        "session_requested",
        "session_running",
        "pr_ready",
        "validating",
        "verified",
    ]


def test_lease_claim_and_crash_recovery(confirmed: Finding, db_session: Session) -> None:
    job, _ = create_job(db_session, confirmed, issue_number=1, delivery_id=None, triggered_by=None)
    db_session.commit()
    claimed = claim_next(db_session, "w1", lease_seconds=600)
    assert claimed is not None and claimed.id == job.id
    assert claim_next(db_session, "w2", lease_seconds=600) is None  # leased by w1
    # simulate w1 crashing: lease expires
    job.lease_until = utcnow() - timedelta(seconds=1)
    db_session.flush()
    reclaimed = claim_next(db_session, "w2", lease_seconds=600)
    assert reclaimed is not None and reclaimed.lease_owner == "w2"
    # future next_run_at is not claimable
    transition(db_session, job, JobState.SESSION_REQUESTED, delay_s=3600)
    db_session.flush()
    assert claim_next(db_session, "w3", lease_seconds=600) is None


def test_record_error_backs_off_then_escalates(confirmed: Finding, db_session: Session) -> None:
    job, _ = create_job(db_session, confirmed, issue_number=1, delivery_id=None, triggered_by=None)
    record_error(db_session, job, "boom", max_errors=3, backoff_s=10)
    assert job.error_count == 1 and job.state == JobState.QUEUED
    assert job.next_run_at > utcnow() + timedelta(seconds=5)
    record_error(db_session, job, "boom", max_errors=3, backoff_s=10)
    assert job.next_run_at > utcnow() + timedelta(seconds=15)
    record_error(db_session, job, "boom", max_errors=3, backoff_s=10)
    assert job.state == JobState.ESCALATED
    assert "infrastructure failure" in (job.outcome_reason or "")
