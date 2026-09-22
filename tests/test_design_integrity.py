"""Cross-surface evidence checks and packaged-archive fidelity."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import presentation
from app.main import create_app
from app.presentation import linked_verdict, recorded_evidence, workbench
from app.simulation import run_demo, simulation_settings


def test_archive_package_copy_matches_the_historical_source():
    """Keep the public archive available after packaging without silently forking it."""

    # ELI5: compare the shipped copy byte-for-byte with the source record reviewers trust.
    source = Path(__file__).resolve().parents[1] / "evidence/live-application.json"
    assert presentation.ARCHIVE_PATH.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("sha", [None, "", "not-a-commit", "A" * 40])
def test_archive_rejects_invalid_baseline_sha(tmp_path, monkeypatch, sha):
    """Reject archive records whose baseline SHA is missing, malformed, or incorrectly cased."""

    # ELI5: alter only the baseline identity, then point the verifier at this disposable record.
    record = json.loads(presentation.ARCHIVE_PATH.read_text())
    record["baseline"]["sha"] = sha
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(presentation, "ARCHIVE_PATH", path)
    assert recorded_evidence() is None


def test_invalid_matched_placeholders_are_not_commit_proof():
    """Treat matching placeholder values as incomplete evidence rather than a verified commit link."""

    # ELI5: equal strings are insufficient when they are placeholders or when one SHA is absent.
    assert linked_verdict({"status": "VERIFIED", "candidate_sha": "placeholder", "validated_sha": "placeholder"}) == "INCOMPLETE"
    assert linked_verdict({"status": "VERIFIED", "candidate_sha": "a" * 40}) == "INCOMPLETE"


def test_unlinked_evidence_is_attention_not_verified_across_surfaces(tmp_path):
    """Keep a stored VERIFIED row in the attention view when its candidate and validation SHAs do not link."""

    # ELI5: start with the six deterministic demo jobs, then damage one stored link without touching the store.
    settings = simulation_settings(tmp_path)
    run_demo(settings)
    with TestClient(create_app(settings)) as client:
        report = client.get("/report.json").json()
        damaged = copy.deepcopy(report)
        job = next(job for job in damaged["jobs"] if job["issue_number"] == 105)
        # ELI5: a zero SHA looks present but cannot prove that the candidate was actually validated.
        job["validated_sha"] = "0" * 40
        preserved = copy.deepcopy(damaged)
        desk = workbench(damaged)
        assert desk["counts"] == {"all": 6, "attention": 3, "verified": 3, "active": 0}
        assert (desk["verified_count"], desk["attention_count"], desk["unlinked_count"]) == (3, 3, 1)
        assert job in workbench(damaged, view="attention")["rows"]
        assert job not in workbench(damaged, view="verified")["rows"]
        assert workbench(damaged, query="no match")["verified_count"] == 3
        # ELI5: rendering the damaged snapshot must not rewrite the report returned by the JSON route.
        assert damaged == preserved
        with patch("app.views.build_report", return_value=damaged):
            html = client.get("/dashboard").text
        assert "1 stored VERIFIED record(s) have incomplete SHA evidence" in html
        assert 'class="final-stage"><span class="stage-count">3</span>' in html
        assert client.get("/report.json").json()["jobs"] == report["jobs"]


def test_live_fixture_without_webhook_secret_reports_a_blocker(tmp_path):
    """Show LIVE mode as blocked when its webhook secret is absent, even with empty storage."""

    # ELI5: LIVE mode with no secret is a configuration blocker, not a successful empty run.
    settings = simulation_settings(tmp_path).model_copy(update={"mode": "LIVE", "github_webhook_secret": SecretStr("")})
    with TestClient(create_app(settings)) as client:
        report = client.get("/report.json").json()
        assert report["metrics"]["jobs_total"] == 0
        assert any("webhook" in issue.lower() for issue in report["readiness"]["problems"])
        assert 'class="status bad">Blocked</span>' in client.get("/cases").text
