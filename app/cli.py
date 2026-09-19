"""Operator commands (also exposed as Make targets).

    demo               run the credential-free simulation (add --serve to browse its dashboard)
    baseline           prove each case is weak on the real Superset checkout; writes data/live/baselines/
    bootstrap-context  create/reuse the Devin Playbook + Knowledge note; writes data/live/context.json
    doctor             list everything still missing before the live worker may run
"""

import argparse
import json
import shutil

from app.cases import Registry
from app.config import Settings
from app.devin import Devin
from app.simulation import run_demo, simulation_settings
from app.validator import Validator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["demo", "doctor", "bootstrap-context", "baseline"])
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()

    if args.command == "demo":
        settings = simulation_settings()
        if args.reset and settings.storage.exists():
            shutil.rmtree(settings.storage)  # only ever deletes data/simulation/
        print(json.dumps(run_demo(settings), indent=2))
        if args.serve:
            import uvicorn
            from app.main import create_app
            uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)
        return

    settings = Settings()
    registry = Registry(settings)
    if args.command == "baseline":
        results = [Validator(settings).baseline(case) for case in registry.cases.values()]
        # Print the verdicts without the (large) raw pytest evidence; the JSON files keep everything.
        print(json.dumps([{key: value for key, value in proof.items() if key not in {"normal_evidence", "mutant_evidence"}}
                          for proof in results], indent=2))
        raise SystemExit(0 if all(proof["outcome"] == "CONFIRMED" for proof in results) else 1)
    if args.command == "bootstrap-context":
        if not settings.devin_api_key.get_secret_value() or not settings.devin_org_id:
            raise SystemExit("DEVIN_API_KEY and DEVIN_ORG_ID are required")
        print(json.dumps(Devin(settings).bootstrap(registry), indent=2))
        return

    # doctor: config + interpreter + context + baselines. Exit 1 if anything is missing.
    problems = settings.live_errors()
    if not settings.superset_python.is_file():
        problems.append("Prepared Superset Python interpreter is missing")
    if not (settings.storage / "context.json").is_file():
        problems.append("Run make bootstrap-context after confirming baselines")
    if set(settings.case_issues) != set(registry.cases):
        problems.append("Bind both approved case IDs in CASE_ISSUES")
    for case in registry.cases.values():
        try:
            registry.evidence(case)
        except (ValueError, OSError) as exc:
            problems.append(str(exc))
    print(json.dumps({"mode": settings.mode, "ready": not problems, "problems": problems}, indent=2))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
