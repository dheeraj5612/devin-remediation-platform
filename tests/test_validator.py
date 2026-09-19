import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.cases import Registry
from app.validator import Validator, classify, compare_runs, run_process

NODE = "tests/test_example.py::test_contract"


def report(outcome="passed", assertion=False):
    return {"collected": [NODE], "exitcode": 0 if outcome == "passed" else 1, "controls": {NODE: True},
            "reports": [{"nodeid": NODE, "when": phase, "outcome": outcome if phase == "call" else "passed",
                         "assertion": assertion if phase == "call" else False, "xfail": False}
                        for phase in ["setup", "call", "teardown"]]}


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
    assert classify(data, [NODE]) == expected


def test_xfail_and_setup_failures_do_not_count_as_detection():
    xfail = report("failed", True)
    xfail["reports"][1]["xfail"] = True
    assert classify(xfail, [NODE]) == "NOT_VERIFIED"
    setup_error = report()
    setup_error["reports"][0].update(outcome="failed", assertion=True)
    setup_error["exitcode"] = 1
    assert classify(setup_error, [NODE]) == "INFRA_ERROR"


@pytest.mark.parametrize("normal,mutant,outcome", [
    ("PASS", "ASSERTION_FAILED", "VERIFIED"), ("PASS", "PASS", "REGRESSION_SURVIVED"),
    ("ASSERTION_FAILED", "ASSERTION_FAILED", "NORMAL_FAILED"),
    ("PASS", "INVALID_CONTROL", "INFRA_ERROR"), ("PASS", "NOT_VERIFIED", "INFRA_ERROR"),
    ("INFRA_ERROR", "ASSERTION_FAILED", "INFRA_ERROR"), ("PASS", "INFRA_ERROR", "INFRA_ERROR"),
])
def test_before_after_decision(normal, mutant, outcome):
    assert compare_runs(normal, mutant).outcome == outcome


@pytest.fixture
def repository(settings, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    target = root / "tests/test_example.py"
    target.parent.mkdir()
    target.write_text("def test_contract():\n    assert 1 == 1\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    case = next(iter(Registry(settings).cases.values())).model_copy(update={
        "baseline_sha": git("rev-parse", "HEAD"), "test_ids": [NODE], "allowed_paths": ["tests/test_example.py"],
    })
    settings.superset_repo_path = root
    settings.superset_python = Path(sys.executable)
    settings.allow_local_validation = True
    return Validator(settings), case, git, target


def test_scope_and_disposable_worktree(repository):
    validator, case, git, target = repository
    assert validator.scope_error(case, case.baseline_sha) == "Candidate contains no repair"
    target.write_text("def test_contract():\n    assert 2 == 2\n")
    git("commit", "-qam", "candidate")
    sha = git("rev-parse", "HEAD")
    assert validator.scope_error(case, sha) is None
    with validator.worktree(sha) as checkout:
        assert (checkout / "tests/test_example.py").read_text() == target.read_text()
        assert git("worktree", "list").count("detached HEAD") == 1
    assert not checkout.exists()
    assert "detached HEAD" not in git("worktree", "list")


@pytest.mark.parametrize("kind", ["dependency", "new-test", "delete", "executable", "symlink", "broad"])
def test_scope_rejects_unsafe_changes(repository, kind):
    validator, case, git, target = repository
    if kind == "dependency":
        (target.parent.parent / "requirements.txt").write_text("unrelated\n")
    elif kind == "new-test":
        (target.parent / "test_other.py").write_text("pass\n")
    elif kind == "delete":
        target.unlink()
    elif kind == "executable":
        target.chmod(0o755)
    elif kind == "symlink":
        target.unlink()
        target.symlink_to("/etc/passwd")
    else:
        target.write_text("# line\n" * 210)
    git("add", "-A")
    git("commit", "-qm", kind)
    assert validator.scope_error(case, git("rev-parse", "HEAD")) is not None


def test_missing_superset_environment_is_infrastructure_not_confirmation(repository):
    validator, case, git, target = repository
    proof = validator.baseline(case)
    assert proof["outcome"] == "INFRA_ERROR"
    assert proof["normal"] == "INFRA_ERROR"
    assert proof["mutant"] == "INFRA_ERROR"
    assert target.read_text() == "def test_contract():\n    assert 1 == 1\n"


def test_baseline_decision_and_saved_evidence(repository, monkeypatch):
    validator, case, git, target = repository
    monkeypatch.setattr(validator, "run_phase", lambda *args: ("PASS", {"unit_test_fixture": True}))
    proof = validator.baseline(case)
    assert proof["outcome"] == "CONFIRMED"
    saved = validator.settings.data_dir / "live/baselines" / f"{case.id}.json"
    assert json.loads(saved.read_text())["sha"] == case.baseline_sha
    monkeypatch.setattr(validator, "run_phase", lambda case, sha, phase:
                        ("PASS" if phase == "normal" else "ASSERTION_FAILED", {}))
    assert validator.baseline(case)["outcome"] == "REJECTED"


def test_timeout_terminates_subprocess(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, {"PATH": os.environ["PATH"]}, 1)


def test_candidate_environment_does_not_include_keys(repository, monkeypatch):
    validator, case, git, target = repository
    monkeypatch.setenv("DEVIN_API_KEY", "do-not-pass-this-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-pass-this-secret")
    def execute(command, cwd, env, timeout):
        assert "DEVIN_API_KEY" not in env and "GITHUB_TOKEN" not in env
        assert command[command.index("--drp-challenge") + 1] == case.challenge
        destination = Path(command[command.index("--drp-report") + 1])
        destination.write_text(json.dumps(report()))
        return subprocess.CompletedProcess(command, 0, "")
    monkeypatch.setattr("app.validator.run_process", execute)
    outcome, evidence = validator.run_phase(case, case.baseline_sha, "normal")
    assert outcome == "PASS"
    assert evidence["phase"] == "normal"
