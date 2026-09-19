from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TERMINAL = {"VERIFIED", "ESCALATED", "FAILED"}
TRANSITIONS = {
    "QUEUED": {"DEVIN_RUNNING", "FAILED"},
    "DEVIN_RUNNING": {"PR_OPENED", "FAILED"},
    "PR_OPENED": {"VALIDATING", "FAILED"},
    "VALIDATING": {"VERIFIED", "CORRECTING", "ESCALATED", "FAILED"},
    "CORRECTING": {"PR_OPENED", "ESCALATED", "FAILED"},
}


def now() -> float:
    return datetime.now(UTC).timestamp()


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("mode", "repository", "issue_number", "generation"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid4().hex)
    case_id: Mapped[str]
    mode: Mapped[str]
    github_delivery_id: Mapped[str] = mapped_column(unique=True)
    repository: Mapped[str]
    issue_number: Mapped[int]
    generation: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="QUEUED", index=True)
    devin_session_id: Mapped[str | None]
    devin_session_url: Mapped[str | None]
    candidate_pr_number: Mapped[int | None]
    candidate_pr_url: Mapped[str | None]
    candidate_sha: Mapped[str | None]
    correction_count: Mapped[int] = mapped_column(default=0)
    correction_sent: Mapped[bool] = mapped_column(default=False)
    launch_intent: Mapped[bool] = mapped_column(default=False)
    validation_status: Mapped[str | None]
    failure_reason: Mapped[str | None]
    transient_failures: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    started_at: Mapped[float | None] = mapped_column(Float)
    pr_created_at: Mapped[float | None] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float)
    next_poll_at: Mapped[float] = mapped_column(Float, default=0)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str]
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[float] = mapped_column(Float, default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    event_type: Mapped[str]
    timestamp: Mapped[float] = mapped_column(Float, default=now)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
