import argparse
import json
import shutil
import subprocess
from pathlib import Path

from app.cases import baseline_evidence, detect, load_cases, ROOT
from app.config import Settings
from app.devin import Devin, ProviderError
from app.validator import InfrastructureError, Validator


def main() -> None:
    parser = argparse.ArgumentParser(description="Local setup and evidence tools; no command launches a Devin session")
    parser.add_argument("command", choices=["doctor", "baseline", "bootstrap-context", "scan", "issue", "serve"])
    parser.add_argument("--case", dest="case_id", default="all")
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    settings, cases = Settings(), load_cases()
    selected = list(cases.values()) if args.case_id == "all" else [cases[args.case_id]]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "serve":
            import uvicorn
            from app.main import create_app

            if settings.mode == "SIMULATION":
                from app.demo import seed

                settings, store, _ = seed(settings.data_dir)
                uvicorn.run(create_app(settings, store), host="0.0.0.0", port=8000)
            else:
                uvicorn.run(create_app(settings), host="0.0.0.0", port=8000)
        elif args.command == "doctor":
            checks = {"mode": settings.mode, "docker_cli": bool(shutil.which("docker")),
                      "devin_key_present": bool(settings.devin_api_key.get_secret_value()),
                      "github_token_present": bool(settings.github_token.get_secret_value()),
                      "context_file_present": (settings.data_dir / "context.json").exists(), "cases": {}}
            for case in cases.values():
                try:
                    baseline_evidence(case, settings.data_dir)
                    checks["cases"][case.id] = "CONFIRMED"
                except (ValueError, OSError):
                    checks["cases"][case.id] = "NOT CONFIRMED"
            print(json.dumps(checks, indent=2))
        elif args.command == "bootstrap-context":
            context = Devin(settings).bootstrap(cases)
            context.update(org_id=settings.devin_org_id, repository=settings.github_repository)
            temporary = settings.data_dir / "context.tmp"
            temporary.write_text(json.dumps(context, indent=2))
            temporary.replace(settings.data_dir / "context.json")
            print("Playbook and knowledge note ready. No session was launched.")
        elif args.command == "baseline":
            if args.build:
                subprocess.run(["docker", "build", "--target", "superset-eval", "--tag", settings.evaluator_image, str(ROOT)], check=True)
            failed = False
            for case in selected:
                evidence = Validator(settings).baseline(case)
                folder = settings.data_dir / "baselines"
                folder.mkdir(exist_ok=True)
                temporary = folder / f"{case.id}.tmp"
                temporary.write_text(json.dumps(evidence, indent=2))
                temporary.replace(folder / f"{case.id}.json")
                print(json.dumps({"case": case.id, "confirmed": evidence["confirmed"], "baseline_sha": case.baseline_sha}))
                failed = failed or not evidence["confirmed"]
            if failed:
                raise SystemExit("At least one baseline was rejected. Inspect data/baselines; live execution remains blocked.")
        elif args.command == "scan":
            if not args.checkout:
                raise ValueError("scan requires --checkout pointing to your local Superset checkout")
            for path in sorted({path for case in selected for path in case.allowed_paths}):
                print(json.dumps({"path": path, "findings": detect(args.checkout / path)}, indent=2))
        elif args.command == "issue":
            for case in selected:
                evidence = baseline_evidence(case, settings.data_dir)
                print(f"# {case.title}\n\nCase: `{case.id}`\nBaseline: `{case.baseline_sha}`\n\n"
                      f"{case.contract}\n\nAcceptance: {case.acceptance}\n\n"
                      f"Designated tests: {', '.join(case.test_ids)}\n\n"
                      f"Baseline confirmed with original tests passing both normal and active-regression runs. "
                      f"Evidence fingerprint: `{evidence['fingerprint']}`.\n")
    except (ProviderError, InfrastructureError, ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
