"""Trusted acceptance oracle for Superset's non-string report anchor crash.

ELI5: a report's ``extra.dashboard.anchor`` comes straight from the API caller.
At the pinned commit a list, dict, or number there makes validation crash with
a raw TypeError (an HTTP 500) instead of a clean validation error. This file is
kept in the control plane, so Devin cannot make the check pass by editing it.
It proves the candidate module is loaded, runs valid-anchor controls, then
checks the malformed-anchor contract.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# ELI5: one known tab id lets valid anchors pass and unknown ones be reported.
POSITION = {"TAB-1": {}}
# ELI5: anchors that must keep validating cleanly (plain id and JSON list of ids).
VALID_ANCHORS = ["TAB-1", json.dumps(["TAB-1"])]
# ELI5: user-supplied non-string anchors that must become one validation error each.
NON_STRING_ANCHORS: list[Any] = [[1, 2], {"tab": "TAB-1"}, 7]


def _candidate_root() -> Path:
    """Read the validator-provided checkout root and reject an absent provenance marker."""
    value = os.environ.get("DRP_CANDIDATE_ROOT")
    if not value:
        raise RuntimeError("DRP_CANDIDATE_ROOT is required")
    return Path(value).resolve()


def _validation_error_type() -> type[Exception]:
    """Return Superset's marshmallow validation error type at oracle runtime."""
    from marshmallow import ValidationError

    return ValidationError


@contextmanager
def _superset_app_context():
    """Initialize a disposable candidate application so report commands can import."""
    with tempfile.TemporaryDirectory(prefix="drp-superset-") as home:
        previous = {key: os.environ.get(key) for key in (
            "SUPERSET_CONFIG", "SUPERSET_HOME", "SUPERSET_SECRET_KEY", "SUPERSET_LOAD_EXAMPLES"
        )}
        os.environ["SUPERSET_CONFIG"] = "superset.config"
        os.environ["SUPERSET_HOME"] = home
        os.environ["SUPERSET_SECRET_KEY"] = "drp-local-acceptance-secret-1234567890"
        os.environ["SUPERSET_LOAD_EXAMPLES"] = "no"
        try:
            from superset.app import create_app

            app = create_app()
            with app.app_context():
                yield app
        finally:
            # Restore the parent environment even when app initialization fails.
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _validate(command_type: type, anchor: Any) -> list[Any]:
    """Run the production validator for one anchor against a fixed one-tab dashboard."""
    command = command_type()
    command._properties = {
        "extra": {"dashboard": {"anchor": anchor}},
        "dashboard": SimpleNamespace(position_json=json.dumps(POSITION)),
    }
    exceptions: list[Any] = []
    command._validate_report_extra(exceptions)
    return exceptions


def evaluate() -> dict[str, Any]:
    """Return PASS, exact REGRESSION, or CONTRACT_FAILED after proving candidate provenance."""
    root = _candidate_root()
    with _superset_app_context():
        module = importlib.import_module("superset.commands.report.base")
        module_path = Path(module.__file__).resolve()
        if not module_path.is_relative_to(root):
            raise RuntimeError(f"Superset module escaped candidate checkout: {module_path}")
        ValidationError = _validation_error_type()  # noqa: N806 - mirrors the production class name
        command_type = module.BaseReportScheduleCommand
        result = {"module": str(module_path), "provenance": True}

        # Controls: valid anchors must stay clean, and an unknown tab must still be reported.
        for anchor in VALID_ANCHORS:
            if _validate(command_type, anchor):
                return {**result, "outcome": "CONTRACT_FAILED", "reason": f"valid anchor {anchor!r} was rejected"}
        unknown = _validate(command_type, "TAB-MISSING")
        if len(unknown) != 1 or not isinstance(unknown[0], ValidationError):
            return {**result, "outcome": "CONTRACT_FAILED", "reason": "unknown tab id was not reported once"}

        crashed = []
        for anchor in NON_STRING_ANCHORS:
            try:
                errors = _validate(command_type, anchor)
            except TypeError:
                # The baseline bug is a raw TypeError escaping validation.
                crashed.append(type(anchor).__name__)
                continue
            except Exception as exc:  # the validator records application behavior as a repair failure
                return {**result, "outcome": "CONTRACT_FAILED",
                        "reason": f"{type(anchor).__name__} anchor raised {type(exc).__name__}"}
            if (len(errors) != 1 or not isinstance(errors[0], ValidationError)
                    or getattr(errors[0], "field_name", None) != "extra"):
                return {**result, "outcome": "CONTRACT_FAILED",
                        "reason": f"{type(anchor).__name__} anchor did not produce one extra ValidationError"}
        if len(crashed) == len(NON_STRING_ANCHORS):
            return {**result, "outcome": "REGRESSION", "crashed": crashed}
        if crashed:
            return {**result, "outcome": "CONTRACT_FAILED", "reason": f"still crashes for {', '.join(crashed)}"}
        return {**result, "outcome": "PASS"}


def main() -> int:
    """Print one machine-readable result and return a nonzero code only for infrastructure failure."""
    try:
        result = evaluate()
    except Exception as exc:  # pragma: no cover - exercised by the validator's infra handling
        result = {"outcome": "INFRA_ERROR", "reason": str(exc)[:240], "provenance": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["outcome"] in {"PASS", "REGRESSION", "CONTRACT_FAILED"} else 2


if __name__ == "__main__":
    sys.exit(main())
