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

# ELI5: this hard budget prevents a small repair from hiding a broad refactor.
# One line-of-diff budget keeps repairs reviewable by a human.
MAX_CHANGED_LINES = 200


# ELI5: this record is the single trusted verdict passed to the state machine and dashboard.
@dataclass
class Evaluation:
    """Verdict for one candidate SHA. `outcome` drives the state machine; `evidence` is shown on the dashboard."""

    # ELI5: this top-level outcome decides whether the job verifies, retries, or escalates.
    outcome: str  # VERIFIED | NORMAL_FAILED | REGRESSION_SURVIVED | SCOPE_REJECTED | STALE_SHA | INFRA_ERROR
    # ELI5: keep a human-readable explanation alongside machine status.
    summary: str
    # ELI5: record the ordinary test phase classification for test-quality cases.
    normal: str = "NOT_RUN"  # per-phase classification, see `classify`
    # ELI5: record the mutant test phase classification for test-quality cases.
    mutant: str = "NOT_RUN"
    # ELI5: preserve raw reports and hashes so reviewers can inspect why the verdict was made.
    evidence: dict = field(default_factory=dict)
    # ELI5: record the trusted application oracle result for production cases.
    application: str = "NOT_RUN"  # application oracle result: PASS, REGRESSION, CONTRACT_FAILED, or INFRA_ERROR

    @property
    def repair_failure(self) -> bool:
        """Report whether one bounded correction attempt is appropriate for this verdict."""
        # ELI5: these are assessed repair problems, not missing infrastructure.
        return self.outcome in {"NORMAL_FAILED", "REGRESSION_SURVIVED", "APPLICATION_FAILED"}

    def to_dict(self) -> dict:
        """Serialize the verdict and all phase/application evidence for durable event storage."""
        # ELI5: turn the dataclass into JSON-friendly fields without dropping evidence.
        return asdict(self)


