"""The independent validator: the only thing that can say "VERIFIED".

ELI5: Devin hands us a commit. We check it out into a throwaway folder and run
the designated test twice with the *same* pytest plugin (`evals/challenges.py`):

    phase "normal" -> clean Superset code                -> test must PASS
    phase "mutant" -> the pre-registered regression on   -> test must FAIL (assertion)

Both must hold, from real pytest reports we wrote ourselves, or it is not verified.
The same two runs, applied to the *original* test, produce the baseline proof
(there the mutant run PASSING is what proves the test was weak).

Everything Devin could tamper with is checked: the commit must descend from the
pinned baseline, only allowed test files may change, the production module must
import from inside the worktree, and the run may not modify tracked files.
"""

import json
import os
import re
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.cases import Case, Registry, detect, harness_fingerprint
from app.config import ROOT, Settings
from app.github import Candidate
from app.models import Job, now

# One line-of-diff budget keeps repairs reviewable by a human.
MAX_CHANGED_LINES = 200


@dataclass
class Evaluation:
    """Verdict for one candidate SHA. `outcome` drives the state machine; `evidence` is shown on the dashboard."""

    outcome: str  # VERIFIED | NORMAL_FAILED | REGRESSION_SURVIVED | SCOPE_REJECTED | STALE_SHA | INFRA_ERROR
    summary: str
    normal: str = "NOT_RUN"  # per-phase classification, see `classify`
    mutant: str = "NOT_RUN"
    evidence: dict = field(default_factory=dict)

    @property
    def repair_failure(self) -> bool:
        # These two mean "Devin's test is wrong" and earn the single correction attempt.
        return self.outcome in {"NORMAL_FAILED", "REGRESSION_SURVIVED"}

    def to_dict(self) -> dict:
        return asdict(self)


def classify(report: dict, test_ids: list[str]) -> str:
    """Turn one raw pytest report (from evals/challenges.py) into PASS / ASSERTION_FAILED / other.

    Anything unexpected (wrong tests collected, missing phases, skipped, xfail, errors
    in setup, control fixture not active) is *not* a pass: INFRA_ERROR / NOT_VERIFIED /
    INVALID_CONTROL. Only a clean run, or a genuine assertion failure in the test body,
    counts as evidence.
    """
    collected = report.get("collected", [])
    if sorted(collected) != sorted(test_ids) or len(collected) != len(set(collected)):
        return "INFRA_ERROR"  # exactly the designated tests, no more, no less
    reports = report.get("reports", [])
    if report.get("exitcode") not in (0, 1) or len(reports) != 3 * len(test_ids):
        return "INFRA_ERROR"  # each test yields setup + call + teardown
    assertion_failed = False
    for node in test_ids:
        if report.get("controls", {}).get(node) is not True:
            return "INVALID_CONTROL"  # the challenge fixture never ran -> the run proves nothing
        phases = [entry for entry in reports if entry.get("nodeid") == node]
        if sorted(entry.get("when", "") for entry in phases) != ["call", "setup", "teardown"]:
            return "INFRA_ERROR"
        for entry in phases:
            if entry.get("xfail") or entry.get("outcome") == "skipped":
                return "NOT_VERIFIED"  # skipping / xfail is the classic way to fake green
            if entry.get("outcome") == "passed":
                continue
            if entry.get("when") == "call" and entry.get("outcome") == "failed" and entry.get("assertion"):
                assertion_failed = True  # the test itself said "no": this is the good kind of failure
            else:
                return "INFRA_ERROR"  # crashed in setup/teardown or a non-assertion exception
    expected_code = 1 if assertion_failed else 0
    return ("ASSERTION_FAILED" if assertion_failed else "PASS") if report["exitcode"] == expected_code else "INFRA_ERROR"


def compare_runs(normal: str, mutant: str) -> Evaluation:
    """Combine the two phase results into the verdict."""
    if normal not in {"PASS", "ASSERTION_FAILED"}:
        return Evaluation("INFRA_ERROR", "Normal run could not establish correct behavior", normal, mutant)
    if normal == "ASSERTION_FAILED":
        return Evaluation("NORMAL_FAILED", "Designated test fails against correct behavior", normal, mutant)
    if mutant == "ASSERTION_FAILED":
        return Evaluation("VERIFIED", "Correct behavior passes; the active registered regression is detected", normal, mutant)
    if mutant == "PASS":
        return Evaluation("REGRESSION_SURVIVED", "The active registered regression still escapes the test", normal, mutant)
    return Evaluation("INFRA_ERROR", "Challenge, collection, setup, skip, or execution error", normal, mutant)


