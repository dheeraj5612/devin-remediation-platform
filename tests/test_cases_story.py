"""Audience-facing narrative blocks added to the Contracts page (/cases)."""

from fastapi.testclient import TestClient

from app.db import Store
from app.main import create_app
from app.simulation import run_demo


def test_cases_story_renders_with_empty_db(client):
    """Every narrative block must render even before any case has a recorded job."""
    response = client.get("/cases")
    assert response.status_code == 200
    text = response.text
    assert "One registry drives every case" in text
    assert "Each case pins a baseline commit, allowed files, and its oracle" in text
    assert "Issue bound to a registered case; unbound issues are refused" in text
    assert "Why trust the green check" in text
    assert "Gates before any paid run" in text
    assert "When to use DevinTrace" in text
    # ELI5: with no INFRA_REVALIDATED event in the store, the recovery callout must be omitted.
    assert "Recovered without a new session" not in text
    assert "cases-story.css" in text


def test_cases_story_renders_after_the_demo(settings):
    """The narrative blocks must also render once the simulation demo has populated the store."""
    run_demo(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/cases")
    assert response.status_code == 200
    text = response.text
    assert "One registry drives every case" in text
    assert "Why trust the green check" in text


def test_cases_story_shows_recovery_line_when_infra_revalidated(live):
    """An INFRA_REVALIDATED event on any job surfaces the derived recovery callout, by issue number."""
    store = Store(live)
    job, duplicate = store.enqueue("delivery-story", live.github_repository, 101, "histogram-invalid-column")
    assert not duplicate
    store.change(job.id, "FAILED", status="FAILED", validation_status="INFRA_ERROR",
                 candidate_sha="a" * 40, candidate_pr_number=10, devin_session_id="session-original")
    store.record_infra_revalidation(job.id, "a" * 40,
                                     {"outcome": "VERIFIED", "summary": "Independent checks passed",
                                      "evidence": {"sha": "a" * 40}})
    store.engine.dispose()

    with TestClient(create_app(live)) as client:
        response = client.get("/cases")
    assert response.status_code == 200
    assert "Recovered without a new session" in response.text
    assert "Issue #101 re-validated at the same commit" in response.text
