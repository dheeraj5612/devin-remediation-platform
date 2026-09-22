"""Presentation regression checks: actual routes, honest evidence, and no write actions."""

import copy
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import create_app
from app.presentation import human_time, linked_verdict, recorded_evidence, workbench
from app.simulation import run_demo, simulation_settings


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    """One deterministic six-run store; tests only read it."""
    settings = simulation_settings(tmp_path_factory.mktemp("presentation"))
    run_demo(settings)
    with TestClient(create_app(settings)) as client:
        yield client, client.get("/report.json").json()


@pytest.mark.parametrize("path,title", [
    ("/", "A repair is"), ("/dashboard", "The evidence desk."),
    ("/cases", "Bounded by a contract."), ("/evidence", "A real run."),
])
def test_routes_and_strict_asset_policy(populated, path, title):
    """Production pages keep functioning with local assets and a strict CSP."""
    client, _ = populated
    response = client.get(path)
    assert response.status_code == 200 and title in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert '<script src="/static/app.js" defer>' in response.text
    assert 'href="#main"' in response.text


@pytest.mark.parametrize("path,content_type", [
    ("/static/app.css", "text/css"), ("/static/app.js", "javascript"),
    ("/static/wordmark.svg", "image/svg+xml"), ("/static/favicon.svg", "image/svg+xml"),
    ("/static/favicon.ico", "image/"), ("/static/apple-touch-icon.png", "image/png"),
    ("/static/social.png", "image/png"),
])
def test_all_brand_and_ui_assets_are_served(populated, path, content_type):
    client, _ = populated
    response = client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert len(response.content) > 100


@pytest.mark.parametrize("params,total", [
    ({}, 6), ({"view": "attention"}, 2), ({"view": "verified"}, 4),
    ({"view": "active"}, 0), ({"kind": "application"}, 2),
    ({"query": "105"}, 1), ({"query": "IMPORT-UNPARSEABLE"}, 2),
    ({"kind": "application", "view": "verified"}, 1),
])
def test_filter_counts_never_rewrite_global_evidence(populated, params, total):
    _, report = populated
    original = copy.deepcopy(report)
    view = workbench(report, **params)
    assert view["total"] == total
    assert report == original
    assert report["metrics"]["jobs_total"] == 6


def test_attention_first_and_exact_sha_search(populated):
    _, report = populated
    view = workbench(report)
    assert [job["status"] for job in view["rows"][:2]] == ["ESCALATED", "ESCALATED"]
    sha = report["jobs"][0]["candidate_sha"]
    assert workbench(report, query=sha)["total"] == 1


def test_pagination_invalid_inputs_and_encoded_links(populated):
    _, report = populated
    large = {**report, "jobs": report["jobs"] * 8}
    result = workbench(large, page=999)
    assert result["page"] == 3 and len(result["rows"]) == 8
    assert result["start"] == 41 and result["end"] == 48
    assert workbench(large, page=-9)["page"] == 1
    invalid = workbench(report, view="broken", kind="broken", sort="broken")
    assert (invalid["view"], invalid["kind"], invalid["sort"]) == ("all", "all", "attention")
    encoded = workbench(report, query='a&<"')
    assert "q=a%26%3C%22" in encoded["tabs"][0]["url"]


def test_search_escaping_and_filtered_export(populated):
    client, report = populated
    response = client.get("/dashboard", params={"q": '<script>alert("x")</script>'})
    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    assert 'value="&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;"' in response.text
    assert "No runs match these filters." in response.text
    assert "Export all evidence" in response.text
    assert client.get("/report.json?q=not-found").json()["jobs"] == report["jobs"]
    assert client.get("/dashboard?page=not-an-integer").status_code == 200


def test_source_modes_and_archive_do_not_mutate_storage(client):
    """The archived success cannot become an empty workspace's success metric."""
    assert "No persisted jobs" in client.get("/dashboard").text
    assert "ARCHIVE" in client.get("/evidence").text
    assert "separate from current workspace metrics" in client.get("/evidence").text
    assert client.get("/report.json").json()["metrics"]["jobs_total"] == 0
    assert client.get("/cases").status_code == 200
    assert not client.app.state.store.jobs()


