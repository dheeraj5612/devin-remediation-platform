"""External pytest plugin holding the registered regressions ("challenges").

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
    """Register the challenge, phase, and report arguments consumed by the validator."""
    # ELI5: pytest will refuse to start unless the control plane names every required switch.
    # ELI5: register the challenge name that selects one trusted production defect.
    parser.addoption("--drp-challenge", required=True)
    # ELI5: register whether production code stays clean or receives the known mutant.
    parser.addoption("--drp-phase", choices=["normal", "mutant"], required=True)
    # ELI5: register the control-plane path where structured evidence must be written.
    parser.addoption("--drp-report", required=True)


def pytest_configure(config: pytest.Config) -> None:
    """Create the one report dictionary shared by collection, controls, and test hooks."""
    # ELI5: start empty lists/maps plus runtime versions so a later reader can audit the run.
    config._drp = {"collected": [], "reports": [], "controls": {}, "provenance": {},
                   "environment": {"python": sys.version, "pytest": version("pytest")}}


def pytest_collection_finish(session: pytest.Session) -> None:
    """Record exact collected node IDs so extra or missing tests cannot pass unnoticed."""
    # ELI5: the validator later compares this list with its allow-listed node IDs.
    # ELI5: record exactly what pytest selected, including order-independent node identities.
    session.config._drp["collected"] = [item.nodeid for item in session.items]  # validator checks exact IDs


def histogram_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
    """Regression: `histogram()` silently coerces non-numeric values to 0 instead of raising ValueError."""
    # ELI5: first prove valid numeric strings still work, then isolate the invalid-input behavior.
    # ELI5: import the normal data frame type used by the production helper.
    from pandas import DataFrame
    # ELI5: import a strict frame comparison for the valid-input control.
    from pandas.testing import assert_frame_equal

    # ELI5: load the candidate production module, not an installed package.
    module = importlib.import_module("superset.utils.pandas_postprocessing.histogram")
    # ELI5: save the real conversion helper before optionally replacing it.
    original = module.to_numeric
    # ELI5: arm the exact historical bug only for the mutant phase.
    if mutant:
        def accept_invalid(values: Any, **kwargs: Any) -> Any:
            """Reintroduce the registered bug by coercing invalid numeric values to zero."""
            return original(values, **kwargs).fillna(0)  # NaN from "not-a-number" becomes 0: the bug
        # ELI5: make every call in this phase use the controlled mutant helper.
        monkeypatch.setattr(module, "to_numeric", accept_invalid)

    # Both phases: numeric strings like "10" are valid input and must equal real numbers.
    # ELI5: compute the expected result from numeric values.
    numeric = module.histogram(DataFrame({"value": [1, 10]}), "value", [], 2)
    # ELI5: compute the same result from numeric strings that should remain valid.
    strings = module.histogram(DataFrame({"value": ["1", "10"]}), "value", [], 2)
    # ELI5: prove valid equivalent inputs still produce identical frames in both phases.
    assert_frame_equal(numeric, strings)
    # Positive control: truly invalid strings must raise on clean code and be accepted under the mutant.
    # ELI5: prepare two deliberately invalid values for the positive control.
    invalid = DataFrame({"value": ["not-a-number", "also-invalid"]})
    # ELI5: choose the historical fallback behavior only for the mutant phase.
    if mutant:
        # ELI5: the mutant must demonstrate the old silent coercion behavior.
        result = module.histogram(invalid, "value", [], 2)
        # ELI5: both invalid values becoming zero is the exact registered weakness.
        assert int(result.to_numpy().sum()) == 2
    else:
        # ELI5: clean code must reject the same values with the documented error.
        with pytest.raises(ValueError, match="^Column 'value' contains non-numeric values$"):
            # ELI5: invoke the production function inside the expected-error assertion.
            module.histogram(invalid, "value", [], 2)


def schema_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
    """Regression: dynamic-form database parameters without an engine are accepted instead of rejected."""
    # ELI5: the control supplies a complete parameter payload with exactly one missing required field.
    # ELI5: import the schema building blocks used by the production contract.
    from marshmallow import fields, Schema, ValidationError

    # ELI5: import the exact candidate mixin and enum under validation.
    from superset.databases.schemas import DatabaseParametersSchemaMixin
    from superset.models.core import ConfigurationMethod

    # ELI5: keep the real method so the mutant wrapper can delegate for all other inputs.
    original = DatabaseParametersSchemaMixin.build_sqlalchemy_uri
    # ELI5: arm the missing-engine bypass only for the mutant phase.
    if mutant:
        @functools.wraps(original)
        def accept_missing_engine(self: Any, data: dict, **kwargs: Any) -> dict:
            """Reintroduce the missing-engine bypass for the mutant phase only."""
            # ELI5: read nested parameters without changing unrelated payload shapes.
            parameters = data.get("parameters", {})
            # ELI5: detect the exact missing engine condition from the historical bug.
            missing = not (data.get("engine") or parameters.get("engine") or data.get("backend"))
            # ELI5: apply the bypass only to dynamic-form requests missing an engine.
            if data.get("configuration_method") == ConfigurationMethod.DYNAMIC_FORM and missing:
                # ELI5: copy the caller's data before constructing the old incorrect URI.
                result = dict(data)  # the bug: invent a URI instead of raising ValidationError
                # ELI5: remove nested parameters just as the old bypass did.
                result.pop("parameters", None)
                # ELI5: create the invalid fallback output that the real fix must reject.
                result["sqlalchemy_uri"] = "sqlite://"
                # ELI5: return the mutant result without calling the corrected implementation.
                return result
            # ELI5: preserve the real behavior for every non-mutant shape.
            return original(self, data, **kwargs)
        # ELI5: replace only the selected method for this test phase.
        monkeypatch.setattr(DatabaseParametersSchemaMixin, "build_sqlalchemy_uri", accept_missing_engine)

    # ELI5: make a minimal schema that exercises the mixin's URI construction hook.
    class ProbeSchema(DatabaseParametersSchemaMixin, Schema):
        """Expose only the URI field needed to test the missing-engine contract."""

        # ELI5: include the output field so the loaded result exposes the URI decision.
        sqlalchemy_uri = fields.String()

    # ELI5: provide all ordinary parameters while intentionally omitting the engine.
    payload = {"configuration_method": ConfigurationMethod.DYNAMIC_FORM,
               "parameters": {"username": "username", "password": "password", "host": "localhost",
                              "port": 12345, "database": "dbname"}}
    # ELI5: choose the historical fallback behavior only for the mutant phase.
    if mutant:
        # ELI5: the mutant must expose its incorrect sqlite fallback.
        assert ProbeSchema().load(payload)["sqlalchemy_uri"] == "sqlite://"
    else:
        # ELI5: clean code must raise instead of manufacturing a URI.
        with pytest.raises(ValidationError) as error:
            # ELI5: invoke schema loading inside the expected-error assertion.
            ProbeSchema().load(payload)
        # ELI5: assert the exact production contract and no weaker truthy error.
        assert error.value.messages == {
            "_schema": ["An engine must be specified when passing individual parameters to a database."]
        }


def normalize_dttm_control(monkeypatch: pytest.MonkeyPatch, mutant: bool) -> None:
    """Regression: a one-row datetime column is left unconverted."""
    # ELI5: import the candidate module so provenance and the control use the same checkout.
    module = importlib.import_module("superset.utils.core")
    # ELI5: keep the real helper available for all rows outside the controlled regression.
    original = module._process_datetime_column
    # ELI5: arm the historical small-frame skip only for the mutant phase.
    if mutant:
        def skip_single_row(df: Any, col: Any) -> None:
            """Reintroduce the regression for exactly one-row frames."""
            if len(df) == 1:
                return
            original(df, col)

        # ELI5: patch the helper called by normalize_dttm_col, leaving production files untouched.
        monkeypatch.setattr(module, "_process_datetime_column", skip_single_row)

    # Positive control: a two-row frame must still convert in both phases.
    import pandas as pd

    multi = pd.DataFrame({"date": ["2023-01-01", "2023-01-02"]})
    module.normalize_dttm_col(multi, (module.DateColumn(col_label="date"),))
    assert pd.api.types.is_datetime64_any_dtype(multi["date"])
    assert multi["date"].tolist() == [pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-02")]

    # The phase control is the one-row contract under test.
    single = pd.DataFrame({"date": ["2023-01-01"]})
    module.normalize_dttm_col(single, (module.DateColumn(col_label="date"),))
    if mutant:
        # ELI5: the registered mutant leaves the source string untouched.
        assert single["date"].dtype == object
        assert single["date"].iloc[0] == "2023-01-01"
    else:
        # ELI5: clean code converts the value in place to a pandas timestamp.
        assert pd.api.types.is_datetime64_any_dtype(single["date"])
        assert single["date"].iloc[0] == pd.Timestamp("2023-01-01")


# ELI5: map each approved challenge name to its candidate module and positive control.
# challenge name -> (production module under test, control function)
CHALLENGES = {
    "histogram-accept-invalid": ("superset.utils.pandas_postprocessing.histogram", histogram_control),
    "schema-accept-missing-engine": ("superset.databases.schemas", schema_control),
    "normalize-dttm-skip-single-row": ("superset.utils.core", normalize_dttm_control),
}


@pytest.fixture(autouse=True)
def registered_challenge(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs before every collected test: verify provenance, arm the phase, run the control, record it."""
    # ELI5: select only a challenge registered by the control plane's command-line option.
    module_name, control = CHALLENGES[request.config.getoption("--drp-challenge")]
    # ELI5: import the production module that this challenge claims to protect.
    module = importlib.import_module(module_name)
    # ELI5: resolve its source path for the candidate-checkout provenance gate.
    origin = Path(module.__file__).resolve()
    # ELI5: reject installed modules because validation must test the detached candidate SHA.
    if not origin.is_relative_to(Path(request.config.rootpath).resolve()):
        # ELI5: do not claim provenance when Python imported code outside the candidate checkout.
        raise RuntimeError("Validation imported production code outside the candidate worktree")
    # ELI5: record the exact production file imported for this test node.
    request.config._drp["provenance"][request.node.nodeid] = str(origin)
    # ELI5: run the positive control before the candidate test can be counted as evidence.
    control(monkeypatch, request.config.getoption("--drp-phase") == "mutant")
    # ELI5: mark the control active only after it completed without error.
    request.config._drp["controls"][request.node.nodeid] = True  # only reached if the control passed


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Generator:
    """Record each phase (setup/call/teardown) and whether a failure was a genuine assertion."""
    # ELI5: let pytest run the phase before reading its final report.
    result = yield
    # ELI5: extract the framework's report for this setup, call, or teardown phase.
    report = result.get_result()
    # ELI5: start false and mark only genuine assertion failures in the test call.
    assertion = False
    # ELI5: inspect exceptions only for failures raised during the test call itself.
    if call.excinfo and report.when == "call":
        # ELI5: inspect the raised exception only for call-phase failures.
        error = call.excinfo.value
        # `pytest.raises` that never saw the exception raises Failed("DID NOT RAISE"): also a real assertion.
        # ELI5: classify ordinary assertions and pytest.raises missing-error failures as assertions.
        assertion = isinstance(error, AssertionError) or (
            isinstance(error, pytest.fail.Exception) and str(error).startswith("DID NOT RAISE")
        )
    # ELI5: append one normalized record that the validator can compare across phases.
    item.config._drp["reports"].append({
        "nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
        "assertion": assertion, "xfail": hasattr(report, "wasxfail"),
    })


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Persist the final exit code so the validator can compare pytest with its child process."""
    # ELI5: get the shared report assembled by the hooks above.
    data = session.config._drp
    # ELI5: store pytest's final code so the parent process can cross-check it.
    data["exitcode"] = int(exitstatus)  # validator cross-checks this against the process exit code
    # ELI5: write JSON to the control-plane path selected before candidate execution.
    Path(session.config.getoption("--drp-report")).write_text(json.dumps(data))
