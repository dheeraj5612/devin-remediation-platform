"""Validator tests for oracle classification, scope checks, and subprocess isolation.

ELI5: these tests make tiny temporary repositories so the validator can prove
what it ran, which files changed, and whether a candidate really detects a bug.
"""

import json  # Read the evidence file written by a baseline proof.
import os  # Pass only the minimal environment to the timeout subprocess.
import subprocess  # Build temporary Git repositories and inspect process failures.
import sys  # Use the current interpreter for isolated test commands.
from pathlib import Path  # Point the validator at temporary files and worktrees.

import pytest  # Provide fixtures, parameterization, and expected exceptions.

from app.cases import Registry  # Build a real registered case for scope checks.
from app.validator import Validator, classify, compare_runs, run_process  # Exercise the control-plane oracle.

NODE = "tests/test_example.py::test_contract"  # The one test ID the synthetic oracle is allowed to run.


def report(outcome="passed", assertion=False):
    """Build the smallest JSON-like pytest report used by classification tests."""

    return {"collected": [NODE], "exitcode": 0 if outcome == "passed" else 1, "controls": {NODE: True},  # Describe collection and the active control.
            "reports": [{"nodeid": NODE, "when": phase, "outcome": outcome if phase == "call" else "passed",  # Model setup, call, and teardown phases.
                         "assertion": assertion if phase == "call" else False, "xfail": False}  # Mark only the call assertion.
                        for phase in ["setup", "call", "teardown"]]}  # Return one report entry per phase.


@pytest.mark.parametrize("data,expected", [
    (report(), "PASS"), (report("failed", True), "ASSERTION_FAILED"),
    (report("failed", False), "INFRA_ERROR"), (report("skipped"), "NOT_VERIFIED"),
    ({**report(), "exitcode": 2}, "INFRA_ERROR"),
    ({**report(), "exitcode": 5}, "INFRA_ERROR"),
    ({**report(), "collected": []}, "INFRA_ERROR"),
    ({**report(), "collected": [NODE, NODE]}, "INFRA_ERROR"),
    ({**report(), "controls": {}}, "INVALID_CONTROL"),
    ({**report(), "controls": {NODE: False}}, "INVALID_CONTROL"),
    ({**report(), "reports": []}, "INFRA_ERROR"),
])
def test_classification_requires_real_call_and_active_control(data, expected):
    """Require a collected call and an active control before declaring detection."""

    assert classify(data, [NODE]) == expected  # Compare the report against the strict oracle verdict.


def test_xfail_and_setup_failures_do_not_count_as_detection():
    """Keep expected failures and setup errors out of the detection verdict."""

    xfail = report("failed", True)  # Build a call failure that pytest marked expected.
    xfail["reports"][1]["xfail"] = True  # Mark the call as an xfail instead of a real regression.
    assert classify(xfail, [NODE]) == "NOT_VERIFIED"  # An expected failure is not evidence.
    setup_error = report()  # Start with an otherwise healthy report.
    setup_error["reports"][0].update(outcome="failed", assertion=True)  # Fail during setup before the call.
    setup_error["exitcode"] = 1  # Reflect the process failure in the report.
    assert classify(setup_error, [NODE]) == "INFRA_ERROR"  # Setup failure is infrastructure noise.


@pytest.mark.parametrize("normal,mutant,outcome", [
    ("PASS", "ASSERTION_FAILED", "VERIFIED"), ("PASS", "PASS", "REGRESSION_SURVIVED"),
    ("ASSERTION_FAILED", "ASSERTION_FAILED", "NORMAL_FAILED"),
    ("PASS", "INVALID_CONTROL", "INFRA_ERROR"), ("PASS", "NOT_VERIFIED", "INFRA_ERROR"),
    ("INFRA_ERROR", "ASSERTION_FAILED", "INFRA_ERROR"), ("PASS", "INFRA_ERROR", "INFRA_ERROR"),
])
def test_before_after_decision(normal, mutant, outcome):
    """Map normal and controlled-regression phases to the final evaluation outcome."""

    assert compare_runs(normal, mutant).outcome == outcome  # Verify the two-phase decision table.


