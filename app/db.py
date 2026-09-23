"""`Store`: the only place that reads or writes SQLite.

ELI5: the worker and the web server are separate processes. They never talk
to each other directly; they both talk to this database. Every write here is
a short transaction, so a crash between two steps leaves a consistent record
the worker can resume from.
"""

import json  # Serialize event details for structured logs.
import logging  # Keep database events visible without printing secrets.
from datetime import timedelta  # Calculate future polling times.
from sqlite3 import Connection  # Type the SQLite connection used by SQLAlchemy hooks.
from typing import Any  # Accept provider-specific event detail values.

from sqlalchemy import create_engine, event, select, text  # Build and query the durable SQLite store.
from sqlalchemy.orm import Session, sessionmaker  # Type sessions and create short-lived ones.

from app.config import Settings
from app.models import Base, Delivery, Event, Job, TERMINAL, TRANSITIONS, now

logger = logging.getLogger("remediation")  # ELI5: send database events to the app's shared operator log.


class Store:
    """Persist jobs, webhook deliveries, and their append-only event timelines."""

    def __init__(self, settings: Settings) -> None:
        """Create a mode-isolated engine and configure SQLite for one worker plus readers."""

        settings.storage.mkdir(parents=True, exist_ok=True)  # Make the selected evidence directory first.
        self.mode = settings.mode  # every query is filtered by mode; LIVE never sees SIMULATION rows
        self.engine = create_engine(  # Open the configured database without sharing connections across threads.
            settings.database_url,
            connect_args={"timeout": 10, "check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection: Connection, _: Any) -> None:
            """Set SQLite connection safeguards once whenever SQLAlchemy opens a connection."""

            connection.execute("PRAGMA foreign_keys=ON")  # Reject events that reference missing jobs.
            connection.execute("PRAGMA busy_timeout=10000")  # Wait instead of failing during a worker lock.
            connection.execute("PRAGMA journal_mode=WAL")  # Let dashboard reads overlap worker writes.

        Base.metadata.create_all(self.engine)  # Create missing tables without rewriting existing evidence.
        self.session = sessionmaker(self.engine, expire_on_commit=False)  # Keep returned rows usable after commit.

    def enqueue(self, delivery_id: str, repository: str, issue: int, case_id: str) -> tuple[Job, bool]:
        """Turn an accepted webhook into a job. Returns (job, was_duplicate).

        Two kinds of duplicate are handled:
        * GitHub re-sent the *same* delivery ID  -> DUPLICATE_DELIVERY, return the existing job.
        * A *different* delivery for the same issue -> DUPLICATE_EXECUTION, still return the existing job.
        Either way exactly one job (and one paid Devin session) exists per issue.
        """
        # ELI5: keep the duplicate check and possible insert in one short transaction.
        with self.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))  # Lock before checking so duplicate delivery handling is atomic.
            delivery = session.get(Delivery, delivery_id)  # Look for an exact GitHub redelivery first.
            if delivery:
                delivery.duplicate_count += 1  # Preserve how often GitHub retried the same delivery.
                job = session.get(Job, delivery.job_id)  # Reuse the original job rather than creating paid work.
                self._event(session, job, "DUPLICATE_DELIVERY")  # Make the duplicate visible on the timeline.
                session.commit()  # Finish the short transaction before returning the existing job.
                return job, True  # Tell the caller this request did not create new work.
            job = session.scalar(  # Find an existing job for the same issue in this mode.
                select(Job).where(
                    Job.mode == self.mode,
                    Job.repository == repository,
                    Job.issue_number == issue,
                    Job.generation == 1,
                )
            )
            duplicate = job is not None  # A different delivery can still refer to the same issue.
            if job is None:
                job = Job(  # Store the admission decision before the worker spends provider credits.
                    mode=self.mode,
                    repository=repository,
                    issue_number=issue,
                    case_id=case_id,
                    github_delivery_id=delivery_id,
                )
                session.add(job)  # Add the new job to the current transaction.
                session.flush()  # Assign job.id so the first event can reference it.
                self._event(session, job, "QUEUED")  # Record the durable handoff to the worker.
            else:
                self._event(session, job, "DUPLICATE_EXECUTION")  # Explain why this delivery did not enqueue again.
            session.add(Delivery(id=delivery_id, mode=self.mode, job_id=job.id))  # Remember every delivery ID.
            session.commit()  # Make the job and delivery visible together.
            return job, duplicate  # Return the durable row and whether it already existed.

    def get(self, job_id: str) -> Job:
        """Load one job from this mode or raise when it is missing or belongs to another mode."""

        # ELI5: read one job through a short session, then return its non-expiring row.
        with self.session() as session:
            job = session.get(Job, job_id)  # Fetch by primary key without scanning other jobs.
            if job is None or job.mode != self.mode:
                raise KeyError(job_id)  # Keep simulation and live records isolated at the API boundary.
            return job  # The session does not expire returned objects after it closes.

    def jobs(self, active_only: bool = False) -> list[Job]:
        """Return this mode's jobs, optionally limited to unfinished jobs due for polling."""

        query = select(Job).where(Job.mode == self.mode)  # Never mix live and simulation dashboards.
        if active_only:
            query = query.where(  # The worker only needs jobs that can act and are not delayed.
                Job.status.not_in(TERMINAL),
                Job.next_poll_at <= now(),
            )
        # ELI5: read the filtered job list without holding the session after return.
        with self.session() as session:
            return list(session.scalars(query.order_by(Job.created_at)))  # Process oldest due work first.

    def events(self, job_id: str | None = None) -> list[Event]:
        """Return this mode's event timeline, optionally narrowed to one job."""

        query = select(Event).join(Job).where(Job.mode == self.mode)  # Filter through the owning job.
        if job_id is not None:
            # ELI5: an empty ID is still a requested filter, so it must not reveal every event.
            query = query.where(Event.job_id == job_id)  # Avoid exposing another job's timeline.
        # ELI5: read the ordered timeline through the same mode filter as jobs.
        with self.session() as session:
            return list(session.scalars(query.order_by(Event.id)))  # IDs preserve append order.

    def change(self, job_id: str, event_type: str, *, status: str | None = None,
               details: dict | None = None, **values: Any) -> Job:
        """Record an event and (optionally) move the job to a new status, in one transaction.

        `values` are plain column updates (e.g. devin_session_id=...). A status change is
        checked against TRANSITIONS, so an impossible jump raises instead of corrupting state.
        """
        # ELI5: commit status, field updates, and the matching event together.
        with self.session.begin() as session:
            job = session.get(Job, job_id)  # Load the row inside the same transaction as every update.
            if job is None or job.mode != self.mode:
                raise KeyError(job_id)  # Refuse cross-mode or unknown updates.
            if status is not None and status != job.status:
                # ELI5: reject a status jump that is not allowed from the current state.
                if status not in TRANSITIONS.get(job.status, set()):
                    raise ValueError(f"Invalid transition: {job.status} -> {status}")  # Keep the state machine closed.
                job.status = status  # Move only after the transition has been approved.
                if status in TERMINAL:
                    job.completed_at = now()  # Terminal jobs receive one completion timestamp.
            for key, value in values.items():
                setattr(job, key, value)  # Apply ordinary column updates requested by the orchestrator.
            self._event(session, job, event_type, details)  # Record the event beside the state change.
        return job  # Return the committed object for callers that need its current values.

    def defer(self, job_id: str, seconds: float) -> None:
        """Set the next polling time for a job without changing its state."""

        # ELI5: commit the new polling deadline as one small update.
        with self.session.begin() as session:
            job = session.get(Job, job_id)  # The caller only defers jobs it already claimed.
            if job is None or job.mode != self.mode:
                # ELI5: fail closed instead of turning an unknown ID into an AttributeError.
                raise KeyError(job_id)
            job.next_poll_at = now() + timedelta(seconds=seconds)  # Delay polling or retry backoff.

    def record_infra_revalidation(self, job_id: str, sha: str, verdict: dict) -> Job:
        """Record an operator's exact-SHA retry of an infrastructure-only validation failure."""
        # ELI5: make the retry and its audit event one transaction, so a crash cannot show half a verdict.
        with self.session.begin() as session:
            job = session.get(Job, job_id)  # ELI5: inspect the saved attempt while holding the write transaction.
            if (job is None or self.mode != "LIVE" or job.mode != "LIVE" or job.status != "FAILED"
                    or job.validation_status != "INFRA_ERROR" or job.candidate_sha != sha):
                raise ValueError("Only the same failed live candidate can be revalidated")
            outcome = verdict.get("outcome")  # ELI5: trust only a known validator outcome shape.
            if outcome not in {"VERIFIED", "INFRA_ERROR", "NORMAL_FAILED", "REGRESSION_SURVIVED", "APPLICATION_FAILED",
                               "INVALID_CONTROL", "NOT_VERIFIED", "SCOPE_REJECTED", "STALE_SHA"}:
                raise ValueError("Unknown revalidation outcome")
            if outcome == "VERIFIED" and verdict.get("evidence", {}).get("sha") != sha:
                raise ValueError("Verified evidence must name the exact candidate SHA")
            job.validation_status = outcome  # ELI5: keep the retried verdict even when it still fails.
            job.validation = verdict  # ELI5: preserve the validator's detailed evidence for review.
            job.validated_sha = sha if outcome == "VERIFIED" else None  # ELI5: only success earns a validated SHA.
            job.failure_reason = None if outcome == "VERIFIED" else str(verdict.get("summary", "Validation failed"))[:200]
            if outcome == "VERIFIED":
                job.status = "VERIFIED"  # ELI5: the operator retry can repair an infrastructure-only false failure.
                job.completed_at = now()  # ELI5: timestamp the final trusted verdict, not the earlier failed attempt.
            self._event(session, job, "INFRA_REVALIDATED", {"outcome": outcome, "sha": sha})
        return job

    @staticmethod
    def _event(session: Session, job: Job, event_type: str, details: dict | None = None) -> None:
        """Append an event row and emit a small structured log line for operators."""

        session.add(Event(job_id=job.id, event_type=event_type, details=details or {}))  # Keep full details in SQLite.
        logger.info(json.dumps({"job_id": job.id, "event": event_type, "mode": job.mode}))  # Log identifiers only.
