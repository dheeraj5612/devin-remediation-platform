"""Operator commands (also exposed as Make targets).

    demo               run the credential-free simulation (add --serve to browse its dashboard)
    baseline           prove each case is weak on the real Superset checkout; writes data/live/baselines/
    bootstrap-context  create/reuse the Devin Playbook + Knowledge note; writes data/live/context.json
    doctor             list everything still missing before the live worker may run
"""

import argparse  # Turn command-line words into typed options.
import json  # Print machine-readable command results.
import shutil  # Remove only the simulation folder when asked.

from app.cases import Registry  # Load the approved remediation cases.
from app.config import Settings  # Load environment-backed live settings.
from app.devin import Devin  # Call Devin only after explicit credential checks.
from app.simulation import run_demo, simulation_settings  # Run the safe local demo.
from app.validator import Validator  # Prove a case is weak or validate its candidate.


def main() -> None:
    """Run one operator command and exit with a status suitable for automation.

    Inputs are the command name plus optional demo flags. The commands print JSON;
    ``baseline`` and ``doctor`` raise ``SystemExit(1)`` when their checks fail.
    """

    parser = argparse.ArgumentParser()  # Describe the small operator-facing command set.
    parser.add_argument(  # Keep unsupported commands from reaching live code.
        "command", choices=["demo", "doctor", "bootstrap-context", "baseline"]
    )
    parser.add_argument("--reset", action="store_true")  # Allow a clean simulation rerun.
    parser.add_argument("--serve", action="store_true")  # Open the demo dashboard after seeding it.
    args = parser.parse_args()  # Read the actual command line supplied by the operator.

    if args.command == "demo":
        settings = simulation_settings()  # Point all demo writes at the simulation data directory.
        if args.reset and settings.storage.exists():
            shutil.rmtree(settings.storage)  # only ever deletes data/simulation/
        print(json.dumps(run_demo(settings), indent=2))  # Show the seeded job summary as JSON.
        if args.serve:
            import uvicorn  # Import the optional server only when the flag requests it.
            from app.main import create_app  # Build the dashboard with the same demo settings.
            uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)  # Bind to localhost only.
        return

    settings = Settings()  # Read live-mode settings for all commands below.
    registry = Registry(settings)  # Resolve the approved case list and its baseline evidence.
    if args.command == "baseline":
        results = [Validator(settings).baseline(case) for case in registry.cases.values()]  # Prove each weak test.
        # Print the verdicts without the (large) raw pytest evidence; the JSON files keep everything.
        print(json.dumps([{key: value for key, value in proof.items() if key not in {"normal_evidence", "mutant_evidence"}}
                          for proof in results], indent=2))
        raise SystemExit(0 if all(proof["outcome"] == "CONFIRMED" for proof in results) else 1)  # Make shell scripts stop on an unproven baseline.
    if args.command == "bootstrap-context":
        if not settings.devin_api_key.get_secret_value() or not settings.devin_org_id:
            raise SystemExit("DEVIN_API_KEY and DEVIN_ORG_ID are required")  # Fail before any provider call.
        print(json.dumps(Devin(settings).bootstrap(registry), indent=2))  # Save or reuse the provider context.
        return

    # doctor: config + interpreter + context + baselines. Exit 1 if anything is missing.
    problems = settings.live_errors()  # Collect credential and policy failures without exposing values.
    if not settings.superset_python.is_file():
        problems.append("Prepared Superset Python interpreter is missing")  # Candidate validation cannot run.
    if not (settings.storage / "context.json").is_file():
        problems.append("Run make bootstrap-context after confirming baselines")  # Devin needs its playbook.
    if set(settings.case_issues) != set(registry.cases):
        problems.append("Bind all configured case IDs in CASE_ISSUES")  # Webhooks must map to known cases.
    for case in registry.cases.values():
        try:
            registry.evidence(case)  # Check that this case has complete, matching proof artifacts.
        except (ValueError, OSError) as exc:
            problems.append(str(exc))  # Keep the actionable validation failure for the operator.
    print(json.dumps({"mode": settings.mode, "ready": not problems, "problems": problems}, indent=2))  # Report readiness.
    raise SystemExit(1 if problems else 0)  # Exit nonzero until every live gate is satisfied.


if __name__ == "__main__":
    main()  # Make ``python -m app.cli`` execute the operator entry point.
