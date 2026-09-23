"""Registry loading, baseline-evidence gate, static weak-test detector, metrics denominators, and the full demo."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.cases as cases_module
from app.cases import Registry, detect
from app.db import Store
from app.main import create_app
from app.metrics import metrics
from app.models import now
from app.report import pilot_readiness
from app.simulation import run_demo


def test_detector_is_only_a_static_finding():
    """The static detector labels suspicious structure without claiming runtime proof."""
    # ELI5: give the detector one weak and one strong test so only the weak shape is reported.
    source = """def test_weak():
    try:
        operation()
    except ValueError:
        assert True

def test_strong():
    assert operation() == 1
"""
    # ELI5: run the syntax-only detector against a named source path.
    findings = detect(source, "tests/example.py")
    # ELI5: exactly one suspicious test should be found.
    assert len(findings) == 1
    # ELI5: the finding must point at the weak test, not the strong one.
    assert findings[0]["test"] == "test_weak"
    # ELI5: static structure is not runtime confirmation.
    assert findings[0]["status"] == "SUSPICIOUS_NOT_CONFIRMED"


def test_normalize_dttm_case_keeps_the_single_test_scope(settings):
    """The datetime case binds the trusted challenge to the approved test file."""
    # ELI5: load the allow-list that drives both prompts and validator scope.
    case = Registry(settings).cases["superset-normalize-dttm-edge-cases"]
    # ELI5: confirm production code is context only and Devin can edit one test file.
    assert case.affected_paths == ["superset/utils/core.py", "tests/unit_tests/utils/test_date_parsing.py"]
    assert case.allowed_paths == ["tests/unit_tests/utils/test_date_parsing.py"]
    # ELI5: keep the original test node and trusted one-row mutant bound together.
    assert case.test_ids == ["tests/unit_tests/utils/test_date_parsing.py::test_edge_cases"]
    assert case.challenge == "normalize-dttm-skip-single-row"


@pytest.mark.parametrize("field,value", [("outcome", "REJECTED"), ("mode", "SIMULATION"),
    ("sha", "0" * 40), ("case_fingerprint", "stale"), ("harness_fingerprint", "stale"),
    ("normal", "INFRA_ERROR"), ("mutant", "NOT_RUN"), ("case_id", "other-case")])
def test_live_evidence_must_be_current_and_confirmed(live, field, value):
    """Any stale or nonconfirmed evidence is rejected by the registry gate."""
    # ELI5: load the approved registry and its known-good baseline proof.
    registry = Registry(live)
    # ELI5: select the case whose proof will be intentionally corrupted.
    case = registry.cases["histogram-invalid-column"]
    # ELI5: establish that the fixture starts with confirmed evidence.
    assert registry.evidence(case)["outcome"] == "CONFIRMED"
    # ELI5: open the persisted proof file that the registry will re-check.
    path = live.storage / "baselines" / f"{case.id}.json"
    # ELI5: parse the proof so one parameter can make it stale or invalid.
    proof = json.loads(path.read_text())
    # ELI5: apply the specific invalid value supplied by this parameterized row.
    proof[field] = value
    # ELI5: write the corrupted proof back to the isolated test storage.
    path.write_text(json.dumps(proof))
    # ELI5: every corruption must fail the same baseline gate.
    with pytest.raises(ValueError, match="stale"):
        registry.evidence(case)


def test_duplicate_issue_bindings_are_rejected(settings):
    """Two approved cases cannot share one issue number."""
    # ELI5: bind two names to one issue to model an ambiguous spend trigger.
    settings.case_issues = {"histogram-invalid-column": 1, "schema-missing-engine": 1}
    # ELI5: registry construction must reject the ambiguous mapping.
    with pytest.raises(ValueError, match="unique"):
        Registry(settings)


def test_harness_fingerprint_includes_nested_application_oracles(tmp_path, monkeypatch):
    """Changing a nested trusted application oracle must invalidate its fingerprint."""

    # ELI5: build the minimal trusted source tree that the fingerprint reader expects.
    for relative in ("app/cases.py", "app/validator.py", "evals/challenges.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    # ELI5: put the oracle in a nested directory because top-level-only scans miss it.
    oracle = tmp_path / "evals/application_cases/nested/oracle.py"
    oracle.parent.mkdir(parents=True, exist_ok=True)
    oracle.write_text("first")
    # ELI5: point only this test's fingerprint calculation at the temporary source tree.
    monkeypatch.setattr(cases_module, "ROOT", tmp_path)
    before = cases_module.harness_fingerprint()
    oracle.write_text("changed")
    assert before != cases_module.harness_fingerprint()


def test_live_readiness_aggregates_blocked_gate_rows(live):
    """Live readiness stays blocked when a non-configuration gate is missing."""
    # ELI5: the live-shaped fixture has valid-looking credentials but intentionally lacks two operational gates.
    live.superset_python = live.data_dir / "missing-python"
    registry = Registry(live)
    # ELI5: ask the read-only report helper for the same readiness answer the dashboard displays.
    readiness = pilot_readiness(live, registry)
    # ELI5: a missing interpreter and incomplete issue bindings must block a paid pilot.
    assert readiness["state"] == "BLOCKED"
    assert "Superset interpreter" in readiness["problems"]
    assert "Case bindings" in readiness["problems"]


@pytest.mark.parametrize("context_state", ["malformed", "mismatched"])
def test_live_readiness_rejects_invalid_context(context_state, live):
    """Readiness stays blocked when context JSON is malformed or belongs to another repository."""
    # ELI5: make every unrelated readiness row valid so this test isolates context validation.
    live.superset_repo_path = Path.cwd()
    live.superset_python = Path(sys.executable)
    live.case_issues = {
        "histogram-invalid-column": 101,
        "schema-missing-engine": 102,
        "import-unparseable-yaml": 105,
        "superset-normalize-dttm-edge-cases": 103,
        "report-anchor-non-string": 106,
    }
    context_path = live.storage / "context.json"
    if context_state == "malformed":
        # ELI5: an existing file with invalid JSON must not look ready just because it exists.
        context_path.write_text("{malformed")
    else:
        # ELI5: keep valid JSON but bind it to a different repository than the configured job.
        context = json.loads(context_path.read_text())
        context["base_branches"] = {
            "histogram-invalid-column": live.base_branch,
            "schema-missing-engine": live.base_branch,
            "import-unparseable-yaml": "remediation-import-yaml",
            "superset-normalize-dttm-edge-cases": live.base_branch,
            "report-anchor-non-string": live.base_branch,
        }
        context["repository"] = "other/repository"
        context_path.write_text(json.dumps(context))
    readiness = pilot_readiness(live, Registry(live))
    assert readiness["state"] == "BLOCKED"
    assert "Devin context" in readiness["problems"]
    assert "Superset interpreter" not in readiness["problems"]
    assert "Case bindings" not in readiness["problems"]
    assert "Baseline evidence" not in readiness["problems"]


def test_simulation_exercises_real_state_machine_with_separate_storage(settings):
    """The credential-free demo covers test and application outcomes in isolated storage."""
    # ELI5: run the scripted six-job story once, then inspect the same SQLite snapshot the UI reads.
    result = run_demo(settings)
    # ELI5: the run must stay in the credential-free simulation mode.
    assert result["mode"] == "SIMULATION"
    # ELI5: each of the six jobs creates one scripted session.
    assert result["sessions_created"] == 6
    # ELI5: three jobs verify without correction, measured over six evaluated first attempts.
    assert result["first_pass"] == 3 and result["first_pass_denominator"] == 6
    # ELI5: one job recovers after a correction, measured over three correction attempts.
    assert result["correction_recovery"] == 1 and result["correction_denominator"] == 3
    # ELI5: the rejected application candidate is represented by the application failure outcome.
    assert result["validation_counts"]["APPLICATION_FAILED"] == 1
    # ELI5: the replayed webhook was suppressed once.
    assert result["duplicate_deliveries"] == 1
    # ELI5: the simulation must leave a durable restart event in its event diary.
    store = Store(settings)
    assert "WORKER_RESUMED" in [event.event_type for event in store.events()]
    with TestClient(create_app(settings)) as client:
        # ELI5: request the page to prove the app can render the persisted snapshot.
        page = client.get("/dashboard").text
        # ELI5: the page must make synthetic mode and the persisted activity view visible.
        assert "SIMULATION" in page
        assert "Job activity" in page
        # ELI5: fetch the downloadable snapshot from the same report model.
        report_response = client.get("/report.json")
        # ELI5: the export endpoint must return success and an attachment disposition.
        assert report_response.status_code == 200
        assert "attachment" in report_response.headers["content-disposition"]
        # ELI5: parse the JSON so structured truth and workflow fields can be checked.
        report = report_response.json()
        # ELI5: the schema is stable for a customer or review tool.
        assert report["schema"] == "devin-remediation-evidence/v1"
        # ELI5: the export carries an explicit synthetic truth flag and statement.
        assert report["truth"]["simulation"] is True
        assert "No live remediation evidence" in report["truth"]["statement"]
        # ELI5: six persisted jobs feed the KPI and first funnel step.
        assert report["metrics"]["jobs_total"] == 6
        assert report["workflow"]["steps"][0]["count"] == 6
        # ELI5: the third application case is present in the portfolio export.
        assert any(job["case_id"] == "import-unparseable-yaml" for job in report["jobs"])
        # ELI5: every demo job is labeled synthetic instead of looking like live proof.
        assert all(job["evidence_level"] == "SIMULATED" for job in report["jobs"])
        issue_104 = next(job for job in report["jobs"] if job["issue_number"] == 104)
        issue_104_api = [event for event in issue_104["events"] if event["type"] == "DEVIN_API_OPERATION"]
        assert [(event["details"]["operation_key"], event["details"]["outcome"]) for event in issue_104_api] == [
            ("attachment_upload", "SUCCEEDED"),
            ("session_create", "RETRYABLE_ERROR"),
            ("list_reconcile", "FOUND"),
            ("session_poll", "SUCCEEDED"),
        ]
        assert len([event for event in issue_104_api if event["details"]["operation_key"] == "session_create"]) == 1
        assert report["provider_api"]["operations"]["list_reconcile"]["outcome_counts"] == {"FOUND": 1}
        # ELI5: raw failure text must never be part of the customer report.
        assert all("failure_reason" not in job for job in report["jobs"])
        # ELI5: the truth language must remain consistent with the simulation boundary.
        assert "simulation-only" not in json.dumps(report)
        # ELI5: application cases use one trusted contract result, so they must not look like missing test phases.
        application_jobs = [job for job in report["jobs"] if job["case_kind"] == "application"]
        assert {job["issue_number"] for job in application_jobs} == {105, 106}
        accepted = next(job for job in application_jobs if job["issue_number"] == 105)
        rejected = next(job for job in application_jobs if job["issue_number"] == 106)
        assert accepted["validation"]["application_status"] == "PASS"
        assert rejected["validation"]["application_status"] == "REGRESSION"
        assert rejected["validation"]["outcome"] == "APPLICATION_FAILED"
        # ELI5: select the passing application job to inspect its case-aware detail view.
        app_detail = client.get(f"/jobs/{accepted['id']}")
        # ELI5: the selected detail must render and expose the application contract result.
        assert app_detail.status_code == 200
        assert "App contract" in app_detail.text
        assert "Application: PASS" in app_detail.text
        assert "NOT_RUN / NOT_RUN" not in app_detail.text
        # ELI5: select a normal test-quality job to retain the existing detail smoke check.
        job = store.jobs()[0]
        detail = client.get(f"/jobs/{job.id}")
        # ELI5: the normal detail still shows its verified simulation state.
        assert detail.status_code == 200 and "VERIFIED" in detail.text
    # ELI5: live-mode storage is separate and starts empty in this test.
    live = settings.model_copy(update={"mode": "LIVE"})
    live_store = Store(live)
    # ELI5: no live rows were created as a side effect of the simulation.
    assert not live_store.jobs()
    # ELI5: an empty live store reports no attempted jobs.
    assert metrics(live_store)["attempted"] == 0
    # ELI5: simulation and live mode use different database paths.
    assert settings.storage != live.storage
    # ELI5: the demo refuses to write live storage by design.
    with pytest.raises(ValueError, match="never"):
        run_demo(live)
    # ELI5: close both SQLite engines so the temporary files can be cleaned up.
    store.engine.dispose()
    live_store.engine.dispose()


def test_report_redacts_untrusted_event_details(settings, store):
    """The export keeps timeline labels but never copies arbitrary event text."""
    # ELI5: a hostile provider message should disappear before it reaches HTML or JSON.
    # ELI5: create one isolated job for the redaction check.
    job, _ = store.enqueue("delivery-redaction", settings.github_repository, 777, "histogram-invalid-column")
    # ELI5: save unsafe link and text fields beside one allowed outcome label.
    store.change(job.id, "UNSAFE_EVENT", candidate_pr_url="javascript:credential", details={"unsafe": "<script>credential</script>", "outcome": "VERIFIED"})
    with TestClient(create_app(settings)) as client:
        # ELI5: render both output channels that could accidentally leak the text.
        page = client.get("/dashboard").text
        export = client.get("/report.json").text
    # ELI5: the secret marker must be absent from the HTML page.
    assert "credential" not in page
    # ELI5: the secret marker must be absent from the JSON export.
    assert "credential" not in export
    # ELI5: unsafe URL schemes must not become links in the page.
    assert "javascript:" not in page
    # ELI5: unsafe URL schemes must not become links in the export.
    assert "javascript:" not in export
    # ELI5: the allow-listed outcome remains useful after redaction.
    assert '"outcome":"VERIFIED"' in export


def test_report_redacts_provider_payload_details(settings, store):
    """Keep provider operation counts while excluding payload-shaped event fields."""

    # ELI5: model a trace row that accidentally receives every sensitive payload field.
    job, _ = store.enqueue("delivery-provider-redaction", settings.github_repository, 778,
                           "histogram-invalid-column")
    sentinel = "PROVIDER_PROMPT_MESSAGE_TOKEN_BODY_SENTINEL"
    store.change(job.id, "DEVIN_API_OPERATION", details={
        "operation": "session_create", "operation_key": "session_create", "outcome": "SUCCEEDED",
        "mode": "SIMULATION", "latency_ms": 0, "session_id": "sim-safe",
        "prompt": sentinel, "message": sentinel, "token": sentinel, "body": sentinel,
        "provider_response": sentinel, "url": sentinel,
    })
    with TestClient(create_app(settings)) as client:
        # ELI5: both HTML and JSON must expose the safe count without the provider payload.
        page = client.get("/dashboard").text
        export = client.get("/report.json").text
        report = client.get("/report.json").json()
    assert sentinel not in page and sentinel not in export
    assert report["provider_api"]["event_count"] == 1
    details = report["jobs"][0]["events"][-1]["details"]
    assert details == {
        "operation": "session_create", "operation_key": "session_create", "outcome": "SUCCEEDED",
        "mode": "SIMULATION", "latency_ms": 0, "session_id": "sim-safe",
    }


def test_latencies_and_denominators_exclude_infrastructure(settings, store):
    """Metrics keep infrastructure failures out of assessed repair denominators."""
    # ELI5: use one shared timestamp so expected latencies are deterministic.
    timestamp = now() - timedelta(minutes=5)
    for issue in [101, 102]:
        # ELI5: create one verified job and one infrastructure-failed job.
        job, _ = store.enqueue(str(issue), settings.github_repository, issue, "histogram-invalid-column")
        # ELI5: persist a five-minute-old worker start.
        store.change(job.id, "STARTED", status="DEVIN_RUNNING", created_at=timestamp, started_at=timestamp)
        # ELI5: persist a PR exactly ten seconds after the webhook.
        store.change(job.id, "PR", status="PR_OPENED", pr_created_at=timestamp + timedelta(seconds=10))
        # ELI5: persist the validation handoff.
        store.change(job.id, "VALIDATING", status="VALIDATING")
        # ELI5: issue 102 models infrastructure failure while 101 earns a verdict.
        outcome = "VERIFIED" if issue == 101 else "INFRA_ERROR"
        # ELI5: record the first-attempt outcome without claiming a correction.
        store.change(job.id, "EVALUATED", details={"outcome": outcome, "correction_count": 0})
        # ELI5: close each job twenty seconds after its webhook using its appropriate terminal status.
        store.change(job.id, outcome if issue == 101 else "FAILED", status=outcome if issue == 101 else "FAILED",
                     completed_at=timestamp + timedelta(seconds=20))
    # ELI5: calculate metrics from the two persisted jobs.
    values = metrics(store)
    # ELI5: only the verified repair belongs in the first-pass denominator.
    assert values["first_pass_denominator"] == 1
    # ELI5: that one repair was verified on its first attempt.
    assert values["first_pass"] == 1
    # ELI5: no correction was sent in this fixture.
    assert values["correction_denominator"] == 0
    # ELI5: the persisted PR timestamp yields ten seconds.
    assert values["median_event_to_pr_seconds"] == 10
    # ELI5: the persisted verification timestamp yields twenty seconds.
    assert values["median_event_to_verified_seconds"] == 20
