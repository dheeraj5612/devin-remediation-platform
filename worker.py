"""Worker process: the only thing that spends money (creates Devin sessions) or runs candidate code.

ELI5: a loop that every few seconds asks the database "which jobs are due?"
and calls `Orchestrator.step` on each. It refuses to start unless live config
is complete and every case has baseline proof, and a file lock guarantees at
most one worker per database (two workers could double-launch Devin).
"""

import fcntl  # Lock the live database so only one worker can spend provider credits.
import logging  # Report lifecycle events in a format operators can collect.
import time  # Wait between polling passes.

from app.cases import Registry  # Load approved cases and their baseline evidence.
from app.config import Settings  # Read the live-mode safety controls.
from app.db import Store  # Open the durable job and event store.
from app.devin import Devin  # Create or resume the provider session.
from app.github import GitHub  # Discover the candidate pull request.
from app.orchestrator import Orchestrator  # Advance each durable job one safe step.
from app.validator import Validator  # Run independent candidate validation.


def main() -> None:
    """Start the single live worker after every spending and proof gate passes.

    The worker reads its input from the configured SQLite store and provider clients.
    It runs until interrupted; startup failures raise ``SystemExit`` before any session
    can be created, and a file lock prevents two workers from sharing one database.
    """

    logging.basicConfig(level=logging.INFO, format="%(message)s")  # Keep worker logs compact and machine-readable.
    settings = Settings()  # Load environment and `.env` values through the typed settings object.
    if settings.mode != "LIVE":
        raise SystemExit("Use make demo for simulation")  # Keep simulation traffic out of the live worker.
    problems = settings.live_errors()  # Collect all missing credentials and safety confirmations first.
    if problems:
        raise SystemExit("Live worker disabled: " + ", ".join(problems))  # Fail closed before provider use.
    registry = Registry(settings)  # Load only the cases declared in the trusted case registry.
    for case in registry.cases.values():
        registry.evidence(case)  # Refuse to run if any baseline proof is missing or stale.
    store = Store(settings)  # Open the mode-isolated durable state database.
    with (settings.storage / "worker.lock").open("w") as lock:  # Hold the process-wide worker lock.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Acquire without waiting behind another worker.
        except BlockingIOError:
            raise SystemExit("Another worker already owns this database") from None  # Avoid duplicate launches.
        orchestrator = Orchestrator(  # Wire durable state, provider clients, and the independent validator.
            settings, store, Devin(settings), GitHub(settings), Validator(settings)
        )
        orchestrator.resume()  # Resume saved sessions without recreating provider work.
        try:
            while True:
                for job in store.jobs(active_only=True):  # Select only nonterminal jobs whose backoff expired.
                    orchestrator.step(job.id)  # Advance one job and persist its next durable state.
                time.sleep(settings.poll_seconds)  # Leave provider and database capacity between passes.
        except KeyboardInterrupt:
            return  # Let a normal operator interrupt stop the loop cleanly.


if __name__ == "__main__":
    main()  # Run the worker when invoked as `python worker.py`.
