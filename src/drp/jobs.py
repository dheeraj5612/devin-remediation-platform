"""Durable remediation job service: creation, idempotency and audited state transitions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from drp.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    Finding,
    JobEvent,
    JobState,
    RemediationJob,
    utcnow,
)


class InvalidTransitionError(RuntimeError):
    pass


def transition(
    session: Session,
    job: RemediationJob,
    to_state: JobState,
    message: str = "",
    data: dict[str, Any] | None = None,
    *,
    delay_s: float = 0.0,
) -> None:
    if to_state not in ALLOWED_TRANSITIONS[job.state]:
        raise InvalidTransitionError(f"{job.state.value} -> {to_state.value} is not allowed")
    session.add(
        JobEvent(
            job_id=job.id,
            from_state=job.state.value,
            to_state=to_state.value,
            message=message,
            data=data or {},
        )
    )
    job.state = to_state
    now = utcnow()
    job.next_run_at = now + timedelta(seconds=delay_s)
    job.lease_until = None
    job.lease_owner = None
    if to_state == JobState.SESSION_RUNNING and job.session_created_at is None:
        job.session_created_at = now
    if to_state == JobState.PR_READY and job.pr_ready_at is None:
        job.pr_ready_at = now
    if to_state in TERMINAL_STATES:
        job.finished_at = now
        job.outcome_reason = message


def note(
    session: Session, job: RemediationJob, message: str, data: dict[str, Any] | None = None
) -> None:
    """Record an event that does not change state (e.g. poll results)."""
    session.add(
        JobEvent(
            job_id=job.id,
            from_state=job.state.value,
            to_state=job.state.value,
            message=message,
            data=data or {},
        )
    )


def active_job_for_finding(session: Session, finding_id: str) -> RemediationJob | None:
    stmt = (
        select(RemediationJob)
        .where(RemediationJob.finding_id == finding_id)
        .where(RemediationJob.state.not_in(list(TERMINAL_STATES)))
        .order_by(RemediationJob.created_at.desc())
    )
    return session.scalars(stmt).first()


def create_job(
    session: Session,
    finding: Finding,
    *,
    issue_number: int,
    delivery_id: str | None,
    triggered_by: str | None,
) -> tuple[RemediationJob, bool]:
    """Create a job unless one is already active for the finding. Returns (job, created)."""
    existing = active_job_for_finding(session, finding.id)
    if existing is not None:
        return existing, False
    job = RemediationJob(
        finding_id=finding.id,
        issue_number=issue_number,
        trigger_delivery_id=delivery_id,
        triggered_by=triggered_by,
        state=JobState.QUEUED,
    )
    session.add(job)
    session.flush()
    session.add(
        JobEvent(
            job_id=job.id,
            from_state=None,
            to_state=JobState.QUEUED.value,
            message=f"job created from issue #{issue_number}",
            data={"delivery_id": delivery_id, "triggered_by": triggered_by},
        )
    )
    return job, True


def claim_next(session: Session, owner: str, lease_seconds: int) -> RemediationJob | None:
    """Lease the next runnable job. Expired leases are reclaimable, which is what makes the
    worker crash-safe: a job is never lost, only re-driven from its persisted state."""
    now = utcnow()
    stmt = (
        select(RemediationJob)
        .where(RemediationJob.state.not_in(list(TERMINAL_STATES)))
        .where(RemediationJob.next_run_at <= now)
        .where((RemediationJob.lease_until.is_(None)) | (RemediationJob.lease_until < now))
        .order_by(RemediationJob.next_run_at)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None
    job.lease_owner = owner
    job.lease_until = now + timedelta(seconds=lease_seconds)
    session.flush()
    return job


def record_error(
    session: Session, job: RemediationJob, error: str, *, max_errors: int, backoff_s: float
) -> None:
    """Transient failure handling: exponential backoff, escalate once the budget is exhausted."""
    job.error_count += 1
    job.last_error = error[-4000:]
    note(session, job, f"transient error #{job.error_count}: {error[:500]}")
    if job.error_count >= max_errors:
        transition(
            session,
            job,
            JobState.ESCALATED,
            f"infrastructure failure after {job.error_count} errors: {error[:300]}",
        )
        return
    job.lease_until = None
    job.lease_owner = None
    job.next_run_at = utcnow() + timedelta(seconds=backoff_s * (2 ** (job.error_count - 1)))
