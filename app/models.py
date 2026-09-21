"""Database tables and the job state machine.

ELI5: a `Job` is one attempt to fix one weak test. It moves through a fixed
list of statuses (below). `Event` rows are the job's diary; `Delivery` rows
remember every webhook GitHub sent us so a re-sent webhook is not a new job.

    QUEUED -> DEVIN_RUNNING -> PR_OPENED -> VALIDATING -> VERIFIED
                                   ^            |
                                   +- CORRECTING (one retry, same session)
    Any non-terminal state can end in FAILED (our problem) or ESCALATED (a human must look).
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Once a job is here, the worker never touches it again.
TERMINAL = {"VERIFIED", "ESCALATED", "FAILED"}

# Allowed moves. `Store.change` refuses anything not listed here, so a bug can't skip validation.
TRANSITIONS = {
    "QUEUED": {"DEVIN_RUNNING", "FAILED"},
    "DEVIN_RUNNING": {"PR_OPENED", "FAILED", "ESCALATED"},
    "PR_OPENED": {"VALIDATING", "FAILED", "ESCALATED"},
    "VALIDATING": {"VERIFIED", "CORRECTING", "PR_OPENED", "FAILED", "ESCALATED"},  # PR_OPENED = head SHA moved
    "CORRECTING": {"PR_OPENED", "FAILED", "ESCALATED"},
}


def now() -> datetime:
    # Naive UTC everywhere; SQLite has no timezone type.
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    # One live job per issue: the same issue labelled twice must not start two Devin sessions.
    __table_args__ = (UniqueConstraint("mode", "repository", "issue_number", "generation"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid4().hex)
    mode: Mapped[str]  # LIVE or SIMULATION, so demo rows never mix with real ones
    case_id: Mapped[str]  # which approved weak test (see evals/cases.yaml)
    github_delivery_id: Mapped[str]  # the webhook delivery that created this job
    repository: Mapped[str]
    issue_number: Mapped[int]
    generation: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="QUEUED", index=True)

    # What Devin is doing.
    devin_session_id: Mapped[str | None]
    devin_session_url: Mapped[str | None]
    provider_status: Mapped[str | None]  # last seen "status/status_detail" from the Devin API

    # The PR Devin opened and which commit of it we judged.
    candidate_pr_number: Mapped[int | None]
    candidate_pr_url: Mapped[str | None]
    candidate_sha: Mapped[str | None]
    validated_sha: Mapped[str | None]  # only set on VERIFIED; the exact commit that passed

    # Bookkeeping flags that make retries safe (see Orchestrator for how each is used).
    correction_count: Mapped[int] = mapped_column(default=0)  # max 1
    launch_requested: Mapped[bool] = mapped_column(default=False)  # "we sent the create-session call"
    correction_requested: Mapped[bool] = mapped_column(default=False)  # "we sent the correction message"
    correction_acknowledged: Mapped[bool] = mapped_column(default=False)  # "...and it was accepted"
    api_failures: Mapped[int] = mapped_column(default=0)  # consecutive provider errors
    stale_count: Mapped[int] = mapped_column(default=0)  # how often the PR head moved under us

    # Validator verdict and full evidence blob.
    validation_status: Mapped[str | None]
    validation: Mapped[dict | None] = mapped_column(JSON)
    failure_reason: Mapped[str | None]

    # Timestamps used by the metrics endpoint.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None]
    pr_created_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    next_poll_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # worker backoff


class Event(Base):
    """Append-only timeline shown on the dashboard."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    event_type: Mapped[str]
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Delivery(Base):
    """One row per GitHub webhook delivery ID; lets us answer a redelivery without creating anything."""

    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    mode: Mapped[str]
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    duplicate_count: Mapped[int] = mapped_column(default=0)
