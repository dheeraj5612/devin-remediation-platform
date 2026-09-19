import json
import subprocess
import tempfile
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path

from app.cases import Case, ROOT
from app.config import Settings
from app.github import Candidate


class InfrastructureError(Exception):
    pass


@dataclass
class Verdict:
    outcome: str
    reason: str
    evidence: dict = field(default_factory=dict)


def complete(case: Case, report: dict) -> bool:
    collected = report.get("collected", [])
    if sorted(collected) != sorted(case.test_ids) or report.get("collection_errors"):
        return False
    phases = report.get("phases", [])
    for node in case.test_ids:
        rows = [row for row in phases if row.get("nodeid") == node]
        if len(rows) != 3 or {row.get("when") for row in rows} != {"setup", "call", "teardown"}:
            return False
        if not report.get("controls", {}).get(node):
            return False
        if any(row.get("outcome") == "skipped" or row.get("xfail") for row in rows):
            return False
        if any(row.get("outcome") != "passed" for row in rows if row.get("when") != "call"):
            return False
    return True


def passes(case: Case, report: dict) -> bool:
    return complete(case, report) and report.get("exit_code") == 0 and all(
        row.get("outcome") == "passed" for row in report["phases"]
    )


def baseline_confirmed(case: Case, normal: dict, mutant: dict) -> bool:
    return passes(case, normal) and passes(case, mutant)


def judge(case: Case, normal: dict, mutant: dict) -> Verdict:
    evidence = {"normal": "UNKNOWN", "mutant": "UNKNOWN", "normal_report": normal, "mutant_report": mutant}
    if not complete(case, normal):
        return Verdict("INFRA", "Normal run had missing tests, inactive controls, skips, or setup/collection errors", evidence)
    normal_failures = [row for row in normal["phases"] if row["when"] == "call" and row["outcome"] == "failed"]
    if any(row.get("exception") in {"ImportError", "ModuleNotFoundError"} for row in normal_failures):
        return Verdict("INFRA", "Normal test execution encountered an import failure", evidence)
    if normal_failures:
        evidence["normal"] = "FAIL"
        return Verdict("REPAIR", "Designated tests fail against correct behavior", evidence)
    if not passes(case, normal):
        return Verdict("INFRA", "Normal pytest process did not finish successfully", evidence)
    evidence["normal"] = "PASS"
    if not complete(case, mutant):
        return Verdict("INFRA", "Regression run had missing tests, inactive controls, skips, or setup/collection errors", evidence)
    failures = [row for row in mutant["phases"] if row["when"] == "call" and row["outcome"] == "failed"]
    if mutant.get("exit_code") == 1 and failures and all(row["failure"] in {"assertion", "missing_expected_exception"} for row in failures):
        evidence["mutant"] = "DETECTED"
        return Verdict("VERIFIED", "Correct behavior passes and the active pre-registered regression is detected", evidence)
    if passes(case, mutant):
        evidence["mutant"] = "ESCAPED"
        return Verdict("REPAIR", "The active pre-registered regression still escapes the designated tests", evidence)
    return Verdict("INFRA", "Regression failed for a reason other than the intended assertion", evidence)


class Validator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def docker(self, arguments: list[str], timeout: int = 30) -> str:
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(["docker", *arguments], stdout=output, stderr=output, timeout=timeout, check=False)
                output.seek(0)
                text = output.read(200000).decode("utf-8", errors="replace")
            if arguments[0] in {"start", "build"}:
                self.settings.data_dir.mkdir(parents=True, exist_ok=True)
                (self.settings.data_dir / "last-evaluator.log").write_text(text[-16000:])
            if result.returncode:
                raise InfrastructureError(f"Docker {arguments[0]} failed (exit {result.returncode})")
            return text.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InfrastructureError(f"Docker {arguments[0]} unavailable or timed out") from error

    def run(self, case: Case, mutant: bool, files: dict[str, str] | None = None) -> dict:
        label = self.docker(["image", "inspect", "--format", '{{ index .Config.Labels "remediation.baseline" }}', self.settings.evaluator_image])
        if label != case.baseline_sha:
            raise InfrastructureError("Evaluator image does not match the pinned baseline")
        container_id = None
        image = f"remediation-candidate:{uuid4().hex}"
        image_built = False
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            try:
                (directory / "probe.py").write_bytes((ROOT / "evals" / "probe.py").read_bytes())
                instructions = [f"FROM {self.settings.evaluator_image}", "USER root", "COPY probe.py /evaluator/probe.py"]
                for path, content in (files or {}).items():
                    if path not in case.allowed_paths:
                        raise InfrastructureError("Unexpected candidate path")
                    source = directory / path
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text(content)
                    instructions.append(f"COPY {path} /opt/superset/{path}")
                (directory / "Dockerfile").write_text("\n".join([*instructions, "USER 1000:1000"]))
                self.docker(["build", "--network", "none", "--tag", image, str(directory)], 120)
                image_built = True
                container_id = self.docker([
                    "create", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--pids-limit", "256", "--memory", "3g", "--cpus", "2",
                    "--tmpfs", "/tmp:rw,nosuid,size=512m", "--mount", "type=volume,destination=/reports",
                    "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                    "--env", "PYTHONPATH=/evaluator:/opt/superset", "--env", "SUPERSET_SECRET_KEY=local-test-only-not-a-service",
                    "--env", f"EVAL_CHALLENGE={case.challenge}", "--env", f"EVAL_MUTANT={int(mutant)}",
                    image, *case.command,
                ])
                # `docker start -a` forwards pytest's nonzero exit status, which is expected for a detected mutant.
                try:
                    self.docker(["start", "-a", container_id], self.settings.validation_timeout_seconds)
                except InfrastructureError:
                    state = json.loads(self.docker(["inspect", "--format", "{{json .State}}", container_id]))
                    if state.get("Running") or state.get("OOMKilled") or state.get("ExitCode") not in (0, 1):
                        raise
                report_path = directory / "result.json"
                self.docker(["cp", f"{container_id}:/reports/result.json", str(report_path)])
                if report_path.stat().st_size > 200000:
                    raise InfrastructureError("Evaluator report exceeded its size limit")
                report = json.loads(report_path.read_text())
                state = json.loads(self.docker(["inspect", "--format", "{{json .State}}", container_id]))
                if state.get("OOMKilled") or state.get("ExitCode") != report.get("exit_code"):
                    raise InfrastructureError("Evaluator process and report disagree")
                return report
            except (ValueError, OSError) as error:
                raise InfrastructureError("Evaluator did not produce a valid report") from error
            finally:
                if container_id:
                    self.docker(["rm", "-f", "-v", container_id])
                if image_built:
                    self.docker(["image", "rm", "-f", image])

    def evaluate(self, case: Case, candidate: Candidate) -> Verdict:
        try:
            normal = self.run(case, False, candidate.files)
            mutant = self.run(case, True, candidate.files)
            return judge(case, normal, mutant)
        except InfrastructureError as error:
            return Verdict("INFRA", str(error))

    def baseline(self, case: Case) -> dict:
        normal, mutant = self.run(case, False), self.run(case, True)
        return {"case_id": case.id, "baseline_sha": case.baseline_sha, "fingerprint": case.fingerprint,
                "confirmed": baseline_confirmed(case, normal, mutant), "normal": normal, "mutant": mutant}
