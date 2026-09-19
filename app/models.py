from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TERMINAL = {"VERIFIED", "ESCALATED", "FAILED"}
TRANSITIONS = {
    "QUEUED": {"DEVIN_RUNNING", "FAILED"},
    "DEVIN_RUNNING": {"PR_OPENED", "FAILED", "ESCALATED"},
    "PR_OPENED": {"VALIDATING", "FAILED", "ESCALATED"},
    "VALIDATING": {"VERIFIED", "CORRECTING", "PR_OPENED", "FAILED", "ESCALATED"},
    "CORRECTING": {"PR_OPENED", "FAILED", "ESCALATED"},
}


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("mode", "repository", "issue_number", "generation"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid4().hex)
    mode: Mapped[str]
    case_id: Mapped[str]
    github_delivery_id: Mapped[str]
    repository: Mapped[str]
    issue_number: Mapped[int]
    generation: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="QUEUED", index=True)
    devin_session_id: Mapped[str | None]
    devin_session_url: Mapped[str | None]
    provider_status: Mapped[str | None]
    candidate_pr_number: Mapped[int | None]
    candidate_pr_url: Mapped[str | None]
    candidate_sha: Mapped[str | None]
    validated_sha: Mapped[str | None]
    correction_count: Mapped[int] = mapped_column(default=0)
    launch_requested: Mapped[bool] = mapped_column(default=False)
    correction_requested: Mapped[bool] = mapped_column(default=False)
    correction_acknowledged: Mapped[bool] = mapped_column(default=False)
    api_failures: Mapped[int] = mapped_column(default=0)
    stale_count: Mapped[int] = mapped_column(default=0)
    validation_status: Mapped[str | None]
    validation: Mapped[dict | None] = mapped_column(JSON)
    failure_reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None]
    pr_created_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    next_poll_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    event_type: Mapped[str]
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    mode: Mapped[str]
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    duplicate_count: Mapped[int] = mapped_column(default=0)