@pytest.fixture
def repository(settings, tmp_path):
    """Create a disposable Git repository with one approved test file."""

    root = tmp_path / "source"  # Keep candidate files outside the project checkout.
    root.mkdir()  # Create the temporary repository root.

    def git(*args):
        """Run one Git command against the disposable repository."""

        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()  # Return clean command output.

    git("init", "-q")  # Initialize the repository without printing progress.
    git("config", "user.email", "test@example.com")  # Supply an isolated commit identity.
    git("config", "user.name", "Test")  # Name the synthetic repository author.
    target = root / "tests/test_example.py"  # Place the approved test under its allowed path.
    target.parent.mkdir()  # Create the test directory.
    target.write_text("def test_contract():\n    assert 1 == 1\n")  # Seed the correct baseline behavior.
    git("add", ".")  # Stage the baseline fixture.
    git("commit", "-qm", "baseline")  # Record the pinned starting commit.
    case = next(iter(Registry(settings).cases.values())).model_copy(update={  # Adapt one registered case to this fixture.
        "baseline_sha": git("rev-parse", "HEAD"), "test_ids": [NODE], "allowed_paths": ["tests/test_example.py"],
    })  # Keep the validator's normal case contract while changing only repository details.
    settings.superset_repo_path = root  # Point validation at the disposable repository.
    settings.superset_python = Path(sys.executable)  # Use the current interpreter for test execution.
    settings.allow_local_validation = True  # Explicitly enable this controlled local fixture.
    return Validator(settings), case, git, target  # Expose the validator and fixture handles.


def test_scope_and_disposable_worktree(repository):
    """Accept one allowed test edit and remove its detached validation worktree afterward."""

    validator, case, git, target = repository  # Unpack the disposable validator fixture.
    assert validator.scope_error(case, case.baseline_sha) == "Candidate contains no repair"  # Require an actual candidate diff.
    target.write_text("def test_contract():\n    assert 2 == 2\n")  # Make an in-scope candidate edit.
    git("commit", "-qam", "candidate")  # Record the candidate commit.
    sha = git("rev-parse", "HEAD")  # Capture the exact candidate SHA.
    assert validator.scope_error(case, sha) is None
    with validator.worktree(sha) as checkout:  # Validate the candidate from a fresh detached checkout.
        assert (checkout / "tests/test_example.py").read_text() == target.read_text()  # The worktree has the candidate contents.
        assert git("worktree", "list").count("detached HEAD") == 1  # Exactly one temporary checkout is active.
    assert not checkout.exists()  # The context manager removes the checkout on exit.
    assert "detached HEAD" not in git("worktree", "list")  # No temporary worktree remains registered.


@pytest.mark.parametrize("kind", ["dependency", "new-test", "delete", "executable", "symlink", "broad"])
def test_scope_rejects_unsafe_changes(repository, kind):
    """Reject changes outside the approved file, deletion, executable, symlink, or size scope."""

    validator, case, git, target = repository  # Reuse a fresh repository for each unsafe variant.
    if kind == "dependency":
        (target.parent.parent / "requirements.txt").write_text("unrelated\n")  # Add an unapproved dependency file.
    elif kind == "new-test":
        (target.parent / "test_other.py").write_text("pass\n")  # Add an unapproved test file.
    elif kind == "delete":
        target.unlink()  # Remove the approved file entirely.
    elif kind == "executable":
        target.chmod(0o755)  # Change a file mode outside the allowed edit.
    elif kind == "symlink":
        target.unlink()  # Remove the regular file before replacing it.
        target.symlink_to("/etc/passwd")  # Point the candidate at an external file.
    else:
        target.write_text("# line\n" * 210)  # Exceed the bounded diff size.
    git("add", "-A")  # Stage the unsafe candidate shape.
    git("commit", "-qm", kind)  # Record it so scope checks inspect a real commit.
    assert validator.scope_error(case, git("rev-parse", "HEAD")) is not None  # Fail closed on every unsafe variant.


