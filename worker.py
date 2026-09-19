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
        registry.evidence(case)
    store = Store(settings)
    with (settings.storage / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another worker already owns this database") from None
        orchestrator = Orchestrator(settings, store, Devin(settings), GitHub(settings), Validator(settings))
        orchestrator.resume()
        try:
            while True:
                for job in store.jobs(active_only=True):
                    orchestrator.step(job.id)
                time.sleep(settings.poll_seconds)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
