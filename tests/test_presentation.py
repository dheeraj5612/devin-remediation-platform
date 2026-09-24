"""Presentation regression checks: actual routes, honest evidence, and no write actions."""

import copy
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import Store
from app.main import create_app
from app.presentation import human_time, linked_verdict, recorded_evidence, workbench
from app.simulation import run_demo, simulation_settings


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    """One deterministic six-run store; tests only read it."""

    # ELI5: build the demo once for this module so route tests share stable evidence without writes.
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

    # ELI5: each public HTML route must render its heading and deny inline or third-party scripts.
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
    """Serve every declared brand and interface asset with its expected media type."""

    # ELI5: request each declared asset as a browser would and reject empty placeholders.
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
    """Apply each dashboard filter to a view copy while preserving the full report metrics."""

    # ELI5: filters change what the reviewer sees, not the stored six-job evidence totals.
    _, report = populated
    original = copy.deepcopy(report)
    view = workbench(report, **params)
    assert view["total"] == total
    assert report == original
    assert report["metrics"]["jobs_total"] == 6


def test_attention_first_and_exact_sha_search(populated):
    """Order attention rows first and allow an exact candidate SHA to identify one run."""

    # ELI5: unresolved rows lead the desk, while an immutable candidate SHA finds one record.
    _, report = populated
    view = workbench(report)
    assert [job["status"] for job in view["rows"][:2]] == ["ESCALATED", "ESCALATED"]
    sha = report["jobs"][0]["candidate_sha"]
    assert workbench(report, query=sha)["total"] == 1


def test_pagination_invalid_inputs_and_encoded_links(populated):
    """Clamp invalid pages and filters while URL-encoding query text in generated links."""

    # ELI5: a bad page falls back to the last page, and hostile query text remains safe in links.
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
    """Escape hostile search text in HTML and keep JSON export independent of dashboard filters."""

    # ELI5: user search is displayed as text, while the export always contains the complete report.
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

    # ELI5: an empty current store can show historical evidence without pretending it ran locally.
    assert "No persisted jobs" in client.get("/dashboard").text
    assert "ARCHIVE" in client.get("/evidence").text
    assert "separate from current workspace metrics" in client.get("/evidence").text
    assert client.get("/report.json").json()["metrics"]["jobs_total"] == 0
    assert client.get("/cases").status_code == 200
    assert not client.app.state.store.jobs()


def test_case_specific_checks_and_synthetic_artifacts(populated):
    """Render application and test cases with their distinct evidence language and safe links."""

    # ELI5: application and test-quality records use different proof labels but share safe rendering.
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


def test_job_page_oracle_method_cues_and_infra_revalidated(populated, live):
    """Show the right oracle-method cues per case kind, plus the recovery cue when it happened."""

    # ELI5: application and test-quality proof pages explain their own oracle method, not the other one's.
    client, report = populated
    application = next(job for job in report["jobs"] if job["case_kind"] == "application")
    test_repair = next(job for job in report["jobs"] if job["case_kind"] == "test_quality")
    app_html = client.get(f"/jobs/{application['id']}").text
    test_html = client.get(f"/jobs/{test_repair['id']}").text
    assert "Controls first: valid inputs must still pass." in app_html
    assert "Some crash = CONTRACT_FAILED; partial fix rejected." in app_html
    assert "Known regression injected into the code." not in app_html
    assert "Old test missed it; new test must catch it." in test_html
    assert "Controls first: valid inputs must still pass." not in test_html

    store = Store(live)
    job, duplicate = store.enqueue("delivery-infra", live.github_repository, 101, "histogram-invalid-column")
    assert not duplicate
    store.change(job.id, "FAILED", status="FAILED", validation_status="INFRA_ERROR",
                 candidate_sha="a" * 40, candidate_pr_number=10, devin_session_id="session-original")
    store.record_infra_revalidation(job.id, "a" * 40,
                                     {"outcome": "VERIFIED", "summary": "Independent checks passed",
                                      "evidence": {"sha": "a" * 40}})
    store.engine.dispose()
    with TestClient(create_app(live)) as live_client:
        live_report = live_client.get("/report.json").json()
        revalidated_job = next(j for j in live_report["jobs"] if j["issue_number"] == 101)
        html = live_client.get(f"/jobs/{revalidated_job['id']}").text
    assert "Evaluator broke once (INFRA_ERROR, never a pass); re-checked the same commit, no new Devin session." in html


def test_provider_activity_is_mode_labeled_and_bootstrap_stays_unrecorded(populated):
    """Show persisted fake operation records while keeping absent bootstrap proof explicit."""

    # ELI5: simulation emits provider-shaped operation rows but does not run live bootstrap.
    client, report = populated
    provider = report["provider_api"]
    assert provider["mode"] == "SIMULATION"
    assert provider["event_count"] > 0 and provider["success_count"] < provider["event_count"]
    assert provider["latency_basis"] == "synthetic"
    assert provider["operations"]["session_poll"]["event_count"] > 0
    assert provider["operations"]["session_poll"]["latency_ms"]["median"] == 0
    assert provider["bootstrap"]["status"] == "NOT_RECORDED"
    assert provider["bootstrap"]["playbook"]["status"] == "NOT_RECORDED"
    assert provider["bootstrap"]["knowledge"]["status"] == "NOT_RECORDED"
    assert "playbook_id" not in json.dumps(provider)
    assert "body_sha256" not in json.dumps(provider)
    dashboard = client.get("/dashboard").text
    assert "Devin API activity" in dashboard
    assert f">{provider['event_count']}</strong><span>operation records" in dashboard
    assert f">{provider['success_count']}</strong><span>successful" in dashboard
    assert f">{provider['latency_ms']['median']} ms" in dashboard
    job = report["jobs"][0]
    detail = client.get(f"/jobs/{job['id']}").text
    assert "Devin API activity" in detail
    assert "Once per repository" in detail
    assert "Sessions API · poll" in detail
    assert "Not needed in this run." in detail
    dashboard_zero = client.get("/dashboard").text
    assert "Sessions API · list by tag" in dashboard_zero