def test_missing_superset_environment_is_infrastructure_not_confirmation(repository):
    """Classify a missing local interpreter or fixture as infrastructure failure."""

    validator, case, git, target = repository  # Use the controlled repository and settings.
    proof = validator.baseline(case)  # Run the baseline proof without a prepared Superset environment.
    assert proof["outcome"] == "INFRA_ERROR"  # Infrastructure failures cannot confirm a weak test.
    assert proof["normal"] == "INFRA_ERROR"  # The normal phase did not execute.
    assert proof["mutant"] == "INFRA_ERROR"  # The controlled phase did not execute either.
    assert target.read_text() == "def test_contract():\n    assert 1 == 1\n"  # The validator leaves the baseline untouched.


def test_baseline_decision_and_saved_evidence(repository, monkeypatch):
    """Persist a confirmed weak baseline and reject one whose mutant survives."""

    validator, case, git, target = repository  # Reuse the disposable case.
    monkeypatch.setattr(validator, "run_phase", lambda *args: ("PASS", {"unit_test_fixture": True}))  # Fake a passing baseline phase.
    proof = validator.baseline(case)  # Write the resulting proof artifact.
    assert proof["outcome"] == "CONFIRMED"  # A passing baseline phase confirms the test is weak.
    saved = validator.settings.data_dir / "live/baselines" / f"{case.id}.json"  # Locate the persisted proof.
    assert json.loads(saved.read_text())["sha"] == case.baseline_sha  # Bind evidence to the exact baseline SHA.
    monkeypatch.setattr(validator, "run_phase", lambda case, sha, phase:  # Make the mutant phase pass instead of detecting.
                        ("PASS" if phase == "normal" else "ASSERTION_FAILED", {}))  # Preserve the normal/mutant contract.
    assert validator.baseline(case)["outcome"] == "REJECTED"  # A nonweak baseline must not be admitted.


def test_timeout_terminates_subprocess(tmp_path):
    """Terminate a child process that exceeds the validator's time budget."""

    with pytest.raises(subprocess.TimeoutExpired):  # The bounded runner should surface its timeout.
        run_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, {"PATH": os.environ["PATH"]}, 1)  # Keep the child environment minimal.


def test_candidate_environment_does_not_include_keys(repository, monkeypatch):
    """Keep provider credentials out of the environment passed to candidate code."""

    validator, case, git, target = repository  # Reuse the disposable candidate fixture.
    monkeypatch.setenv("DEVIN_API_KEY", "do-not-pass-this-secret")  # Put a sentinel only in the parent test process.
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-pass-this-secret")  # Put another sentinel in the parent process.

    def execute(command, cwd, env, timeout):
        """Inspect the sanitized subprocess request and emit a valid synthetic report."""

        assert "DEVIN_API_KEY" not in env and "GITHUB_TOKEN" not in env  # Candidate code receives no provider secrets.
        assert command[command.index("--drp-challenge") + 1] == case.challenge  # Preserve the registered challenge.
        destination = Path(command[command.index("--drp-report") + 1])  # Locate the report file requested by the runner.
        destination.write_text(json.dumps(report()))  # Return a passing control report.
        return subprocess.CompletedProcess(command, 0, "")  # Mimic a successful child process.

    monkeypatch.setattr("app.validator.run_process", execute)  # Replace only the process boundary.
    outcome, evidence = validator.run_phase(case, case.baseline_sha, "normal")  # Run the normal phase.
    assert outcome == "PASS"  # The synthetic report should classify as a pass.
    assert evidence["phase"] == "normal"  # Evidence records which phase produced the result.
