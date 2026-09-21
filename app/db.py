"""`Store`: the only place that reads or writes SQLite.

ELI5: the worker and the web server are separate processes. They never talk
to each other directly; they both talk to this database. Every write here is
a short transaction, so a crash between two steps leaves a consistent record
the worker can resume from.
"""

import json
import logging
from datetime import timedelta
from sqlite3 import Connection
from typing import Any

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Base, Delivery, Event, Job, TERMINAL, TRANSITIONS, now

logger = logging.getLogger("remediation")


class Store:
    def __init__(self, settings: Settings) -> None:
        settings.storage.mkdir(parents=True, exist_ok=True)
        self.mode = settings.mode  # every query is filtered by mode; LIVE never sees SIMULATION rows
        self.engine = create_engine(settings.database_url, connect_args={"timeout": 10, "check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def configure(connection: Connection, _: Any) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")  # wait instead of erroring when the worker holds a lock
            connection.execute("PRAGMA journal_mode=WAL")  # readers (dashboard) don't block the writer (worker)

        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def enqueue(self, delivery_id: str, repository: str, issue: int, case_id: str) -> tuple[Job, bool]:
        """Turn an accepted webhook into a job. Returns (job, was_duplicate).

        Two kinds of duplicate are handled:
        * GitHub re-sent the *same* delivery ID  -> DUPLICATE_DELIVERY, return the existing job.
        * A *different* delivery for the same issue -> DUPLICATE_EXECUTION, still return the existing job.
        Either way exactly one job (and one paid Devin session) exists per issue.
        """
        with self.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))  # take the write lock now: check-then-insert must be atomic
            delivery = session.get(Delivery, delivery_id)
            if delivery:
                delivery.duplicate_count += 1
                job = session.get(Job, delivery.job_id)
                self._event(session, job, "DUPLICATE_DELIVERY")
                session.commit()
                return job, True
            job = session.scalar(select(Job).where(
                Job.mode == self.mode, Job.repository == repository, Job.issue_number == issue, Job.generation == 1,
            ))
            duplicate = job is not None
            if job is None:
                job = Job(mode=self.mode, repository=repository, issue_number=issue, case_id=case_id,
                          github_delivery_id=delivery_id)
                session.add(job)
                session.flush()  # assigns job.id so the event row can reference it
                self._event(session, job, "QUEUED")
            else:
                self._event(session, job, "DUPLICATE_EXECUTION")
            session.add(Delivery(id=delivery_id, mode=self.mode, job_id=job.id))
            session.commit()
            return job, duplicate

    def get(self, job_id: str) -> Job:
        with self.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.mode != self.mode:
                raise KeyError(job_id)
            return job

    def jobs(self, active_only: bool = False) -> list[Job]:
        """All jobs in this mode; `active_only` = not finished and due for another poll."""
        query = select(Job).where(Job.mode == self.mode)
        if active_only:
            query = query.where(Job.status.not_in(TERMINAL), Job.next_poll_at <= now())
        with self.session() as session:
            return list(session.scalars(query.order_by(Job.created_at)))

    def events(self, job_id: str | None = None) -> list[Event]:
        query = select(Event).join(Job).where(Job.mode == self.mode)
        if job_id:
            query = query.where(Event.job_id == job_id)
        with self.session() as session:
            return list(session.scalars(query.order_by(Event.id)))

    def change(self, job_id: str, event_type: str, *, status: str | None = None,
               details: dict | None = None, **values: Any) -> Job:
        """Record an event and (optionally) move the job to a new status, in one transaction.

        `values` are plain column updates (e.g. devin_session_id=...). A status change is
        checked against TRANSITIONS, so an impossible jump raises instead of corrupting state.
        """
        with self.session.begin() as session:
            job = session.get(Job, job_id)
            if job is None or job.mode != self.mode:
                raise KeyError(job_id)
            if status is not None and status != job.status:
                if status not in TRANSITIONS.get(job.status, set()):
                    raise ValueError(f"Invalid transition: {job.status} -> {status}")
                job.status = status
                if status in TERMINAL:
                    job.completed_at = now()
            for key, value in values.items():
                setattr(job, key, value)
            self._event(session, job, event_type, details)
        return job

    def defer(self, job_id: str, seconds: float) -> None:
        """Don't look at this job again for `seconds` (polling interval or retry backoff)."""
        with self.session.begin() as session:
            job = session.get(Job, job_id)
            job.next_poll_at = now() + timedelta(seconds=seconds)

    @staticmethod
    def _event(session: Session, job: Job, event_type: str, details: dict | None = None) -> None:
        session.add(Event(job_id=job.id, event_type=event_type, details=details or {}))
        logger.info(json.dumps({"job_id": job.id, "event": event_type, "mode": job.mode}))  # one JSON line per event
