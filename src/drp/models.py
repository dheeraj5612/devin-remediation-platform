"""SQLAlchemy models: findings, baselines, durable remediation jobs, validation evidence."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class TZDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo on read)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class BaselineStatus(enum.StrEnum):
    UNREGISTERED = "unregistered"  # finding loaded, baseline not yet run
    CONFIRMED = "confirmed"  # original test passes on clean AND on mutant -> weak test proven
    REFUTED = "refuted"  # original test already catches the mutant -> not a finding
    ERROR = "error"  # baseline could not be established (infra / patch does not apply)


class JobState(enum.StrEnum):
    QUEUED = "queued"
    SESSION_REQUESTED = "session_requested"
    SESSION_RUNNING = "session_running"
    PR_READY = "pr_ready"
    VALIDATING = "validating"
    VERIFIED = "verified"
    REJECTED = "rejected"
    ESCALATED = "escalated"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset({JobState.VERIFIED, JobState.REJECTED, JobState.ESCALATED})

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.SESSION_REQUESTED, JobState.ESCALATED}),
    JobState.SESSION_REQUESTED: frozenset(
        {JobState.SESSION_RUNNING, JobState.QUEUED, JobState.ESCALATED}
    ),
    JobState.SESSION_RUNNING: frozenset({JobState.PR_READY, JobState.ESCALATED}),
    JobState.PR_READY: frozenset({JobState.VALIDATING, JobState.ESCALATED}),
    JobState.VALIDATING: frozenset(
        {
            JobState.VERIFIED,
            JobState.REJECTED,
            JobState.ESCALATED,
            JobState.SESSION_RUNNING,  # feedback loop: another attempt in the same session
            JobState.PR_READY,  # transient validator failure -> retry validation
        }
    ),
    JobState.VERIFIED: frozenset(),
    JobState.REJECTED: frozenset(),
    JobState.ESCALATED: frozenset(),
}


class Verdict(enum.StrEnum):
    VERIFIED = "verified"  # clean pass + mutant detected
    REJECTED = "rejected"  # deterministic negative evidence (clean fail or mutant escaped)
    ESCALATED = "escalated"  # scope violation, mutant no longer applies, ambiguous
    INFRA_ERROR = "infra_error"  # validator itself failed; retryable


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    repo: Mapped[str] = mapped_column(String(128))
    source_sha: Mapped[str] = mapped_column(String(64))
    test_file: Mapped[str] = mapped_column(String(512))
    test_node_id: Mapped[str] = mapped_column(String(512))
    weakness_kind: Mapped[str] = mapped_column(String(64))
    weakness_description: Mapped[str] = mapped_column(Text)
    protected_behavior: Mapped[str] = mapped_column(Text)
    test_command: Mapped[list[str]] = mapped_column(JSON)
    allowed_paths: Mapped[list[str]] = mapped_column(JSON)
    mutant_path: Mapped[str] = mapped_column(String(512))
    mutant_sha256: Mapped[str] = mapped_column(String(64))
    mutant_description: Mapped[str] = mapped_column(Text)
    baseline_status: Mapped[BaselineStatus] = mapped_column(
        Enum(BaselineStatus), default=BaselineStatus.UNREGISTERED
    )
    baseline_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_issue_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)

    jobs: Mapped[list[RemediationJob]] = relationship(back_populates="finding")


class RemediationJob(Base):
    __tablename__ = "remediation_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), index=True)
    issue_number: Mapped[int] = mapped_column(Integer)
    trigger_delivery_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.QUEUED, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    devin_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    devin_session_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)
    session_created_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    pr_ready_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    finding: Mapped[Finding] = relationship(back_populates="jobs")
    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job", order_by="JobEvent.created_at", cascade="all, delete-orphan"
    )
    validations: Mapped[list[ValidationRun]] = relationship(
        back_populates="job", order_by="ValidationRun.started_at", cascade="all, delete-orphan"
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("remediation_jobs.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    job: Mapped[RemediationJob] = relationship(back_populates="events")


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("remediation_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    pr_head_sha: Mapped[str] = mapped_column(String(64))
    mutant_sha256: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[Verdict] = mapped_column(Enum(Verdict))
    scope_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    target_test_present: Mapped[bool] = mapped_column(Boolean, default=False)
    clean_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mutant_detected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifacts_dir: Mapped[str] = mapped_column(String(512), default="")
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    job: Mapped[RemediationJob] = relationship(back_populates="validations")


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(256))
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
