"""Database tables and the job state machine.

ELI5: a `Job` is one attempt to fix one weak test. It moves through a fixed
list of statuses (below). `Event` rows are the job's diary; `Delivery` rows
remember every webhook GitHub sent us so a re-sent webhook is not a new job.

    QUEUED -> DEVIN_RUNNING -> PR_OPENED -> VALIDATING -> VERIFIED
                                   ^            |
                                   +- CORRECTING (one retry, same session)
    Any non-terminal state can end in FAILED (our problem) or ESCALATED (a human must look).
"""

from datetime import UTC, datetime  # Create consistent UTC timestamps for every evidence row.
from uuid import uuid4  # Give jobs stable identifiers before provider work starts.

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint  # Describe SQLite tables.
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column  # Map typed Python fields to columns.

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
    """Return a timezone-free UTC value that SQLite can store consistently."""

    # Naive UTC everywhere; SQLite has no timezone type.
    return datetime.now(UTC).replace(tzinfo=None)  # Strip the marker only after choosing UTC.


class Base(DeclarativeBase):
    """Shared SQLAlchemy base that collects all tables for ``create_all``."""

    pass


class Job(Base):
    """One durable remediation attempt and the evidence needed to resume it safely."""

    __tablename__ = "jobs"  # Store one row per issue-level remediation attempt.
    # One live job per issue: the same issue labelled twice must not start two Devin sessions.
    __table_args__ = (  # Keep the idempotency rule in the database as a final safety net.
        UniqueConstraint("mode", "repository", "issue_number", "generation"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid4().hex)  # Public job key.
    mode: Mapped[str]  # LIVE or SIMULATION, so demo rows never mix with real ones.
    case_id: Mapped[str]  # Approved weak test from evals/cases.yaml.
    github_delivery_id: Mapped[str]  # GitHub's idempotency key for the triggering webhook.
    repository: Mapped[str]  # Repository name checked by the webhook gate.
    issue_number: Mapped[int]  # Issue that a human explicitly labelled for remediation.
    generation: Mapped[int] = mapped_column(default=1)  # Future retry generations remain distinct.
    status: Mapped[str] = mapped_column(default="QUEUED", index=True)  # Current state-machine state.

    # What Devin is doing.
    devin_session_id: Mapped[str | None]  # Provider session identifier, when launch succeeds.
    devin_session_url: Mapped[str | None]  # Human-readable provider link, when supplied.
    provider_status: Mapped[str | None]  # Last seen status/status_detail from the Devin API.

    # The PR Devin opened and which commit of it we judged.
    candidate_pr_number: Mapped[int | None]  # Pull request number discovered from the session.
    candidate_pr_url: Mapped[str | None]  # Pull request link for the dashboard.
    candidate_sha: Mapped[str | None]  # Exact candidate commit observed by the worker.
    validated_sha: Mapped[str | None]  # Only set on VERIFIED; the exact commit that passed.

    # Bookkeeping flags that make retries safe (see Orchestrator for how each is used).
    correction_count: Mapped[int] = mapped_column(default=0)  # Maximum one correction per session.
    launch_requested: Mapped[bool] = mapped_column(default=False)  # Create-session request was sent.
    correction_requested: Mapped[bool] = mapped_column(default=False)  # Correction message was sent.
    correction_acknowledged: Mapped[bool] = mapped_column(default=False)  # Provider accepted that message.
    api_failures: Mapped[int] = mapped_column(default=0)  # Consecutive provider errors.
    stale_count: Mapped[int] = mapped_column(default=0)  # PR-head changes observed while validating.

    # Validator verdict and full evidence blob.
    validation_status: Mapped[str | None]  # PASS/FAIL/INFRA_ERROR style evaluator result.
    validation: Mapped[dict | None] = mapped_column(JSON)  # Full redacted evaluator evidence.
    failure_reason: Mapped[str | None]  # Human-readable reason for a non-verified outcome.

    # Timestamps used by the metrics endpoint.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # Admission time.
    started_at: Mapped[datetime | None]  # First provider session start time.
    pr_created_at: Mapped[datetime | None]  # First observed candidate PR time.
    completed_at: Mapped[datetime | None]  # Terminal verdict time.
    next_poll_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # Worker backoff deadline.


class Event(Base):
    """Append-only timeline entry shown on the dashboard and report."""

    __tablename__ = "events"  # Keep every state and provider observation in order.

    id: Mapped[int] = mapped_column(primary_key=True)  # Monotonic local ordering key.
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)  # Owning job.
    event_type: Mapped[str]  # Short timeline label used by metrics and the UI.
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now)  # Event creation time.
    details: Mapped[dict] = mapped_column(JSON, default=dict)  # Redacted structured context.


class Delivery(Base):
    """One row per webhook delivery ID, so redelivery does not create new work."""

    __tablename__ = "deliveries"  # Store webhook receipts separately from job state.

    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # GitHub delivery identifier.
    mode: Mapped[str]  # Keep simulation and live delivery ledgers separate.
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))  # Job created by the first receipt.
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # First receipt time.
    duplicate_count: Mapped[int] = mapped_column(default=0)  # Number of retries for this delivery.