def run_process(command: list[str], cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    """Run pytest in its own process group so a timeout kills it *and* any children."""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        output.seek(0)
        return subprocess.CompletedProcess(command, process.returncode, output.read(65536).decode(errors="replace"))


class Validator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo = settings.superset_repo_path.resolve()
        # absolute(), not resolve(): a venv's bin/python is a symlink to the bare interpreter (no pytest there).
        self.python = settings.superset_python.absolute()

    def git(self, *args: str) -> str:
        """Run git in the Superset checkout with hooks and prompts disabled."""
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if result.returncode:
            raise ValueError("Git checkout/fetch failed; inspect the configured public fork and pinned baseline")
        return result.stdout.strip()

    def scope_error(self, case: Case, sha: str) -> str | None:
        """Why this commit is out of bounds, or None if it only edits allowed test files within budget."""
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return "Invalid candidate SHA"
        if self.git("merge-base", case.baseline_sha, sha) != case.baseline_sha:
            return "Candidate is not based on the pinned baseline"
        changed = self.git("diff", "--name-status", "--no-renames", case.baseline_sha, sha)
        if not changed:
            return "Candidate contains no repair"
        for line in changed.splitlines():
            status, path = line.split("\t", 1)
            if status != "M" or path not in case.allowed_paths:  # M = modified; no adds/deletes/renames
                return "Changes escape the allowed test paths or add/remove/rename files"
            if not self.git("ls-tree", sha, "--", path).startswith("100644 blob "):
                return "Only regular, non-executable test files are allowed"
        total = 0
        for line in self.git("diff", "--numstat", case.baseline_sha, sha).splitlines():
            added, removed, _ = line.split("\t", 2)
            if not added.isdigit() or not removed.isdigit():
                return "Binary changes are not allowed"
            total += int(added) + int(removed)
        return f"Repair exceeds the {MAX_CHANGED_LINES}-line review budget" if total > MAX_CHANGED_LINES else None

    @contextmanager
    def worktree(self, sha: str) -> Iterator[Path]:
        """A fresh, detached checkout of `sha` that disappears afterwards."""
        with tempfile.TemporaryDirectory(prefix="drp-") as directory:
            checkout = Path(directory) / "checkout"
            self.git("worktree", "add", "--detach", str(checkout), sha)
            try:
                yield checkout
            finally:
                self.git("worktree", "remove", "--force", str(checkout))

    def run_phase(self, case: Case, sha: str, phase: str) -> tuple[str, dict]:
        """Run the designated tests once at `sha` in the given phase ("normal" or "mutant"). Returns (class, evidence)."""
        with self.worktree(sha) as checkout, tempfile.TemporaryDirectory(prefix="drp-report-") as directory:
            report_path = Path(directory) / "report.json"
            # `-p evals.challenges` loads our plugin from ROOT; `-o addopts=` ignores Superset's own pytest flags.
            command = [str(self.python), "-m", "pytest", "-p", "evals.challenges", "--rootdir", str(checkout),
                       "-o", "addopts=", "--drp-challenge", case.challenge, "--drp-phase", phase,
                       "--drp-report", str(report_path), *case.test_ids]
            # Minimal environment: never pass the control plane's API keys to candidate Python.
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory,
                           "PYTHONPATH": os.pathsep.join([str(ROOT), str(checkout)]), "PYTHONDONTWRITEBYTECODE": "1",
                           "PYTHONHASHSEED": "0", "TZ": "UTC", "SUPERSET_TESTENV": "true",
                           "SUPERSET_SECRET_KEY": "disposable-local-evaluation-only", "SUPERSET_HOME": directory}
            if self.settings.superset_config_path:
                environment["SUPERSET_CONFIG_PATH"] = str(self.settings.superset_config_path.resolve())
            try:
                process = run_process(command, checkout, environment, self.settings.validation_timeout_seconds)
                report = json.loads(report_path.read_text()) if report_path.exists() else {}
            except subprocess.TimeoutExpired:
                return "INFRA_ERROR", {"reason": "timeout"}
            except (OSError, ValueError):
                return "INFRA_ERROR", {"reason": "missing interpreter or invalid evidence"}
            if process.returncode != report.get("exitcode"):
                return "INFRA_ERROR", {"reason": "missing or inconsistent pytest report", "exitcode": process.returncode}
            changed = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=checkout, timeout=10).returncode
            if changed:
                return "INFRA_ERROR", {"reason": "test run modified tracked files"}  # a test rewriting code is suspicious
            return classify(report, case.test_ids), {"phase": phase, "pytest": report, "command": command}

    def run_both(self, case: Case, sha: str) -> tuple[str, str, dict]:
        """Normal then mutant phase at `sha`; returns (normal_class, mutant_class, evidence)."""
        normal, normal_evidence = self.run_phase(case, sha, "normal")
        mutant, mutant_evidence = self.run_phase(case, sha, "mutant")
        return normal, mutant, {"normal_evidence": normal_evidence, "mutant_evidence": mutant_evidence}

    def baseline(self, case: Case) -> dict:
        """Prove the *original* test is weak at the pinned SHA and write the proof to data/live/baselines/.

        CONFIRMED  = passes clean AND passes with the regression on (weak test; admit the case)
        REJECTED   = both runs produced real verdicts but not that pattern (the test is fine, or broken)
        INFRA_ERROR = could not get trustworthy evidence
        """
        proof = {"case_id": case.id, "sha": case.baseline_sha, "mode": "LIVE", "recorded_at": now().isoformat(),
                 "case_fingerprint": case.fingerprint, "harness_fingerprint": harness_fingerprint()}
        try:
            if not self.settings.allow_local_validation:
                raise ValueError("Local execution requires ALLOW_LOCAL_VALIDATION=true in a disposable environment")
            test_file = case.test_ids[0].split("::")[0]
            proof["findings"] = detect(self.git("show", f"{case.baseline_sha}:{test_file}"), test_file)
            normal, mutant, evidence = self.run_both(case, case.baseline_sha)
            proof.update(normal=normal, mutant=mutant, **evidence)
            if normal == mutant == "PASS":
                proof["outcome"] = "CONFIRMED"
            elif normal in {"PASS", "ASSERTION_FAILED"} and mutant in {"PASS", "ASSERTION_FAILED"}:
                proof["outcome"] = "REJECTED"
            else:
                proof["outcome"] = "INFRA_ERROR"
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            proof.update(outcome="INFRA_ERROR", reason=str(exc)[:200])
        destination = self.settings.data_dir.resolve() / "live/baselines" / f"{case.id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(proof, indent=2))
        return proof

    def validate(self, job: Job, case: Case, candidate: Candidate) -> Evaluation:
        """Judge Devin's candidate commit. Order: baseline still valid -> fetch exact SHA -> scope -> run both phases."""
        try:
            if not self.settings.allow_local_validation:
                raise ValueError("Local evaluation has not been explicitly enabled")
            Registry(self.settings).evidence(case)
            self.git("fetch", "--no-tags", f"https://github.com/{job.repository}.git", f"refs/pull/{candidate.number}/head")
            if self.git("rev-parse", "FETCH_HEAD") != candidate.sha:
                return Evaluation("STALE_SHA", "PR changed while fetching the candidate")
            error = self.scope_error(case, candidate.sha)
            if error:
                return Evaluation("SCOPE_REJECTED", error)
            normal, mutant, evidence = self.run_both(case, candidate.sha)
            result = compare_runs(normal, mutant)
            result.evidence = {"sha": candidate.sha, "challenge": case.challenge,
                               "harness_fingerprint": harness_fingerprint(),
                               "normal": evidence["normal_evidence"], "mutant": evidence["mutant_evidence"]}
            return result
        except (ValueError, OSError, subprocess.SubprocessError):
            return Evaluation("INFRA_ERROR", "Baseline, interpreter, worktree, or public-fork fetch unavailable")