def test_provider_activity_absence_is_not_recorded(client):
    """An empty mode has no inferred provider calls or bootstrap resources."""

    # ELI5: an empty store and absent context file must remain visibly empty in the export and UI.
    report = client.get("/report.json").json()
    provider = report["provider_api"]
    assert provider["status"] == "NOT_RECORDED"
    assert provider["event_count"] == 0
    assert provider["bootstrap"]["status"] == "NOT_RECORDED"
    assert provider["bootstrap"]["timestamp"] == "NOT_RECORDED"
    dashboard = client.get("/dashboard").text
    assert "Not Recorded" in dashboard
    assert "Not needed in live runs." in dashboard


def test_dashboard_thesis_breakdown_and_when_to_use(populated):
    """The dashboard shows the opening thesis, a computed live breakdown, and a when-to-use block."""

    # ELI5: every number in the new cues must trace back to the same report, never a hardcoded count.
    client, report = populated
    dashboard = client.get("/dashboard").text
    assert "Devin writes the fix. An independent oracle proves it. A human merges." in dashboard
    verified = [job for job in report["jobs"] if linked_verdict(job) == "VERIFIED"]
    test_quality = sum(job["case_kind"] == "test_quality" for job in verified)
    application = sum(job["case_kind"] == "application" for job in verified)
    assert f"{test_quality} test repair" in dashboard
    assert f"{application} app contract" in dashboard
    metrics = report["metrics"]
    assert f"{metrics['first_pass']} of {metrics['first_pass_denominator']} verified on first pass" in dashboard
    assert "When to use it" in dashboard
    assert "Not for: vague feature work." in dashboard
    assert "Never auto-merges; human reviews every PR." in dashboard


def test_devin_api_card_shows_acu_cap_and_failure_total(populated):
    """The Devin API card quotes the real configured ACU cap and a plain failures total."""

    # ELI5: the cap must come from settings, not a hardcoded string, and failures must be visible.
    client, report = populated
    dashboard = client.get("/dashboard").text
    assert "Starts one session capped at 3 ACUs, referencing playbook, note, attachment." in dashboard
    assert "never edit the checker, never merge" in dashboard
    provider = report["provider_api"]
    assert f"{provider['event_count']} Devin API calls, {provider['event_count'] - provider['success_count']} failures" in dashboard


def test_metadata_and_error_recovery(populated):
    """Keep indexing metadata, not-found responses, and method errors within their route contracts."""

    # ELI5: check crawler hints and both HTML and JSON error shapes from one public client.
    client, _ = populated
    assert 'content="index,follow"' in client.get("/").text
    assert 'content="noindex,nofollow"' in client.get("/dashboard").text
    assert "Disallow: /jobs/" in client.get("/robots.txt").text
    response = client.get("/jobs/missing", headers={"Accept": "text/html"})
    assert response.status_code == 404 and "Back to workbench" in response.text
    assert client.get("/jobs/missing").json() == {"detail": "Unknown job"}
    assert client.post("/dashboard").status_code == 405


def test_database_failure_is_not_an_empty_success(client, monkeypatch):
    """Return a generic service failure when storage reads break instead of claiming an empty workspace."""

    # ELI5: force the storage boundary to fail and make sure private SQL text never reaches the response.
    def fail():
        """Raise a private database error for the route-boundary test."""

        raise OperationalError("private SQL", {}, Exception("do-not-leak-this"))

    monkeypatch.setattr(client.app.state.store, "jobs", fail)
    response = client.get("/dashboard", headers={"Accept": "text/html"})
    assert response.status_code == 503
    assert "No empty or successful result is inferred" in response.text
    assert "do-not-leak-this" not in response.text and "private SQL" not in response.text
    assert client.get("/report.json").status_code == 503


def test_live_empty_storage_stays_live_and_blocked(settings):
    """Keep an empty LIVE workspace labelled as live and blocked rather than as simulation success."""

    # ELI5: mode labels and readiness blockers must survive even when no live rows exist.
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
    """Format absent, malformed, naive, and offset timestamps according to the UTC display contract."""

    # ELI5: every input shape gets the stable short timestamp text shown to reviewers.
    assert human_time(value, short=True) == expected


def test_archive_fails_closed_on_a_mismatched_sha(tmp_path, monkeypatch):
    """Reject archived evidence when its candidate link mismatches or its JSON becomes unreadable."""

    # ELI5: success is valid only for the original archive, an exact SHA link, and readable JSON.
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
    """Keep incomplete SHA links out of the verified language on the run detail page."""

    # ELI5: make a row look verified while breaking its SHA link, then inspect the detail page.
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
    """Explain a retired case instead of borrowing another registered case's baseline evidence."""

    # ELI5: changing only the case ID must produce a retirement message, never borrowed proof.
    client, report = populated
    damaged = copy.deepcopy(report)
    damaged["jobs"][0]["case_id"] = "retired-case"
    with patch("app.views.build_report", return_value=damaged):
        html = client.get(f"/jobs/{damaged['jobs'][0]['id']}").text
    assert "original contract is no longer registered" in html
