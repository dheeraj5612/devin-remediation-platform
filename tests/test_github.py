"""GitHub adapter: only exact-repo PR URLs are discovered; PR must be open/non-draft/right base/same fork."""

import httpx
import pytest

from app.devin import RemoteError, SessionState
from app.github import GitHub


def payload(settings):
    """Build a valid open PR response for the configured repository."""
    return {"state": "open", "merged": False, "draft": False,
            "base": {"ref": settings.base_branch, "repo": {"id": settings.github_repository_id}},
            "head": {"sha": "a" * 40, "repo": {"id": settings.github_repository_id}}}


def api(settings, data):
    """Build the adapter with a deterministic in-process HTTP transport."""
    return GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))))


def test_discovers_only_exact_repository_pr_url(settings):
    """Discovery ignores lookalike hosts and repositories before fetching a candidate."""
    github = api(settings, payload(settings))
    state = SessionState(session_id="test", status="running", pull_requests=[
        {"pr_url": "https://github.com/demo/superset/pull/7"},
        {"pr_url": "https://github.com/attacker/superset/pull/99"},
        {"pr_url": "https://github.com.evil/demo/superset/pull/42"},
    ])
    candidate = github.discover(state)
    assert candidate.number == 7
    assert candidate.sha == "a" * 40
    assert candidate.url == "https://github.com/demo/superset/pull/7"
    assert github.discover(state.model_copy(update={"pull_requests": []})) is None


@pytest.mark.parametrize("change", [
    lambda data: data.update(state="closed"), lambda data: data.update(draft=True),
    lambda data: data["base"].update(ref="master"),
    lambda data: data["head"]["repo"].update(id=999),
    lambda data: data["base"]["repo"].update(id=999),
    lambda data: data["head"].update(sha="not-a-sha"),
    lambda data: data.update(head=None),
])
def test_invalid_candidate_metadata(settings, change):
    """Invalid state, fork, branch, or SHA metadata fails closed."""
    data = payload(settings)
    change(data)
    with pytest.raises(RemoteError):
        api(settings, data).candidate(7)


def test_multiple_candidates_require_human_selection(settings):
    """Two matching PRs require manual selection instead of arbitrary automation."""
    state = SessionState(session_id="test", status="running", pull_requests=[
        {"pr_url": f"https://github.com/demo/superset/pull/{number}"} for number in [7, 8]])
    with pytest.raises(RemoteError, match="Multiple"):
        api(settings, payload(settings)).discover(state)


def test_application_case_branch_is_checked_independently_of_global_branch(settings):
    """An application case may accept its own base branch while rejecting another branch."""
    # ELI5: the same PR response is accepted only when the caller supplies its approved branch.
    data = payload(settings)
    data["base"]["ref"] = "remediation-import-yaml"
    github = api(settings, data)
    assert github.candidate(7, "remediation-import-yaml").sha == "a" * 40
    with pytest.raises(RemoteError):
        github.candidate(7, "another-case-branch")
