"""Operator commands (also exposed as Make targets).

    demo               run the credential-free simulation (add --serve to browse its dashboard)
    baseline           prove each case is weak on the real Superset checkout; writes data/live/baselines/
    bootstrap-context  create/reuse the Devin Playbook + Knowledge note; writes data/live/context.json
    doctor             list everything still missing before the live worker may run

ELI5: these commands let an operator run the safe demo, prove baselines, prepare
the provider context, or inspect the gates before live work is allowed.
"""

import argparse  # ELI5: turn command-line words into typed options.
import json  # ELI5: print machine-readable command results.
import shutil  # ELI5: remove only the simulation folder when asked.

from app.cases import Registry  # ELI5: load the approved remediation cases.
from app.config import Settings  # ELI5: load environment-backed live settings.
from app.devin import Devin, launch_preflight  # ELI5: share the local launch gate without calling Devin.
from app.simulation import run_demo, simulation_settings  # ELI5: run the safe local demo.
from app.validator import Validator  # ELI5: prove a case is weak or validate its candidate.


# ELI5: this entry point chooses one bounded operator command and reports its result.
def main() -> None:
    """Run one operator command and exit with a status suitable for automation.

    Inputs are the command name plus optional demo flags. The commands print JSON;
    ``baseline`` and ``doctor`` raise ``SystemExit(1)`` when their checks fail.
    """

    # ELI5: create the small command parser used by Make targets and local operators.
    parser = argparse.ArgumentParser()
    # ELI5: allow only the four commands implemented below.
    parser.add_argument(
        "command", choices=["demo", "doctor", "bootstrap-context", "baseline"]
    )
    # ELI5: let a demo operator request a fresh simulation folder.
    parser.add_argument("--reset", action="store_true")
    # ELI5: let a demo operator open the dashboard after seeding it.
    parser.add_argument("--serve", action="store_true")
    # ELI5: read the actual command line supplied by the operator.
    args = parser.parse_args()

    # ELI5: the demo path stays credential-free and never uses live storage.
    if args.command == "demo":
        # ELI5: point all demo writes at the simulation data directory.
        settings = simulation_settings()
        # ELI5: remove the simulation folder only when reset was explicitly requested.
        if args.reset and settings.storage.exists():
            # ELI5: clear only the simulation folder when the operator asks for a reset.
            shutil.rmtree(settings.storage)
        # ELI5: show the seeded job summary as JSON.
        print(json.dumps(run_demo(settings), indent=2))
        # ELI5: start the optional browser server only when the operator asks for it.
        if args.serve:
            # ELI5: import the optional server only when the flag requests it.
            import uvicorn
            # ELI5: build the dashboard with the same demo settings.
            from app.main import create_app
            # ELI5: bind the optional demo server to localhost.
            uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)
        # ELI5: stop after the demo so live-mode checks cannot run accidentally.
        return

    # ELI5: all remaining commands inspect or prepare live-mode configuration.
    settings = Settings()
    # ELI5: resolve the approved case list and its baseline evidence.
    registry = Registry(settings)
    # ELI5: baseline proves every registered case is weak before remediation is attempted.
    if args.command == "baseline":
        # ELI5: run the independent baseline check for every approved case.
        results = [Validator(settings).baseline(case) for case in registry.cases.values()]
        # ELI5: print verdicts without the large raw pytest evidence stored in JSON files.
        print(json.dumps([{key: value for key, value in proof.items() if key not in {"normal_evidence", "mutant_evidence"}}
                          for proof in results], indent=2))
        # ELI5: make shell scripts stop when any baseline was not confirmed.
        raise SystemExit(0 if all(proof["outcome"] == "CONFIRMED" for proof in results) else 1)
    # ELI5: bootstrap-context prepares the provider's instructions after credential checks.
    if args.command == "bootstrap-context":
        # ELI5: require both provider credentials before creating any remote context.
        if not settings.devin_api_key.get_secret_value() or not settings.devin_org_id:
            # ELI5: fail before any provider call when required credentials are missing.
            raise SystemExit("DEVIN_API_KEY and DEVIN_ORG_ID are required")
        # ELI5: save or reuse the provider context and print its safe summary.
        print(json.dumps(Devin(settings).bootstrap(registry), indent=2))
        # ELI5: stop after context setup so the doctor path does not run.
        return

    # ELI5: doctor checks config, interpreter, context, and baselines before live work.
    problems = settings.live_errors()
    # ELI5: flag any issue binding that does not match the approved registry.
    if set(settings.case_issues) != set(registry.cases):
        # ELI5: webhooks must map every configured issue to a known case.
        problems.append("Bind all configured case IDs in CASE_ISSUES")
    # ELI5: reuse the exact no-provider preflight that webhook admission and session launch use.
    preflight_failures: dict[str, list[str]] = {}
    for case in registry.cases.values():
        try:
            # ELI5: check the interpreter, checkout, context JSON, repository, and case branch together.
            launch_preflight(settings, case, settings.github_repository)
        except (ValueError, OSError) as exc:
            # ELI5: group identical failures so every case is checked without noisy duplicate output.
            preflight_failures.setdefault(str(exc), []).append(case.id)
    for reason, case_ids in preflight_failures.items():
        # ELI5: state which approved cases need local setup before live work can start.
        problems.append(f"Launch preflight ({', '.join(case_ids)}): {reason}")
    # ELI5: inspect each case's proof and keep its safe failure text for the operator.
    for case in registry.cases.values():
        # ELI5: try each registered case so all missing proof is surfaced.
        try:
            # ELI5: check that this case has complete, matching proof artifacts.
            registry.evidence(case)
        except (ValueError, OSError) as exc:
            # ELI5: keep the actionable validation failure for the operator.
            problems.append(str(exc))
    # ELI5: report readiness without exposing any credential values.
    print(json.dumps({"mode": settings.mode, "ready": not problems, "problems": problems}, indent=2))
    # ELI5: choose success only when the problem list is empty.
    raise SystemExit(1 if problems else 0)


# ELI5: run the command entry point only when this file is executed as a script.
if __name__ == "__main__":
    # ELI5: run the operator entry point when this module is executed directly.
    main()
