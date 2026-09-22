"""Application-case schema and validator tests with no Superset network or Devin calls."""

import json
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cases import Case, Registry, ROOT, harness_fingerprint
from app.github import Candidate
from app.validator import Validator
from evals.application_cases import import_unparseable_yaml as acceptance


def application_case(settings):
    """Return the configured source-backed case used by the focused tests."""
    return Registry(settings).cases["import-unparseable-yaml"]


def test_application_case_is_production_scoped_and_source_pinned(settings):
    """The YAML entry names the exact defect parent and keeps the oracle outside candidate scope."""
    case = application_case(settings)
    assert case.kind == "application"
    assert case.baseline_sha == "dedfe23a805151decca6deaf15b032e040e42e82"
    assert case.target_branch == "remediation-import-yaml"
    assert case.allowed_paths == ["superset/commands/importers/v1/utils.py"]
    assert case.acceptance_test == "evals/application_cases/import_unparseable_yaml.py"


def test_application_case_rejects_oracle_outside_trusted_directory(settings):
    """A YAML typo cannot move the trusted evaluator into candidate-controlled code."""
    data = application_case(settings).model_dump()
    data["acceptance_test"] = "evals/challenges.py"
    with pytest.raises(ValueError, match="acceptance_test"):
        Case.model_validate(data)


def test_application_baseline_requires_controlled_regression(settings, monkeypatch):
    """Baseline confirmation accepts the known regression and rejects a pre-fixed checkout."""
    settings.allow_local_validation = True
    validator = Validator(settings)
    case = application_case(settings)
    monkeypatch.setattr(validator, "run_application", lambda *_args: ("REGRESSION", {
        "result": {"provenance": True}, "stdout": '{"outcome":"REGRESSION"}',
    }))
    assert validator.baseline(case)["outcome"] == "CONFIRMED"
    monkeypatch.setattr(validator, "run_application", lambda *_args: ("PASS", {
        "result": {"provenance": True}, "stdout": '{"outcome":"PASS"}',
    }))
    assert validator.baseline(case)["outcome"] == "REJECTED"


@pytest.mark.parametrize("status,expected", [("PASS", "VERIFIED"), ("REGRESSION", "APPLICATION_FAILED"),
                                               ("CONTRACT_FAILED", "APPLICATION_FAILED")])
def test_application_validation_distinguishes_fix_from_surviving_bug(settings, monkeypatch, status, expected):
    """Candidate validation maps the trusted oracle result to a verifiable or repairable verdict."""
    settings.allow_local_validation = True
    validator = Validator(settings)
    case = application_case(settings)
    candidate = Candidate(7, "a" * 40)
    job = SimpleNamespace(repository=settings.github_repository)
    monkeypatch.setattr(Registry, "evidence", lambda *_args: {"outcome": "CONFIRMED"})
    monkeypatch.setattr(validator, "git", lambda *args: candidate.sha if args and args[0] == "rev-parse" else "")
    monkeypatch.setattr(validator, "scope_error", lambda *_args: None)
    monkeypatch.setattr(validator, "run_application", lambda *_args: (status, {
        "result": {"provenance": True}, "stdout": json.dumps({"outcome": status}),
    }))
    result = validator.validate(job, case, candidate)
    assert result.outcome == expected
    assert result.application == status
    # ELI5: both exact regressions and other trusted contract failures deserve one bounded correction.
    assert result.repair_failure is (status in {"REGRESSION", "CONTRACT_FAILED"})


