import fcntl
import json
import logging
import time

from app.cases import load_cases
from app.config import Settings
from app.db import Store
from app.devin import Devin
from app.github import GitHub
from app.models import TERMINAL
from app.orchestrator import Orchestrator
from app.validator import Validator


def main() -> None:
    settings = Settings()
    if settings.mode == "SIMULATION":
        print("Simulation uses in-process fake adapters; no live worker started.")
        return
    settings.require_live()
    store = Store(settings.database_path)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with settings.database_path.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another worker owns this database") from None
        worker = Orchestrator(settings, store, load_cases(), Devin(settings), GitHub(settings), Validator(settings))
        while True:
            for job in reversed(store.jobs(settings.mode)):
                if job.status in TERMINAL:
                    continue
                try:
                    worker.tick(job.id)
                except Exception as error:
                    logging.error(json.dumps({"job_id": job.id, "event": "WORKER_ERROR", "error_type": type(error).__name__}))
                    current = store.get(job.id)
                    if current.status not in TERMINAL:
                        worker.fail(current, "Unexpected worker error; inspect local diagnostics before retrying")
                updated = store.get(job.id)
                if updated.status != job.status:
                    logging.info(json.dumps({"job_id": job.id, "from": job.status, "to": updated.status}))
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
