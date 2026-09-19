from pathlib import Path
from statistics import median
from uuid import uuid4

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from app.models import Base, Delivery, Event, Job, TERMINAL, TRANSITIONS, now


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 15})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _record):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def enqueue(self, delivery_id: str, mode: str, repository: str, issue: int, case_id: str) -> tuple[str, bool]:
        with self.session.begin() as db:
            added = db.execute(insert(Delivery).values(id=delivery_id, mode=mode).on_conflict_do_nothing())
            if not added.rowcount:
                db.execute(update(Delivery).where(Delivery.id == delivery_id).values(duplicates=Delivery.duplicates + 1))
                job = db.scalar(select(Job).where(Job.github_delivery_id == delivery_id))
                return job.id if job else "", True
            created = db.execute(insert(Job).values(
                id=uuid4().hex, mode=mode, repository=repository,
                issue_number=issue, case_id=case_id, github_delivery_id=delivery_id,
            ).on_conflict_do_nothing())
            job = db.scalar(select(Job).where(Job.mode == mode, Job.repository == repository, Job.issue_number == issue))
            if created.rowcount:
                db.add(Event(job_id=job.id, event_type="QUEUED", details={"delivery_id": delivery_id}))
            else:
                db.add(Event(job_id=job.id, event_type="LOGICAL_DUPLICATE", details={"delivery_id": delivery_id}))
            return job.id, not bool(created.rowcount)

    def get(self, job_id: str) -> Job:
        with self.session() as db:
            job = db.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            return job

    def jobs(self, mode: str) -> list[Job]:
        with self.session() as db:
            return list(db.scalars(select(Job).where(Job.mode == mode).order_by(Job.created_at.desc())))

    def events(self, job_id: str) -> list[Event]:
        with self.session() as db:
            return list(db.scalars(select(Event).where(Event.job_id == job_id).order_by(Event.id)))

    def change(self, job_id: str, event_type: str, *, status: str | None = None, details: dict | None = None, **fields) -> Job:
        with self.session.begin() as db:
            job = db.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if status and status != job.status:
                if status not in TRANSITIONS.get(job.status, set()):
                    raise ValueError(f"Invalid transition: {job.status} -> {status}")
                job.status = status
                if status == "DEVIN_RUNNING":
                    job.started_at = now()
                if status == "PR_OPENED" and job.pr_created_at is None:
                    job.pr_created_at = now()
                if status in TERMINAL:
                    job.completed_at = now()
            for key, value in fields.items():
                setattr(job, key, value)
            db.add(Event(job_id=job_id, event_type=event_type, details=details or {}))
            return job

    def metrics(self, mode: str) -> dict:
        jobs = self.jobs(mode)
        evaluated = [job for job in jobs if job.validation_status is not None]
        first_pass = [job for job in evaluated if job.status == "VERIFIED" and not job.correction_count]
        corrected = [job for job in jobs if job.correction_count]
        completed_corrections = [job for job in corrected if job.status in TERMINAL]
        recovered = [job for job in completed_corrections if job.status == "VERIFIED"]
        with self.session() as db:
            duplicates = sum(db.scalars(select(Delivery.duplicates).where(Delivery.mode == mode)))
        pr_latencies = [job.pr_created_at - job.created_at for job in jobs if job.pr_created_at is not None]
        verified_latencies = [job.completed_at - job.created_at for job in jobs if job.status == "VERIFIED"]
        return {
            "attempted": len(jobs), "active": sum(job.status not in TERMINAL for job in jobs),
            "verified": sum(job.status == "VERIFIED" for job in jobs),
            "failed": sum(job.status in {"FAILED", "ESCALATED"} for job in jobs),
            "first_pass": [len(first_pass), len(evaluated)],
            "recovery": [len(recovered), len(completed_corrections)],
            "event_to_pr": median(pr_latencies) if pr_latencies else None,
            "event_to_verified": median(verified_latencies) if verified_latencies else None,
            "duplicates": duplicates,
        }
