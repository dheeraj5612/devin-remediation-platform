"""Isolated browser-test fixtures. Never use a user's .env or live provider adapters."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from app.main import create_app
from app.simulation import run_demo, simulation_settings


def main() -> None:
    """Serve one deliberately named test scenario on loopback only."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--scenario", choices=("demo", "empty", "live-empty", "storage-error", "queued", "large", "unlinked"),
                        default="demo")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="proofline-review-") as directory:
        settings = simulation_settings(Path(directory))
        if args.scenario in {"demo", "unlinked"}:
            run_demo(settings)
        if args.scenario == "live-empty":
            # Empty LIVE-shaped storage, with spending disabled and every secret blank.
            settings = settings.model_copy(update={"mode": "LIVE", "github_webhook_secret": SecretStr("")})
        application = create_app(settings)
        store = application.state.store
        if args.scenario == "unlinked":
            job = next(job for job in store.jobs() if job.issue_number == 105)
            store.change(job.id, "REVIEW_FIXTURE", validated_sha="0" * 40)
        if args.scenario in {"queued", "large"}:
            count = 45 if args.scenario == "large" else 1
            for index in range(count):
                store.enqueue(f"ui-fixture-{index}", "demo/superset", 700 + index, "import-unparseable-yaml")
        if args.scenario == "storage-error":
            def unavailable(*_args, **_kwargs):
                """Raise a private database error so the UI can exercise its 503 boundary."""

                # ELI5: make every read fail without exposing the simulated SQL details to the page.
                raise OperationalError("test-only read failure", {}, Exception("not public"))

            store.jobs = unavailable
        uvicorn.run(application, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