def test_application_oracle_requires_provenance_and_clean_checkout(settings, monkeypatch, tmp_path):
    """The runner rejects a nontrusted result even when a subprocess exits successfully."""
    settings.allow_local_validation = True
    validator = Validator(settings)
    case = application_case(settings)

    @contextmanager
    def fake_worktree(_sha):
        """Yield a disposable path without touching a real Superset checkout."""
        yield tmp_path

    monkeypatch.setattr(validator, "worktree", fake_worktree)
    monkeypatch.setattr("app.validator.run_process", lambda command, cwd, env, timeout:
                        subprocess.CompletedProcess(command, 0, '{"outcome":"PASS","provenance":false}\n'))
    monkeypatch.setattr("app.validator.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0))
    status, evidence = validator.run_application(case, case.baseline_sha)
    assert status == "INFRA_ERROR"
    assert "provenance" in evidence["reason"]


def test_application_oracle_keeps_contract_failure_assessed(settings, monkeypatch, tmp_path):
    """A trusted import with a wrong behavior result remains a repair failure, not infrastructure."""
    settings.allow_local_validation = True
    validator = Validator(settings)
    case = application_case(settings)

    @contextmanager
    def fake_worktree(_sha):
        """Yield a disposable path for the controlled subprocess result."""
        yield tmp_path

    monkeypatch.setattr(validator, "worktree", fake_worktree)
    monkeypatch.setattr("app.validator.run_process", lambda command, cwd, env, timeout:
                        subprocess.CompletedProcess(command, 0, '{"outcome":"CONTRACT_FAILED","provenance":true}\n'))
    monkeypatch.setattr("app.validator.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0))
    status, evidence = validator.run_application(case, case.baseline_sha)
    assert status == "CONTRACT_FAILED"
    assert evidence["result"]["provenance"] is True


@pytest.mark.parametrize("mode,expected", [("regression", "REGRESSION"), ("valid_bad", "CONTRACT_FAILED"),
                                             ("malformed_bad", "CONTRACT_FAILED")])
def test_trusted_oracle_distinguishes_exact_regression_from_contract_failures(monkeypatch, tmp_path, mode, expected):
    """The oracle only confirms the known crash and assesses wrong valid or malformed behavior."""
    # ELI5: each stand-in result exercises one branch of the real control-plane classifier.
    # The stand-in module has no real Superset checkout, so skip app bootstrap in this unit test.
    monkeypatch.setattr(acceptance, "_superset_app_context", lambda: nullcontext())
    module_path = tmp_path / "superset/commands/importers/v1/utils.py"
    fake_module = SimpleNamespace(__file__=str(module_path), db=SimpleNamespace())

    def fake_load_configs(contents, schemas, _passwords, exceptions, *_rest):
        """Return the selected deterministic behavior for the production-function stand-in."""
        if acceptance.VALID_FILE in contents:
            if mode == "valid_bad":
                return {}
            schemas["datasets/"].load({"key": "value"})
            return {acceptance.VALID_FILE: {"key": "value"}}
        if mode == "regression":
            raise UnboundLocalError("cannot access local variable 'config'")
        if mode == "malformed_bad":
            return {acceptance.MALFORMED_FILE: {"unexpected": True}}
        exceptions.append(SimpleNamespace(messages={acceptance.MALFORMED_FILE: "Not a valid YAML file"}))
        return {}

    fake_module.load_configs = fake_load_configs
    monkeypatch.setenv("DRP_CANDIDATE_ROOT", str(tmp_path))
    monkeypatch.setattr(acceptance.importlib, "import_module", lambda _name: fake_module)
    assert acceptance.evaluate()["outcome"] == expected


def test_application_oracle_source_has_valid_control_and_baseline_guard():
    """The trusted script contains both the valid-path control and exact baseline crash guard."""
    source = Path("evals/application_cases/import_unparseable_yaml.py").read_text()
    assert "VALID_FILE" in source and "MALFORMED_FILE" in source
    assert "UnboundLocalError" in source and "provenance" in source


def test_harness_fingerprint_covers_registry_and_evidence_gates(monkeypatch):
    """Changing the registry code must stale old baseline evidence."""
    before = harness_fingerprint()
    target = (ROOT / "app/cases.py").resolve()
    original_read_bytes = Path.read_bytes

    def changed_registry_bytes(path):
        """Pretend only the registry source changed without touching the checkout."""
        # ELI5: append a harmless marker only when the fingerprint reads its registry file.
        data = original_read_bytes(path)
        return data + b"\n# simulated registry gate change\n" if path.resolve() == target else data

    monkeypatch.setattr(Path, "read_bytes", changed_registry_bytes)
    assert harness_fingerprint() != before