def test_case_specific_checks_and_synthetic_artifacts(populated):
    client, report = populated
    application = next(job for job in report["jobs"] if job["issue_number"] == 105)
    test_repair = next(job for job in report["jobs"] if job["issue_number"] == 101)
    app_html = client.get(f"/jobs/{application['id']}").text
    test_html = client.get(f"/jobs/{test_repair['id']}").text
    assert "Application: PASS" in app_html and "Regression challenge" not in app_html
    assert "Regression challenge" in test_html and "Regression caught" in test_html
    assert 'href="https://github.com/demo/' not in app_html
    assert "Synthetic SHA matched" in app_html
    assert "data-secondary" in app_html and "Show all" in app_html


def test_metadata_and_error_recovery(populated):
    client, _ = populated
    assert 'content="index,follow"' in client.get("/").text
    assert 'content="noindex,nofollow"' in client.get("/dashboard").text
    assert "Disallow: /jobs/" in client.get("/robots.txt").text
    response = client.get("/jobs/missing", headers={"Accept": "text/html"})
    assert response.status_code == 404 and "Back to workbench" in response.text
    assert client.get("/jobs/missing").json() == {"detail": "Unknown job"}
    assert client.post("/dashboard").status_code == 405


def test_database_failure_is_not_an_empty_success(client, monkeypatch):
    def fail():
        raise OperationalError("private SQL", {}, Exception("do-not-leak-this"))
    monkeypatch.setattr(client.app.state.store, "jobs", fail)
    response = client.get("/dashboard", headers={"Accept": "text/html"})
    assert response.status_code == 503
    assert "No empty or successful result is inferred" in response.text
    assert "do-not-leak-this" not in response.text and "private SQL" not in response.text
    assert client.get("/report.json").status_code == 503


def test_live_empty_storage_stays_live_and_blocked(settings):
    config = settings.model_copy(update={"mode": "LIVE"})
    with TestClient(create_app(config)) as client:
        assert "Live control-plane records only" in client.get("/dashboard").text
        assert "Pilot blocked" in client.get("/cases").text
        assert client.get("/report.json").json()["metrics"]["jobs_total"] == 0


@pytest.mark.parametrize("value,expected", [
    (None, "Not recorded"), ("bad", "Time unavailable"),
    ("2026-09-22T12:15:00+02:00", "10:15:00 UTC"),
    ("2026-09-22T10:15:00", "10:15:00 UTC"),
])
def test_utc_formatting(value, expected):
    assert human_time(value, short=True) == expected


def test_archive_fails_closed_on_a_mismatched_sha(tmp_path, monkeypatch):
    from app import presentation
    valid = json.loads(presentation.ARCHIVE_PATH.read_text())
    assert recorded_evidence()["application"] == "PASS"
    (tmp_path / "evidence").mkdir()
    path = tmp_path / "evidence/live-application.json"
    monkeypatch.setattr(presentation, "ARCHIVE_PATH", path)
    assert recorded_evidence() is None
    valid["candidate"]["validated_sha"] = "0" * 40
    path.write_text(json.dumps(valid))
    assert recorded_evidence() is None
    path.write_text("broken JSON")
    assert recorded_evidence() is None


def test_unlinked_verification_is_not_endorsed(populated):
    client, report = populated
    damaged = copy.deepcopy(report)
    job = damaged["jobs"][0]
    job.update(status="VERIFIED", candidate_sha="a" * 40, validated_sha="b" * 40)
    assert linked_verdict(job) == "INCOMPLETE"
    with patch("app.views.build_report", return_value=damaged):
        html = client.get(f"/jobs/{job['id']}").text
    assert "The verification link is incomplete." in html
    assert "The check passed. Review comes next." not in html
    job.update(candidate_sha="placeholder", validated_sha="placeholder")
    with patch("app.views.build_report", return_value=damaged):
        html = client.get(f"/jobs/{job['id']}").text
    assert "Exact SHA matched" not in html


def test_missing_registry_case_never_borrows_another_baseline(populated):
    client, report = populated
    damaged = copy.deepcopy(report)
    damaged["jobs"][0]["case_id"] = "retired-case"
    with patch("app.views.build_report", return_value=damaged):
        html = client.get(f"/jobs/{damaged['jobs'][0]['id']}").text
    assert "original contract is no longer registered" in html