def classify(report: dict, test_ids: list[str]) -> str:
    """Turn one raw pytest report (from evals/challenges.py) into PASS / ASSERTION_FAILED / other.

    Anything unexpected (wrong tests collected, missing phases, skipped, xfail, errors
    in setup, control fixture not active) is *not* a pass: INFRA_ERROR / NOT_VERIFIED /
    INVALID_CONTROL. Only a clean run, or a genuine assertion failure in the test body,
    counts as evidence.
    """
    # ELI5: reject a non-object report before any dictionary lookup can raise an unhelpful exception.
    if not isinstance(report, dict) or not isinstance(test_ids, list) or not all(isinstance(node, str) for node in test_ids):
        return "INFRA_ERROR"
    # ELI5: read the list of tests pytest says it collected.
    collected = report.get("collected", [])
    # ELI5: require exactly the approved node IDs, with no duplicates or extras.
    if (not isinstance(collected, list) or not all(isinstance(node, str) for node in collected)
            or sorted(collected) != sorted(test_ids) or len(collected) != len(set(collected))):
        # ELI5: reject missing, extra, or duplicated test collection evidence.
        return "INFRA_ERROR"  # exactly the designated tests, no more, no less
    # ELI5: read per-phase report entries for the collected tests.
    reports = report.get("reports", [])
    # ELI5: every test needs setup, call, and teardown, and only exit codes 0 or 1 are expected.
    exitcode = report.get("exitcode")
    if (isinstance(exitcode, bool) or exitcode not in (0, 1) or not isinstance(reports, list)
            or not all(isinstance(entry, dict) for entry in reports)
            or len(reports) != 3 * len(test_ids)):
        # ELI5: reject a report with an unexpected process code or phase count.
        return "INFRA_ERROR"  # each test yields setup + call + teardown
    controls = report.get("controls", {})
    # ELI5: a missing control map is the known invalid-control result; another shape is malformed evidence.
    if not isinstance(controls, dict):
        return "INFRA_ERROR"
    # ELI5: remember whether the intentional mutant produced a real assertion failure.
    assertion_failed = False
    # ELI5: validate each designated test independently so one malformed report cannot hide another.
    for node in test_ids:
        # ELI5: the challenge plugin must prove its controls ran for this exact node.
        if controls.get(node) is not True:
            return "INVALID_CONTROL"  # the challenge fixture never ran -> the run proves nothing
        # ELI5: select only report entries belonging to this test node.
        phases = [entry for entry in reports if entry.get("nodeid") == node]
        # ELI5: require one report for each pytest phase in a predictable order-independent check.
        phase_names = [entry.get("when") for entry in phases]
        if (not all(isinstance(phase, str) for phase in phase_names)
                or len(phases) != 3 or set(phase_names) != {"call", "setup", "teardown"}):
            # ELI5: missing or repeated lifecycle phases cannot prove a test result.
            return "INFRA_ERROR"
        # ELI5: inspect every phase so skip, xfail, and setup crashes cannot look green.
        for entry in phases:
            # ELI5: skipped or xfailed tests are not evidence of a working repair.
            if entry.get("xfail") or entry.get("outcome") == "skipped":
                return "NOT_VERIFIED"  # skipping / xfail is the classic way to fake green
            if entry.get("outcome") == "passed":
                # ELI5: a passed setup, call, or teardown phase adds no failure signal.
                continue
            # ELI5: only a genuine assertion failure in the test call counts as mutant evidence.
            if entry.get("when") == "call" and entry.get("outcome") == "failed" and entry.get("assertion"):
                # ELI5: remember the one acceptable failure shape from the registered mutant.
                assertion_failed = True  # the test itself said "no": this is the good kind of failure
            else:
                # ELI5: a non-assertion failure means the run itself is untrustworthy.
                return "INFRA_ERROR"  # crashed in setup/teardown or a non-assertion exception
    # ELI5: map the observed assertion flag to the only valid pytest exit code.
    expected_code = 1 if assertion_failed else 0
    # ELI5: return a verdict only when the exit code agrees with all inspected phases.
    return ("ASSERTION_FAILED" if assertion_failed else "PASS") if exitcode == expected_code else "INFRA_ERROR"


def compare_runs(normal: str, mutant: str) -> Evaluation:
    """Combine the two phase results into the verdict."""
    # ELI5: a normal run must be either a clean pass or a genuine assertion failure before comparison.
    if normal not in {"PASS", "ASSERTION_FAILED"}:
        # ELI5: an untrusted normal phase prevents any comparison from being meaningful.
        return Evaluation("INFRA_ERROR", "Normal run could not establish correct behavior", normal, mutant)
    # ELI5: a normal assertion failure means the designated test itself is broken.
    if normal == "ASSERTION_FAILED":
        # ELI5: correct application behavior failing means the candidate test is invalid.
        return Evaluation("NORMAL_FAILED", "Designated test fails against correct behavior", normal, mutant)
    # ELI5: a mutant assertion failure proves the test catches the registered regression.
    if mutant == "ASSERTION_FAILED":
        # ELI5: clean behavior passed and the mutant failed, so the test is verified.
        return Evaluation("VERIFIED", "Correct behavior passes; the active registered regression is detected", normal, mutant)
    # ELI5: a mutant pass means the test still misses the registered defect.
    if mutant == "PASS":
        # ELI5: a mutant pass means the repair still lets the defect escape the test.
        return Evaluation("REGRESSION_SURVIVED", "The active registered regression still escapes the test", normal, mutant)
    # ELI5: every other mutant result lacks trustworthy acceptance evidence.
    return Evaluation("INFRA_ERROR", "Challenge, collection, setup, skip, or execution error", normal, mutant)


