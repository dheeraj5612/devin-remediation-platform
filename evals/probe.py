"""Trusted pytest plugin. Copied into the evaluator, never into the candidate repository."""
import ast
import importlib
import inspect
from functools import wraps
import textwrap
import json
import os
from pathlib import Path

import pytest

REPORT = {"collected": [], "phases": [], "controls": {}, "collection_errors": [], "findings": []}


def histogram_control(item, patch, mutant):
    from pandas import DataFrame

    module = importlib.import_module("superset.utils.pandas_postprocessing.histogram")
    exports = importlib.import_module("superset.utils.pandas_postprocessing")
    original = module.histogram
    if mutant:
        @wraps(original)
        def broken(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except ValueError as error:
                if "contains non-numeric values" not in str(error):
                    raise
                return DataFrame()

        patch.setattr(module, "histogram", broken)
        patch.setattr(exports, "histogram", broken)
        for name, value in list(vars(item.module).items()):
            if value is original:
                patch.setattr(item.module, name, broken)
    rejected = False
    try:
        module.histogram(DataFrame({"value": ["invalid"]}), "value", None, 5)
    except ValueError as error:
        rejected = str(error) == "Column 'value' contains non-numeric values"
    numeric = module.histogram(DataFrame({"value": [1, "10"]}), "value", None, 5)
    return rejected == (not mutant) and int(numeric.values.sum()) == 2


def schema_control(item, patch, mutant):
    from marshmallow import fields, Schema, ValidationError
    from superset.databases.schemas import DatabaseParametersSchemaMixin
    from superset.models.core import ConfigurationMethod

    original = DatabaseParametersSchemaMixin.build_sqlalchemy_uri
    if mutant:
        @wraps(original)
        def broken(self, data, **kwargs):
            parameters = data.get("parameters") or {}
            missing = not (data.get("engine") or data.get("backend") or parameters.get("engine"))
            if data.get("configuration_method") == ConfigurationMethod.DYNAMIC_FORM and missing:
                return {key: value for key, value in data.items() if key not in {"parameters", "engine", "backend", "driver"}}
            return original(self, data, **kwargs)

        patch.setattr(DatabaseParametersSchemaMixin, "build_sqlalchemy_uri", broken)

    class ControlSchema(DatabaseParametersSchemaMixin, Schema):
        sqlalchemy_uri = fields.String()

    payload = {"configuration_method": ConfigurationMethod.DYNAMIC_FORM,
               "parameters": {"username": "username", "host": "localhost", "port": 12345, "database": "dbname"}}
    rejected = False
    try:
        ControlSchema().load(payload)
    except ValidationError as error:
        rejected = "An engine must be specified" in str(error.messages)
    return rejected == (not mutant)


def pytest_collection_finish(session):
    REPORT["collected"] = [item.nodeid for item in session.items]
    for item in session.items:
        tree = ast.parse(textwrap.dedent(inspect.getsource(item.obj)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and not node.orelse:
                conditional = any(isinstance(child, ast.Assert) for handler in node.handlers for child in ast.walk(handler))
                guarded = any(isinstance(child, (ast.Assert, ast.Raise)) for statement in node.body for child in ast.walk(statement))
                if conditional and not guarded:
                    REPORT["findings"].append({"test": item.nodeid, "rule": "exception-only-assertion"})


def pytest_collectreport(report):
    if report.failed:
        REPORT["collection_errors"].append(report.nodeid)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    patch = pytest.MonkeyPatch()
    try:
        control = {"histogram-swallow-invalid": histogram_control,
                   "schema-bypass-missing-engine": schema_control}[os.environ["EVAL_CHALLENGE"]]
        REPORT["controls"][item.nodeid] = bool(control(item, patch, os.environ.get("EVAL_MUTANT") == "1"))
    except Exception:
        REPORT["controls"][item.nodeid] = False
    try:
        yield
    finally:
        patch.undo()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    failure, exception_type = "", ""
    if call.excinfo:
        exception = call.excinfo.value
        exception_type = type(exception).__name__
        if isinstance(exception, AssertionError):
            failure = "assertion"
        elif type(exception).__name__ == "Failed" and "DID NOT RAISE" in str(exception):
            expected = "ValueError" if os.environ["EVAL_CHALLENGE"].startswith("histogram") else "ValidationError"
            if expected in str(exception):
                failure = "missing_expected_exception"
    REPORT["phases"].append({"nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
                             "xfail": hasattr(report, "wasxfail"), "failure": failure, "exception": exception_type})


def pytest_sessionfinish(session, exitstatus):
    REPORT["exit_code"] = int(exitstatus)
    destination = Path(os.environ.get("EVAL_REPORT", "/reports/result.json"))
    destination.write_text(json.dumps(REPORT, sort_keys=True))
