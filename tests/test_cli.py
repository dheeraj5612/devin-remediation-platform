"""CLI: pr-status posts once then edits the same PR comment in place, keyed by the hidden marker."""

import httpx
import pytest

import app.cli as cli
from app.db import Store
from app.github import GitHub


def make_pr_job(live):
    """Enqueue a live job bound to an approved case and give it a candidate PR."""
    store = Store(live)
    job, _ = store.enqueue("delivery-1", live.github_repository, 101, "histogram-invalid-column")
    job = store.change(job.id, "DEVIN_RUNNING", status="DEVIN_RUNNING")
    job = store.change(job.id, "PR_OPENED", status="PR_OPENED",
                        candidate_pr_url=f"https://github.com/{live.github_repository}/pull/7", candidate_sha="a" * 40)
    return job


def install_fake_github(monkeypatch, handler):
    """Point app.cli.GitHub at a deterministic in-process transport for every constructed client."""
    def factory(settings):
        return GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
                                              transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(cli, "GitHub", factory)


def test_pr_status_command_posts_then_edits_the_same_comment(live, monkeypatch):
    """No existing marker comment posts once; a later run with that comment now listed edits it in place."""
    job = make_pr_job(live)
    seen = []
    state = {"comments": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=state["comments"])
        if request.method == "POST":
            state["comments"] = [{"id": 42, "body": request.content.decode()}]
            return httpx.Response(201, json={"id": 42})
        raise AssertionError("unexpected method")

    install_fake_github(monkeypatch, handler)
    first = cli.pr_status_command(live, job.id)
    assert first["comment_id"] == 42
    assert first["pr_number"] == 7
    assert seen[0][0] == "GET"  # Idempotency check always searches comments first.
    assert seen[1][0] == "POST"  # No marker was found, so the first run creates the comment.

    seen.clear()

    def handler_edit(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=state["comments"])
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 42})
        raise AssertionError("unexpected method")

    install_fake_github(monkeypatch, handler_edit)
    second = cli.pr_status_command(live, job.id)
    assert second["comment_id"] == 42
    assert seen[0][0] == "GET"
    assert seen[1][0] == "PATCH"  # The marker was found this time, so it edits in place.


def test_pr_status_command_refuses_without_a_pr(live):
    """A job with no observed candidate PR yet cannot post a PR status card."""
    store = Store(live)
    job, _ = store.enqueue("delivery-2", live.github_repository, 101, "histogram-invalid-column")
    with pytest.raises(ValueError, match="no candidate PR"):
        cli.pr_status_command(live, job.id)
