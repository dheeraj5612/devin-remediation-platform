import copy
import json

import pytest

from app.cases import baseline_evidence
from app.github import Candidate
from app.validator import baseline_confirmed, InfrastructureError, judge, Validator


def report(case, exit_code=0, failure=""):
    phases = []
    for index, node in enumerate(case.test_ids):
        for when in ("setup", "call", "teardown"):
            failed = index == 0 and when == "call" and exit_code == 1
            phases.append({"nodeid": node, "when": when, "outcome": "failed" if failed else "passed", "xfail": False,
                           "failure": failure if failed else ""})
    return {"exit_code": exit_code, "collected": case.test_ids, "controls": dict.fromkeys(case.test_ids, True),
            "collection_errors": [], "phases": phases}


def test_baseline_and_candidate_semantics(cases):
    case = cases["histogram-invalid"]
    normal = report(case)
    assert baseline_confirmed(case, normal, normal)
    assert judge(case, normal, normal).outcome == "REPAIR"
    assert judge(case, normal, report(case, 1, "assertion")).outcome == "VERIFIED"
    assert judge(case, normal, report(case, 1, "missing_expected_exception")).outcome == "VERIFIED"
    assert judge(case, normal, report(case, 1, "")).outcome == "INFRA"


@pytest.mark.parametrize("problem", ["inactive", "missing", "collection", "skip", "xfail", "setup", "exit", "duplicate"])
def test_invalid_runs_never_verify(cases, problem):
    case = cases["schema-no-engine"]
    normal, mutant = report(case), report(case, 1, "assertion")
    node = case.test_ids[0]
    if problem == "inactive": mutant["controls"][node] = False
    if problem == "missing": mutant["collected"] = []
    if problem == "collection": mutant["collection_errors"] = ["ImportError"]
    if problem == "skip": mutant["phases"][1]["outcome"] = "skipped"
    if problem == "xfail": mutant["phases"][1]["xfail"] = True
    if problem == "setup": mutant["phases"][0]["outcome"] = "failed"
    if problem == "exit": mutant["exit_code"] = 2
    if problem == "duplicate": mutant["phases"].append(copy.deepcopy(mutant["phases"][1]))
    assert judge(case, normal, mutant).outcome == "INFRA"
    assert not baseline_confirmed(case, normal, mutant)


def test_baseline_gate_binds_challenge_and_sha(cases, settings):
    case = cases["schema-no-engine"]
    with pytest.raises(ValueError, match="not confirmed"):
        baseline_evidence(case, settings.data_dir)
    folder = settings.data_dir / "baselines"
    folder.mkdir()
    path = folder / f"{case.id}.json"
    evidence = {"fingerprint": case.fingerprint, "baseline_sha": case.baseline_sha,
                "normal": report(case), "mutant": report(case)}
    path.write_text(json.dumps(evidence))
    assert baseline_evidence(case, settings.data_dir) == evidence
    evidence["fingerprint"] = "different"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="does not match"):
        baseline_evidence(case, settings.data_dir)


def test_docker_unavailable_is_infrastructure(settings, cases, monkeypatch):
    validator = Validator(settings)
    monkeypatch.setattr(validator, "docker", lambda *_: (_ for _ in ()).throw(InfrastructureError("No Docker")))
    result = validator.evaluate(cases["schema-no-engine"], Candidate(1, None, "a" * 40, {}))
    assert result.outcome == "INFRA"


def test_docker_isolation_and_cleanup(settings, cases, monkeypatch):
    validator, calls = Validator(settings), []
    case = cases["schema-no-engine"]

    def docker(args, timeout=30):
        calls.append(args)
        if args[0] == "image": return case.baseline_sha
        if args[0] == "create": return "container-id"
        if args[0] == "start": raise InfrastructureError("Timeout")
        if args[0] == "inspect": return json.dumps({"Running": True})
        return ""

    monkeypatch.setattr(validator, "docker", docker)
    with pytest.raises(InfrastructureError, match="Timeout"):
        validator.run(case, True)
    create = next(args for args in calls if args[0] == "create")
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create and "no-new-privileges" in create
    assert not any("TOKEN" in arg or "docker.sock" in arg for arg in create)
    assert ["rm", "-f", "-v", "container-id"] in calls
    assert calls[-1][:3] == ["image", "rm", "-f"]


@pytest.mark.parametrize("exception", ["ImportError", "ModuleNotFoundError"])
def test_lazy_import_failure_is_not_coding_feedback(cases, exception):
    case = cases["schema-no-engine"]
    normal = report(case, 1)
    normal["phases"][1]["exception"] = exception
    assert judge(case, normal, report(case)).outcome == "INFRA"
