"""External pytest plugin holding the two registered regressions ("challenges").

ELI5: this file is the referee's rulebook, and it lives *outside* the Superset
checkout so Devin can never edit it. The validator loads it with
`pytest -p evals.challenges` and picks one challenge and one phase:

    --drp-phase normal  -> production code untouched
    --drp-phase mutant  -> monkeypatch the pre-registered bug back in

Before the designated test runs, a *positive control* proves the phase is really
active (clean code rejects bad input; mutant code accepts it). If the control
did not run, the whole run is worthless and the validator says INVALID_CONTROL.
The plugin also records, per test, whether each of setup/call/teardown passed
and whether a failure was a real assertion, then writes one JSON report file.
"""

import functools
import importlib
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any
from collections.abc import Generator

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--drp-challenge", required=True)
    parser.addoption("--drp-phase", choices=["normal", "mutant"], required=True)
    parser.addoption("--drp-report", required=True)


def pytest_configure(config: pytest.Config) -> None:
    # Everything we learn during the run accumulates here and is dumped at the end.
    config._drp = {"collected": [], "reports": [], "controls": {}, "provenance": {},
                   "environment": {"python": sys.version, "pytest": version("pytest")}}


def pytest_collection_finish(session: pytest.Session) -> None:
    session.config._drp["collected"] = [item.nodeid for item in session.items]  # validator checks exact IDs


def histogram_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
    """Regression: `histogram()` silently coerces non-numeric values to 0 instead of raising ValueError."""
    from pandas import DataFrame
    from pandas.testing import assert_frame_equal

    module = importlib.import_module("superset.utils.pandas_postprocessing.histogram")
    original = module.to_numeric
    if mutant:
        def accept_invalid(values: Any, **kwargs: Any) -> Any:
            return original(values, **kwargs).fillna(0)  # NaN from "not-a-number" becomes 0: the bug
        monkeypatch.setattr(module, "to_numeric", accept_invalid)

    # Both phases: numeric strings like "10" are valid input and must equal real numbers.
    numeric = module.histogram(DataFrame({"value": [1, 10]}), "value", [], 2)
    strings = module.histogram(DataFrame({"value": ["1", "10"]}), "value", [], 2)
    assert_frame_equal(numeric, strings)
    # Positive control: truly invalid strings must raise on clean code and be accepted under the mutant.
    invalid = DataFrame({"value": ["not-a-number", "also-invalid"]})
    if mutant:
        result = module.histogram(invalid, "value", [], 2)
        assert int(result.to_numpy().sum()) == 2
    else:
        with pytest.raises(ValueError, match="^Column 'value' contains non-numeric values$"):
            module.histogram(invalid, "value", [], 2)


def schema_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
    """Regression: dynamic-form database parameters without an engine are accepted instead of rejected."""
    from marshmallow import fields, Schema, ValidationError

    from superset.databases.schemas import DatabaseParametersSchemaMixin
    from superset.models.core import ConfigurationMethod

    original = DatabaseParametersSchemaMixin.build_sqlalchemy_uri
    if mutant:
        @functools.wraps(original)
        def accept_missing_engine(self: Any, data: dict, **kwargs: Any) -> dict:
            parameters = data.get("parameters", {})
            missing = not (data.get("engine") or parameters.get("engine") or data.get("backend"))
            if data.get("configuration_method") == ConfigurationMethod.DYNAMIC_FORM and missing:
                result = dict(data)  # the bug: invent a URI instead of raising ValidationError
                result.pop("parameters", None)
                result["sqlalchemy_uri"] = "sqlite://"
                return result
            return original(self, data, **kwargs)
        monkeypatch.setattr(DatabaseParametersSchemaMixin, "build_sqlalchemy_uri", accept_missing_engine)

    class ProbeSchema(DatabaseParametersSchemaMixin, Schema):
        sqlalchemy_uri = fields.String()

    payload = {"configuration_method": ConfigurationMethod.DYNAMIC_FORM,
               "parameters": {"username": "username", "password": "password", "host": "localhost",
                              "port": 12345, "database": "dbname"}}
    if mutant:
        assert ProbeSchema().load(payload)["sqlalchemy_uri"] == "sqlite://"
    else:
        with pytest.raises(ValidationError) as error:
            ProbeSchema().load(payload)
        assert error.value.messages == {
            "_schema": ["An engine must be specified when passing individual parameters to a database."]
        }


# challenge name -> (production module under test, control function)
CHALLENGES = {
    "histogram-accept-invalid": ("superset.utils.pandas_postprocessing.histogram", histogram_control),
    "schema-accept-missing-engine": ("superset.databases.schemas", schema_control),
}


@pytest.fixture(autouse=True)
def registered_challenge(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs before every collected test: verify provenance, arm the phase, run the control, record it."""
    module_name, control = CHALLENGES[request.config.getoption("--drp-challenge")]
    module = importlib.import_module(module_name)
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(Path(request.config.rootpath).resolve()):
        # A globally installed Superset would make the worktree checkout meaningless.
        raise RuntimeError("Validation imported production code outside the candidate worktree")
    request.config._drp["provenance"][request.node.nodeid] = str(origin)
    control(monkeypatch, request.config.getoption("--drp-phase") == "mutant")
    request.config._drp["controls"][request.node.nodeid] = True  # only reached if the control passed


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Generator:
    """Record each phase (setup/call/teardown) and whether a failure was a genuine assertion."""
    result = yield
    report = result.get_result()
    assertion = False
    if call.excinfo and report.when == "call":
        error = call.excinfo.value
        # `pytest.raises` that never saw the exception raises Failed("DID NOT RAISE"): also a real assertion.
        assertion = isinstance(error, AssertionError) or (
            isinstance(error, pytest.fail.Exception) and str(error).startswith("DID NOT RAISE")
        )
    item.config._drp["reports"].append({
        "nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
        "assertion": assertion, "xfail": hasattr(report, "wasxfail"),
    })


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    data = session.config._drp
    data["exitcode"] = int(exitstatus)  # validator cross-checks this against the process exit code
    Path(session.config.getoption("--drp-report")).write_text(json.dumps(data))
