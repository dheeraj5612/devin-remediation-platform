"""Worker process: the only thing that spends money (creates Devin sessions) or runs candidate code.

ELI5: a loop that every few seconds asks the database "which jobs are due?"
and calls `Orchestrator.step` on each. It refuses to start unless live config
is complete and every case has baseline proof, and a file lock guarantees at
most one worker per database (two workers could double-launch Devin).
"""

import fcntl
import logging
import time

from app.cases import Registry
from app.config import Settings
from app.db import Store
from app.devin import Devin
from app.github import GitHub
from app.orchestrator import Orchestrator
from app.validator import Validator


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    if settings.mode != "LIVE":
        raise SystemExit("Use make demo for simulation")
    problems = settings.live_errors()
    if problems:
        raise SystemExit("Live worker disabled: " + ", ".join(problems))
    registry = Registry(settings)
    for case in registry.cases.values():
        registry.evidence(case)  # raises if any baseline proof is missing or stale
    store = Store(settings)
    with (settings.storage / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another worker already owns this database") from None
        orchestrator = Orchestrator(settings, store, Devin(settings), GitHub(settings), Validator(settings))
        orchestrator.resume()  # pick up where a previous worker stopped; never recreates sessions
        try:
            while True:
                for job in store.jobs(active_only=True):
                    orchestrator.step(job.id)
                time.sleep(settings.poll_seconds)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
