"""Control-plane tests for the trusted non-string report anchor oracle."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.cases import Registry
from evals.application_cases import report_anchor_type as acceptance


class FakeValidationError(Exception):
    """Stand in for marshmallow.ValidationError without adding Superset to control-plane tests."""

    def __init__(self, message, field_name):
        """Keep the field name the trusted oracle reads."""
        super().__init__(message)
        self.field_name = field_name


def _command_type(mode):
    """Build a stand-in report command whose anchor validation follows one scripted behavior."""

    class Command:
        """Mimic the production command's `_properties` + `_validate_report_extra` shape."""

        def _validate_report_extra(self, exceptions):
            """Validate the anchor as the selected baseline, partial, bad, or fixed implementation."""
            anchor = self._properties["extra"]["dashboard"]["anchor"]
            positions = json.loads(self._properties["dashboard"].position_json)
            if isinstance(anchor, str):
                ids = json.loads(anchor) if anchor.startswith("[") else [anchor]
                if mode == "valid_bad" or set(ids) - set(positions):
                    exceptions.append(FakeValidationError("Invalid tab ids", "extra"))
                return
            if mode == "regression" or (mode == "partial" and not isinstance(anchor, int)):
                raise TypeError("unhashable type: 'list'")
            if mode == "wrong_field":
                exceptions.append(FakeValidationError("Invalid tab ids", "name"))
                return
            if mode == "generic_error":
                raise ValueError("boom")
            exceptions.append(FakeValidationError("Invalid tab ids", "extra"))

    return Command


@pytest.mark.parametrize("mode,expected", [("regression", "REGRESSION"), ("fixed", "PASS"),
                                           ("partial", "CONTRACT_FAILED"), ("valid_bad", "CONTRACT_FAILED"),
                                           ("wrong_field", "CONTRACT_FAILED"), ("generic_error", "CONTRACT_FAILED")])
def test_oracle_distinguishes_exact_crash_partial_fix_and_real_fix(monkeypatch, tmp_path, mode, expected):
    """Only the full crash is the baseline, and only one extra-field error per bad anchor passes."""
    monkeypatch.setattr(acceptance, "_superset_app_context", lambda: nullcontext())
    monkeypatch.setattr(acceptance, "_validation_error_type", lambda: FakeValidationError)
    module = SimpleNamespace(__file__=str(tmp_path / "superset/commands/report/base.py"),
                             BaseReportScheduleCommand=_command_type(mode))
    monkeypatch.setenv("DRP_CANDIDATE_ROOT", str(tmp_path))
    monkeypatch.setattr(acceptance.importlib, "import_module", lambda _name: module)
    assert acceptance.evaluate()["outcome"] == expected


def test_oracle_rejects_module_outside_candidate(monkeypatch, tmp_path):
    """A module imported from anywhere but the candidate checkout cannot produce a verdict."""
    monkeypatch.setattr(acceptance, "_superset_app_context", lambda: nullcontext())
    module = SimpleNamespace(__file__="/elsewhere/superset/commands/report/base.py",
                             BaseReportScheduleCommand=_command_type("fixed"))
    monkeypatch.setenv("DRP_CANDIDATE_ROOT", str(tmp_path))
    monkeypatch.setattr(acceptance.importlib, "import_module", lambda _name: module)
    with pytest.raises(RuntimeError, match="escaped"):
        acceptance.evaluate()


def test_report_anchor_case_is_scoped_to_one_production_file(settings):
    """Devin may change only the report command, and the oracle lives in the control plane."""
    case = Registry(settings).cases["report-anchor-non-string"]
    assert case.kind == "application"
    assert case.allowed_paths == ["superset/commands/report/base.py"]
    assert case.acceptance_test == "evals/application_cases/report_anchor_type.py"
