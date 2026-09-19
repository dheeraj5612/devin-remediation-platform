"""Two external pytest challenges. Loaded only in disposable Superset worktrees."""

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
    config._drp = {"collected": [], "reports": [], "controls": {}, "provenance": {},
                   "environment": {"python": sys.version, "pytest": version("pytest")}}


def pytest_collection_finish(session: pytest.Session) -> None:
    session.config._drp["collected"] = [item.nodeid for item in session.items]


def histogram_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
    from pandas import DataFrame
    from pandas.testing import assert_frame_equal

    module = importlib.import_module("superset.utils.pandas_postprocessing.histogram")
    original = module.to_numeric
    if mutant:
        def accept_invalid(values: Any, **kwargs: Any) -> Any:
            return original(values, **kwargs).fillna(0)
        monkeypatch.setattr(module, "to_numeric", accept_invalid)

    numeric = module.histogram(DataFrame({"value": [1, 10]}), "value", [], 2)
    strings = module.histogram(DataFrame({"value": ["1", "10"]}), "value", [], 2)
    assert_frame_equal(numeric, strings)
    invalid = DataFrame({"value": ["not-a-number", "also-invalid"]})
    if mutant:
        result = module.histogram(invalid, "value", [], 2)
        assert int(result.to_numpy().sum()) == 2
    else:
        with pytest.raises(ValueError, match="^Column 'value' contains non-numeric values$"):
            module.histogram(invalid, "value", [], 2)


def schema_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
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
                result = dict(data)
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


@pytest.fixture(autouse=True)
def registered_challenge(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    modules = {"histogram-accept-invalid": "superset.utils.pandas_postprocessing.histogram",
               "schema-accept-missing-engine": "superset.databases.schemas"}
    module = importlib.import_module(modules[request.config.getoption("--drp-challenge")])
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(Path(request.config.rootpath).resolve()):
        raise RuntimeError("Validation imported production code outside the candidate worktree")
    request.config._drp["provenance"][request.node.nodeid] = str(origin)
    controls = {"histogram-accept-invalid": histogram_control, "schema-accept-missing-engine": schema_control}
    control = controls[request.config.getoption("--drp-challenge")]
    control(monkeypatch, request.config.getoption("--drp-phase") == "mutant")
    request.config._drp["controls"][request.node.nodeid] = True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Generator:
    result = yield
    report = result.get_result()
    assertion = False
    if call.excinfo and report.when == "call":
        error = call.excinfo.value
        assertion = isinstance(error, AssertionError) or (
            isinstance(error, pytest.fail.Exception) and str(error).startswith("DID NOT RAISE")
        )
    item.config._drp["reports"].append({
        "nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
        "assertion": assertion, "xfail": hasattr(report, "wasxfail"),
    })


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    data = session.config._drp
    data["exitcode"] = int(exitstatus)
    Path(session.config.getoption("--drp-report")).write_text(json.dumps(data))
