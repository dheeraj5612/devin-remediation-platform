"""Trusted acceptance oracle for Superset's malformed import YAML path.

ELI5: this file is kept in the control plane, so Devin cannot make the test
pass by editing the checker. It proves that the real candidate module is
loaded, exercises one valid config as a control, then checks the malformed
config contract that the application must provide.
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


# ELI5: keep the malformed input name that the baseline error must mention.
MALFORMED_FILE = "databases/malformed.yaml"
# ELI5: keep a separate valid mapping that proves ordinary loading still works.
VALID_FILE = "datasets/valid.yaml"


# ELI5: this fake query removes database dependence while preserving the production call shape.
class _EmptyQuery:
    """Return no stored secrets while preserving the query chain used by Superset."""

    def all(self) -> list[tuple[Any, ...]]:
        """Return an empty result set so the oracle never needs a live database."""
        # ELI5: no stored rows means no secrets or external state can affect the result.
        return []


# ELI5: this fake session supplies only the query method load_configs needs.
class _EmptySession:
    """Provide only the SQLAlchemy session behavior needed by ``load_configs``."""

    def query(self, *_columns: Any) -> _EmptyQuery:
        """Return a query object whose deterministic result contains no rows."""
        # ELI5: ignore selected columns and return the same empty query for every lookup.
        return _EmptyQuery()


# ELI5: this fake schema proves the valid control reached schema loading.
class _RecordingSchema:
    """Accept a valid mapping and remember that the normal path really ran."""

    def __init__(self) -> None:
        """Start an empty record so the valid control can prove schema loading occurred."""
        # ELI5: keep each mapping passed to the schema in call order.
        self.loaded: list[dict[str, Any]] = []

    def load(self, config: dict[str, Any]) -> dict[str, Any]:
        """Record and return a mapping without depending on Superset's full schema stack."""
        # ELI5: copy the mapping so later production mutation cannot erase the control record.
        self.loaded.append(dict(config))
        # ELI5: return the original mapping just like a successful schema load.
        return config


def _candidate_root() -> Path:
    """Read the validator-provided checkout root and reject an absent provenance marker."""
    # ELI5: read the root injected by the validator, never infer it from the oracle location.
    value = os.environ.get("DRP_CANDIDATE_ROOT")
    # ELI5: fail before importing candidate code when the validator omitted its provenance root.
    if not value:
        # ELI5: explain the missing provenance input without guessing a checkout location.
        raise RuntimeError("DRP_CANDIDATE_ROOT is required")
    # ELI5: normalize the path before comparing imported module provenance.
    return Path(value).resolve()


@contextmanager
def _superset_app_context():
    """Initialize the candidate application before importing encrypted models.

    ELI5: Superset's model classes need the Flask app's encryption settings while
    Python imports them, so the oracle gives the checkout a disposable app and
    metadata directory before importing the production utility.
    """
    # ELI5: use a temporary metadata home so this acceptance check cannot read or write a user's database.
    with tempfile.TemporaryDirectory(prefix="drp-superset-") as home:
        # ELI5: remember environment values so the helper is safe for in-process unit tests.
        previous = {key: os.environ.get(key) for key in (
            "SUPERSET_CONFIG", "SUPERSET_HOME", "SUPERSET_SECRET_KEY", "SUPERSET_LOAD_EXAMPLES"
        )}
        # ELI5: select the candidate's stock config rather than an operator's hidden module.
        os.environ["SUPERSET_CONFIG"] = "superset.config"
        # ELI5: point Superset's SQLite metadata database at the temporary home.
        os.environ["SUPERSET_HOME"] = home
        # ELI5: use a disposable key only to initialize encrypted model fields.
        os.environ["SUPERSET_SECRET_KEY"] = "drp-local-acceptance-secret-1234567890"
        # ELI5: avoid loading optional example data during this focused oracle.
        os.environ["SUPERSET_LOAD_EXAMPLES"] = "no"
        # ELI5: initialize the candidate app and restore the caller environment in finally.
        try:
            # Importing the app factory first initializes encrypted SQLAlchemy fields.
            from superset.app import create_app

            # ELI5: initialize encryption, extensions, and model metadata before importing the target module.
            app = create_app()
            # ELI5: keep Superset's application context active while loading its production module.
            with app.app_context():
                # ELI5: keep current_app active while the oracle imports and calls production code.
                yield app
        finally:
            # Restore the parent environment even when app initialization fails.
            for key, value in previous.items():
                # ELI5: remove a setting that did not exist before the oracle started.
                if value is None:
                    # ELI5: remove variables that were absent before this helper ran.
                    os.environ.pop(key, None)
                else:
                    # ELI5: restore the caller's original environment value.
                    os.environ[key] = value


def _load_configs(module: Any, contents: dict[str, str], schema: _RecordingSchema,
                  exceptions: list[Any]) -> dict[str, Any]:
    """Call the production function with explicit empty secret maps and no hidden fixtures."""
    # ELI5: supply both schema prefixes and five empty secret maps exactly as production expects.
    return module.load_configs(
        contents,
        {"datasets/": schema, "databases/": schema},
        {},
        exceptions,
        {},
        {},
        {},
        {},
    )