def run_process(command: list[str], cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    """Run pytest in its own process group so a timeout kills it *and* any children."""
    # ELI5: capture noisy candidate output without letting it flood the control-plane logs.
    with tempfile.TemporaryFile() as output:
        # ELI5: start a separate process group so timeouts can stop descendants too.
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        # ELI5: keep baseline setup failures separate from an assessed weak or fixed case.
        try:
            # ELI5: wait only for the configured validation window.
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # ELI5: kill the complete group when a candidate hangs.
            os.killpg(process.pid, signal.SIGKILL)
            # ELI5: reap the killed process before returning the timeout to the caller.
            process.wait()
            # ELI5: let the caller classify this run as infrastructure timeout.
            raise
        # ELI5: rewind the captured stream and return a bounded text excerpt with the exit code.
        output.seek(0)
        # ELI5: package command, exit code, and captured output for the classifier.
        return subprocess.CompletedProcess(command, process.returncode, output.read(65536).decode(errors="replace"))


# ELI5: this class owns every independent check that can grant a VERIFIED verdict.
class Validator:
    """Run scope, isolation, and independent oracle checks before granting VERIFIED."""

    def __init__(self, settings: Settings) -> None:
        """Capture the configured Superset checkout and interpreter used by disposable runs."""
        # ELI5: keep the same settings object used by baseline and candidate gates.
        self.settings = settings
        # ELI5: resolve the source checkout once so every git command uses the intended repository.
        self.repo = settings.superset_repo_path.resolve()
        # absolute(), not resolve(): a venv's bin/python is a symlink to the bare interpreter (no pytest there).
        self.python = settings.superset_python.absolute()

    def git(self, *args: str) -> str:
        """Run git in the Superset checkout with hooks and prompts disabled."""
        # ELI5: run Git with hooks and interactive prompts disabled for deterministic validation.
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        # ELI5: any nonzero git result means the checkout cannot be trusted for validation.
        if result.returncode:
            # ELI5: convert any git failure into a safe configuration error without exposing command output.
            raise ValueError("Git checkout/fetch failed; inspect the configured public fork and pinned baseline")
        # ELI5: return only stdout because callers compare hashes and inspect paths, not diagnostics.
        return result.stdout.strip()

    def scope_error(self, case: Case, sha: str) -> str | None:
        """Explain why a candidate escapes the case's approved paths or diff budget."""
        # ELI5: candidate SHAs must be exactly forty lowercase hexadecimal characters.
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            # ELI5: reject malformed identifiers before asking Git for their ancestry.
            return "Invalid candidate SHA"
        # ELI5: the candidate must descend directly from the case's pinned baseline.
        if self.git("merge-base", case.baseline_sha, sha) != case.baseline_sha:
            # ELI5: reject unrelated history so validation cannot bless a different project state.
            return "Candidate is not based on the pinned baseline"
        # ELI5: inspect every changed path before running any candidate code.
        changed = self.git("diff", "--name-status", "--no-renames", case.baseline_sha, sha)
        # ELI5: a no-op commit cannot represent a repair.
        if not changed:
            # ELI5: require at least one approved file change before running candidate code.
            return "Candidate contains no repair"
        # ELI5: enforce the same file and status allow-list for every changed path.
        for line in changed.splitlines():
            # ELI5: split Git's status record into the operation and repository path.
            status, path = line.split("\t", 1)
            # ELI5: reject additions, removals, renames, and files outside the case scope.
            if status != "M" or path not in case.allowed_paths:  # M = modified; no adds/deletes/renames
                # ELI5: reject this entire candidate when any one path escapes the allow-list.
                return "Changes escape the allowed test paths or add/remove/rename files"
            # ELI5: require ordinary tracked files so executable or unusual entries cannot bypass review.
            if not self.git("ls-tree", sha, "--", path).startswith("100644 blob "):
                # ELI5: reject non-regular file modes before executing the candidate.
                return "Only regular, non-executable test files are allowed"
        # ELI5: start the total changed-line count used by the review budget.
        total = 0
        # ELI5: inspect Git's numeric diff for every changed path.
        for line in self.git("diff", "--numstat", case.baseline_sha, sha).splitlines():
            # ELI5: read added, removed, and path columns from Git's tab-separated output.
            added, removed, _ = line.split("\t", 2)
            # ELI5: binary or otherwise non-numeric diffs are not reviewable under this gate.
            if not added.isdigit() or not removed.isdigit():
                # ELI5: reject an uncountable diff instead of guessing its review size.
                return "Binary changes are not allowed"
            # ELI5: accumulate both additions and removals against the hard budget.
            total += int(added) + int(removed)
        # ELI5: return a human-readable rejection only when the total exceeds the budget.
        return f"Repair exceeds the {MAX_CHANGED_LINES}-line review budget" if total > MAX_CHANGED_LINES else None

    @contextmanager
    def worktree(self, sha: str) -> Iterator[Path]:
        """A fresh, detached checkout of `sha` that disappears afterwards."""
        # ELI5: isolate each candidate in a temporary folder so it cannot modify the configured checkout.
        with tempfile.TemporaryDirectory(prefix="drp-") as directory:
            # ELI5: choose a child path that Git can register as a detached worktree.
            checkout = Path(directory) / "checkout"
            # ELI5: materialize the exact immutable SHA before running candidate code.
            self.git("worktree", "add", "--detach", str(checkout), sha)
            # ELI5: always clean up the detached worktree after yielding it to the validator.
            try:
                # ELI5: yield only the isolated checkout to the caller.
                yield checkout
            finally:
                # ELI5: remove the temporary worktree even when validation raises or times out.
                self.git("worktree", "remove", "--force", str(checkout))

    def run_phase(self, case: Case, sha: str, phase: str) -> tuple[str, dict]:
        """Run the designated tests once at `sha` in the given phase ("normal" or "mutant"). Returns (class, evidence)."""
        # ELI5: give each phase its own checkout and report directory for clean evidence.
        with self.worktree(sha) as checkout, tempfile.TemporaryDirectory(prefix="drp-report-") as directory:
            # ELI5: keep the plugin's JSON report outside candidate-controlled source paths.
            report_path = Path(directory) / "report.json"
            # `-p evals.challenges` loads our plugin from ROOT; `-o addopts=` ignores Superset's own pytest flags.
            # ELI5: run only the registered challenge and designated test IDs in this phase.
            command = [str(self.python), "-m", "pytest", "-p", "evals.challenges", "--rootdir", str(checkout),
                       "-o", "addopts=", "--drp-challenge", case.challenge, "--drp-phase", phase,
                       "--drp-report", str(report_path), *case.test_ids]
            # Minimal environment: never pass the control plane's API keys to candidate Python.
            # ELI5: pass a scrubbed environment so candidate code cannot read control-plane secrets.
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory,
                           "PYTHONPATH": os.pathsep.join([str(ROOT), str(checkout)]), "PYTHONDONTWRITEBYTECODE": "1",
                           "PYTHONHASHSEED": "0", "TZ": "UTC", "SUPERSET_TESTENV": "true",
                           "SUPERSET_SECRET_KEY": "disposable-local-evaluation-only", "SUPERSET_HOME": directory}
            # ELI5: add an optional disposable settings module only when the operator configured one.
            if self.settings.superset_config_path:
                # ELI5: add only the explicitly configured disposable Superset settings module.
                environment["SUPERSET_CONFIG_PATH"] = str(self.settings.superset_config_path.resolve())
            # ELI5: run the candidate phase and collect its structured report before classifying it.
            try:
                # ELI5: execute the phase with the hard timeout and capture its report.
                process = run_process(command, checkout, environment, self.settings.validation_timeout_seconds)
                # ELI5: missing reports are treated as empty evidence instead of inferred success.
                report = json.loads(report_path.read_text()) if report_path.exists() else {}
            except subprocess.TimeoutExpired:
                # ELI5: a timeout means the candidate did not produce trustworthy evidence.
                return "INFRA_ERROR", {"reason": "timeout"}
            except (OSError, ValueError):
                # ELI5: missing interpreters or malformed reports are infrastructure failures.
                return "INFRA_ERROR", {"reason": "missing interpreter or invalid evidence"}
            # ELI5: JSON arrays or scalars are not pytest report objects and cannot prove a result.
            if not isinstance(report, dict):
                return "INFRA_ERROR", {"reason": "pytest evidence is not an object"}
            # ELI5: pytest's process exit code must match the report's recorded exit code.
            if process.returncode != report.get("exitcode"):
                # ELI5: reject missing or forged report metadata instead of guessing a verdict.
                return "INFRA_ERROR", {"reason": "missing or inconsistent pytest report", "exitcode": process.returncode}
            # ELI5: reject tests that rewrite tracked candidate files during evaluation.
            changed = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=checkout, timeout=10).returncode
            # ELI5: reject any phase that changed tracked candidate files during evaluation.
            if changed:
                # ELI5: candidate tests must observe source, not rewrite it during validation.
                return "INFRA_ERROR", {"reason": "test run modified tracked files"}  # a test rewriting code is suspicious
            # ELI5: classify only the validated report and preserve the exact command as evidence.
            return classify(report, case.test_ids), {"phase": phase, "pytest": report, "command": command}

    def run_both(self, case: Case, sha: str) -> tuple[str, str, dict]:
        """Normal then mutant phase at `sha`; returns (normal_class, mutant_class, evidence)."""
        # ELI5: run the clean code first to prove the designated test itself is healthy.
        normal, normal_evidence = self.run_phase(case, sha, "normal")
        # ELI5: run the registered mutant second to prove the same test catches the weakness.
        mutant, mutant_evidence = self.run_phase(case, sha, "mutant")
        # ELI5: return both classifications and their raw reports for review.
        return normal, mutant, {"normal_evidence": normal_evidence, "mutant_evidence": mutant_evidence}

    def run_application(self, case: Case, sha: str) -> tuple[str, dict]:
        """Run a trusted application oracle against one detached candidate checkout.

        ELI5: test-quality cases need two pytest phases, while an application case has a
        control-plane script that knows the real defect contract. The script prints a
        controlled PASS or REGRESSION result and fails closed for every other outcome.
        """
        # ELI5: application validation is impossible without an explicitly trusted oracle path.
        if case.kind != "application" or not case.acceptance_test:
            # ELI5: report missing application configuration as infrastructure, never as a repair result.
            return "INFRA_ERROR", {"reason": "application case has no acceptance oracle"}
        # ELI5: resolving the path and checking its parent prevents a case file from naming candidate code as its oracle.
        # ELI5: resolve the trusted oracle directory and requested file before any subprocess starts.
        oracle_root = (ROOT / "evals/application_cases").resolve()
        # ELI5: resolve the case path so parent-directory checks cannot be bypassed with `..`.
        acceptance = (ROOT / case.acceptance_test).resolve()
        # ELI5: require a real file inside the control-plane oracle directory.
        if not acceptance.is_file() or not acceptance.is_relative_to(oracle_root):
            # ELI5: fail closed if the trusted evaluator was deleted or moved out of scope.
            return "INFRA_ERROR", {"reason": "trusted application oracle is missing"}
        # ELI5: run the oracle against a disposable detached checkout and temporary home.
        with self.worktree(sha) as checkout, tempfile.TemporaryDirectory(prefix="drp-application-") as directory:
            # Candidate code is first so an installed Superset package cannot satisfy the import.
            # ELI5: make the candidate import first while scrubbing inherited secrets and state.
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory,
                           "PYTHONPATH": os.pathsep.join([str(checkout), str(ROOT)]), "PYTHONDONTWRITEBYTECODE": "1",
                           "PYTHONHASHSEED": "0", "TZ": "UTC", "SUPERSET_TESTENV": "true",
                           "SUPERSET_SECRET_KEY": "disposable-local-evaluation-only", "SUPERSET_HOME": directory,
                           "DRP_CANDIDATE_ROOT": str(checkout)}
            # ELI5: pass the optional disposable settings module without exposing control-plane secrets.
            if self.settings.superset_config_path:
                # ELI5: pass only the configured disposable Superset settings, never the control plane's secrets.
                environment["SUPERSET_CONFIG_PATH"] = str(self.settings.superset_config_path.resolve())
            # ELI5: invoke only the trusted script with the selected interpreter.
            command = [str(self.python), str(acceptance)]
            # ELI5: execute the trusted application oracle inside the isolated checkout.
            try:
                # ELI5: capture the oracle's bounded output without allowing an unbounded process.
                process = run_process(command, checkout, environment, self.settings.validation_timeout_seconds)
            except subprocess.TimeoutExpired:
                # ELI5: a hung application process cannot produce acceptance evidence.
                return "INFRA_ERROR", {"reason": "application oracle timeout", "command": command}
            except OSError:
                # ELI5: a missing interpreter or process launch is infrastructure failure.
                return "INFRA_ERROR", {"reason": "missing Superset interpreter", "command": command}
            # ELI5: keep only non-empty lines so the final JSON result is easy to parse.
            lines = [line for line in process.stdout.splitlines() if line.strip()]
            # ELI5: parse only the oracle's final JSON line so diagnostics cannot become a verdict.
            try:
                # ELI5: parse the oracle's last line because Superset may log diagnostics before it.
                result = json.loads(lines[-1]) if lines else {}
            except (json.JSONDecodeError, IndexError):
                # ELI5: no final JSON means the oracle did not prove any outcome.
                return "INFRA_ERROR", {"reason": "application oracle emitted no JSON result", "command": command}
            # ELI5: a JSON array or scalar has no trusted outcome field, so fail closed before .get().
            if not isinstance(result, dict):
                return "INFRA_ERROR", {"reason": "application oracle result is not an object", "command": command}
            # ELI5: read the machine verdict that the oracle explicitly emitted.
            status = result.get("outcome")
            # ELI5: only these four strings are meaningful; every other result fails closed.
            valid_statuses = {"PASS", "REGRESSION", "CONTRACT_FAILED", "INFRA_ERROR"}
            if not isinstance(status, str) or status not in valid_statuses:
                return "INFRA_ERROR", {"reason": "application oracle returned an unknown status", "result": result,
                                        "command": command}
            # ELI5: expected exit 0 means the oracle ran; exit 2 means it reported infrastructure failure.
            expected_exit = 0 if status != "INFRA_ERROR" else 2
            # ELI5: require exit code and status to agree so a truncated or forged result fails closed.
            if process.returncode != expected_exit:
                # ELI5: reject impossible combinations instead of trusting a printed status.
                return "INFRA_ERROR", {"reason": "application oracle result was inconsistent", "result": result,
                                        "exitcode": process.returncode, "command": command}
            # ELI5: verify the oracle did not rewrite tracked candidate source while inspecting it.
            changed = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=checkout, timeout=10).returncode
            # ELI5: reject an oracle that modified candidate source while it was supposed to inspect it.
            if changed:
                # ELI5: an evaluator that changes source cannot be trusted as a read-only oracle.
                return "INFRA_ERROR", {"reason": "application oracle modified tracked files", "result": result}
            # ELI5: missing status or provenance cannot be treated as an application result.
            if status == "INFRA_ERROR" or result.get("provenance") is not True:
                # ELI5: require an explicit true provenance marker before accepting any status.
                return "INFRA_ERROR", {"reason": "application oracle could not prove candidate provenance", "result": result}
            # ELI5: return the assessed status with command, result, and bounded logs for review.
            return status, {"command": command, "result": result, "stdout": process.stdout[-4000:]}

    def baseline(self, case: Case) -> dict:
        """Prove the original case exposes its pinned defect and write proof to data/live/baselines/.

        CONFIRMED  = the trusted test or application oracle shows its expected baseline weakness
        REJECTED   = the pinned source passes or violates the application contract without the exact known regression
        INFRA_ERROR = could not get trustworthy evidence
        """
        # ELI5: start evidence with the case identity and both fingerprints that make it reproducible.
        proof = {"case_id": case.id, "sha": case.baseline_sha, "mode": "LIVE", "recorded_at": now().isoformat(),
                 "case_fingerprint": case.fingerprint, "harness_fingerprint": harness_fingerprint()}
        # ELI5: keep baseline setup and verdict handling inside one failure-safe boundary.
        try:
            # ELI5: never run candidate code unless the operator explicitly enabled local validation.
            if not self.settings.allow_local_validation:
                # ELI5: refuse all baseline execution until the operator confirms a disposable environment.
                raise ValueError("Local execution requires ALLOW_LOCAL_VALIDATION=true in a disposable environment")
            # ELI5: application cases use their trusted production oracle instead of pytest mutants.
            if case.kind == "application":
                # ELI5: exercise the pinned source with the trusted oracle; only REGRESSION confirms weakness.
                status, evidence = self.run_application(case, case.baseline_sha)
                # ELI5: persist application status while marking test phases inapplicable.
                proof.update(application=status, provenance=evidence.get("result", {}).get("provenance", False),
                             application_evidence=evidence, normal="NOT_APPLICABLE", mutant="NOT_APPLICABLE")
                # ELI5: only the exact known baseline regression confirms this application case.
                if status == "REGRESSION":
                    # ELI5: only the exact known baseline crash confirms the case is worth repairing.
                    proof["outcome"] = "CONFIRMED"
                # ELI5: a passing or wrong-contract baseline cannot justify remediation work.
                elif status in {"PASS", "CONTRACT_FAILED"}:
                    # ELI5: a pre-fixed or wrong-contract baseline is not a confirmed case.
                    proof["outcome"] = "REJECTED"
                else:
                    # ELI5: missing infrastructure evidence cannot admit a paid session.
                    proof["outcome"] = "INFRA_ERROR"
            else:
                # ELI5: locate the designated test file for static suspicion hints.
                test_file = case.test_ids[0].split("::")[0]
                # ELI5: record source findings without treating them as runtime proof.
                proof["findings"] = detect(self.git("show", f"{case.baseline_sha}:{test_file}"), test_file)
                # ELI5: run clean and mutant phases against the pinned baseline.
                normal, mutant, evidence = self.run_both(case, case.baseline_sha)
                # ELI5: preserve both phase results and raw reports in the proof.
                proof.update(normal=normal, mutant=mutant, **evidence)
                # ELI5: matching passes confirm that the registered test misses its mutant.
                if normal == mutant == "PASS":
                    # ELI5: both passes confirm the known weak test shape for test-quality cases.
                    proof["outcome"] = "CONFIRMED"
                # ELI5: valid runs with the wrong relationship reject the weak-test claim.
                elif normal in {"PASS", "ASSERTION_FAILED"} and mutant in {"PASS", "ASSERTION_FAILED"}:
                    # ELI5: valid runs that do not match the expected weakness are rejected.
                    proof["outcome"] = "REJECTED"
                else:
                    # ELI5: malformed runs cannot produce baseline evidence.
                    proof["outcome"] = "INFRA_ERROR"
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            # ELI5: record a short safe reason while keeping infrastructure failures separate.
            proof.update(outcome="INFRA_ERROR", reason=str(exc)[:200])
        # ELI5: write the proof under the live data directory for later registry admission.
        destination = self.settings.data_dir.resolve() / "live/baselines" / f"{case.id}.json"
        # ELI5: create the parent folder before writing the JSON evidence.
        destination.parent.mkdir(parents=True, exist_ok=True)
        # ELI5: serialize a complete proof that another process can read after this command exits.
        destination.write_text(json.dumps(proof, indent=2))
        # ELI5: return the same proof for CLI output and tests.
        return proof

    def validate(self, job: Job, case: Case, candidate: Candidate) -> Evaluation:
        """Judge Devin's exact candidate SHA after baseline, ancestry, scope, and trusted acceptance gates."""
        # ELI5: fetch and evaluate one immutable candidate, returning a safe verdict on any setup failure.
        try:
            # ELI5: block validation unless the disposable local evaluator is explicitly enabled.
            if not self.settings.allow_local_validation:
                # ELI5: candidate code cannot run unless the operator enabled local validation.
                raise ValueError("Local evaluation has not been explicitly enabled")
            # ELI5: reject stale or missing baseline proof before fetching any candidate code.
            Registry(self.settings).evidence(case)
            # ELI5: fetch the PR head ref from the configured public fork without tags.
            self.git("fetch", "--no-tags", f"https://github.com/{job.repository}.git", f"refs/pull/{candidate.number}/head")
            # ELI5: compare FETCH_HEAD to the previously observed SHA to detect a moving PR.
            if self.git("rev-parse", "FETCH_HEAD") != candidate.sha:
                # ELI5: refuse to validate a PR whose fetched head differs from the observed head.
                return Evaluation("STALE_SHA", "PR changed while fetching the candidate")
            # ELI5: enforce ancestry, allowed paths, file type, and diff budget before execution.
            error = self.scope_error(case, candidate.sha)
            # ELI5: report scope rejection without opening the candidate worktree.
            if error:
                # ELI5: stop before candidate code runs when any scope gate fails.
                return Evaluation("SCOPE_REJECTED", error)
            # ELI5: application cases use the trusted production oracle instead of pytest mutants.
            if case.kind == "application":
                # ELI5: run the oracle against this exact fetched candidate SHA.
                status, evidence = self.run_application(case, candidate.sha)
                # ELI5: a trusted application PASS is the only application verification result.
                if status == "PASS":
                    # ELI5: a passing contract proves the candidate fix independently.
                    return Evaluation("VERIFIED", "Application contract passes on the candidate", application=status,
                                      evidence={"sha": candidate.sha, "acceptance_test": case.acceptance_test,
                                                "harness_fingerprint": harness_fingerprint(), "application": evidence})
                # ELI5: a regression or contract failure is an assessed repair failure.
                if status in {"REGRESSION", "CONTRACT_FAILED"}:
                    # ELI5: a surviving regression or wrong contract earns one bounded correction.
                    return Evaluation("APPLICATION_FAILED", "Candidate still exposes the application regression",
                                      application=status,
                                      evidence={"sha": candidate.sha, "acceptance_test": case.acceptance_test,
                                                "harness_fingerprint": harness_fingerprint(), "application": evidence})
                # ELI5: missing provenance or process evidence is infrastructure failure, not a repair verdict.
                return Evaluation("INFRA_ERROR", "Application acceptance oracle could not produce trusted evidence",
                                  application=status, evidence={"sha": candidate.sha, "application": evidence})
            # ELI5: test-quality cases require both clean and mutant phases on the same SHA.
            normal, mutant, evidence = self.run_both(case, candidate.sha)
            # ELI5: combine those phase classifications into the normal test verdict.
            result = compare_runs(normal, mutant)
            # ELI5: attach the immutable SHA, challenge, fingerprint, and raw phase evidence.
            result.evidence = {"sha": candidate.sha, "challenge": case.challenge,
                               "harness_fingerprint": harness_fingerprint(),
                               "normal": evidence["normal_evidence"], "mutant": evidence["mutant_evidence"]}
            # ELI5: return the complete verdict for orchestration and durable event storage.
            return result
        except (ValueError, OSError, subprocess.SubprocessError):
            # ELI5: hide setup and network details while clearly refusing to claim verification.
            return Evaluation("INFRA_ERROR", "Baseline, interpreter, worktree, or public-fork fetch unavailable")