def evaluate() -> dict[str, Any]:
    """Return PASS, exact REGRESSION, or CONTRACT_FAILED after proving candidate provenance."""
    # ELI5: identify the candidate root before importing any production module.
    root = _candidate_root()
    # ELI5: initialize the candidate app so encrypted model imports have a valid Flask context.
    with _superset_app_context():
        # ELI5: import the production utility only after app initialization.
        module = importlib.import_module("superset.commands.importers.v1.utils")
        # ELI5: resolve the imported file for the provenance check and evidence record.
        module_path = Path(module.__file__).resolve()
        # ELI5: a successful import from site-packages would make this result unrelated to the candidate SHA.
        if not module_path.is_relative_to(root):
            # ELI5: stop when imports escape the detached candidate checkout.
            raise RuntimeError(f"Superset module escaped candidate checkout: {module_path}")

        # ELI5: save the real database extension before replacing it with deterministic fakes.
        original_db = module.db
        # ELI5: replace database access so all secret lookups return empty lists.
        module.db = SimpleNamespace(session=_EmptySession())
        # ELI5: run both valid and malformed controls while the fake database is installed.
        try:
            # ELI5: run the valid mapping first to prove schema loading is reachable.
            valid_schema = _RecordingSchema()
            # ELI5: collect any validation errors from the valid control separately.
            valid_exceptions: list[Any] = []
            # ELI5: call the candidate's real loader with one valid dataset mapping.
            valid_configs = _load_configs(module, {VALID_FILE: "key: value\n"}, valid_schema, valid_exceptions)
            # ELI5: require exactly one loaded mapping and one schema call, not a shortcut return.
            if valid_configs != {VALID_FILE: {"key": "value"}} or valid_schema.loaded != [{"key": "value"}]:
                # ELI5: reject a valid-control shortcut that did not load exactly one mapping.
                return {"outcome": "CONTRACT_FAILED", "reason": "valid YAML control path did not load exactly one mapping",
                        "module": str(module_path), "provenance": True}
            # ELI5: a valid mapping must not produce a validation exception.
            if valid_exceptions:
                # ELI5: even a valid input producing an exception means the candidate contract is wrong.
                return {"outcome": "CONTRACT_FAILED", "reason": "valid YAML control path produced an exception",
                        "module": str(module_path), "provenance": True}

            # ELI5: collect validation errors from the malformed file independently of the valid control.
            malformed_exceptions: list[Any] = []
            # ELI5: record schema calls so malformed input cannot be mistaken for a valid load.
            malformed_schema = _RecordingSchema()
            # ELI5: run the malformed document and classify its expected error separately.
            try:
                # ELI5: call the same production loader with one syntactically broken YAML document.
                malformed_configs = _load_configs(
                    module,
                    {MALFORMED_FILE: 'key: "unterminated string\n'},
                    malformed_schema,
                    malformed_exceptions,
                )
            except UnboundLocalError as exc:
                # The baseline bug is specifically a diagnostic crash before ``config`` is bound.
                # ELI5: distinguish the exact known unbound-local message from unrelated crashes.
                if "config" not in str(exc):
                    # ELI5: report an unrecognized crash as a contract failure, not the known regression.
                    return {"outcome": "CONTRACT_FAILED", "reason": f"unexpected unbound-local failure: {exc}",
                            "module": str(module_path), "provenance": True}
                # ELI5: report the exact baseline defect while preserving candidate provenance.
                return {"outcome": "REGRESSION", "module": str(module_path), "provenance": True}
            except Exception as exc:  # pragma: no cover - the validator records application behavior as a repair failure
                # ELI5: classify another application exception as a contract failure, not infrastructure.
                return {"outcome": "CONTRACT_FAILED", "reason": f"malformed YAML raised an unexpected exception: {exc}",
                        "module": str(module_path), "provenance": True}

            # ELI5: read the one expected file-scoped validation message, if present.
            messages = str(malformed_exceptions[0].messages) if len(malformed_exceptions) == 1 else ""
            # ELI5: require one file-scoped error, no partial config or schema load, and the filename.
            if malformed_configs != {} or malformed_schema.loaded or MALFORMED_FILE not in messages:
                # ELI5: reject malformed-input results that do not match the application contract.
                return {"outcome": "CONTRACT_FAILED", "reason": "malformed YAML did not produce one file-scoped validation error",
                        "module": str(module_path), "provenance": True}
            # ELI5: the fixed candidate met both valid-control and malformed-input contracts.
            return {"outcome": "PASS", "module": str(module_path), "provenance": True}
        finally:
            # ELI5: put the original database extension back before leaving the app context.
            module.db = original_db


def main() -> int:
    """Print one machine-readable result and return a nonzero code only for infrastructure failure."""
    # ELI5: convert evaluator setup errors into explicit infrastructure output.
    try:
        # ELI5: run the trusted evaluator and keep its structured outcome.
        result = evaluate()
    except Exception as exc:  # pragma: no cover - exercised by the validator's infra handling
        # ELI5: convert setup/import failures into explicit infrastructure JSON and a nonzero exit.
        result = {"outcome": "INFRA_ERROR", "reason": str(exc)[:240], "provenance": False}
    # ELI5: print exactly one final JSON line after any diagnostic logs.
    print(json.dumps(result, sort_keys=True))
    # ELI5: expected assessed outcomes exit cleanly; only infrastructure failures use code two.
    return 0 if result["outcome"] in {"PASS", "REGRESSION", "CONTRACT_FAILED"} else 2


# ELI5: use the evaluator's return code when this oracle runs as a script.
if __name__ == "__main__":
    # ELI5: make command-line execution use the same status code as the evaluator result.
    sys.exit(main())
