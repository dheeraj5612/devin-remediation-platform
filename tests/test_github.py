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


def test_upsert_issue_comment_creates_then_patches_the_same_comment(settings):
    """No saved comment id posts once; a saved id edits that same comment in place."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"id": 42})

    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(handler)))
    created = github.upsert_issue_comment(7, "first body", None)
    assert created == 42
    assert seen[0] == ("POST", f"https://api.github.com/repos/{settings.github_repository}/issues/7/comments")
    edited = github.upsert_issue_comment(7, "second body", 42)
    assert edited == 42
    assert seen[1] == ("PATCH", f"https://api.github.com/repos/{settings.github_repository}/issues/comments/42")


def test_upsert_issue_comment_reposts_when_saved_comment_was_deleted(settings):
    """A deleted status comment (PATCH 404) is replaced by a new one instead of failing forever."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(404) if request.method == "PATCH" else httpx.Response(201, json={"id": 99})

    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(handler)))
    assert github.upsert_issue_comment(7, "body", 42) == 99
    assert seen == ["PATCH", "POST"]


def test_upsert_issue_comment_rejects_malformed_response(settings):
    """A comment response without a valid integer id fails closed instead of returning garbage."""
    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "not-an-int"}))))
    with pytest.raises(RemoteError):
        github.upsert_issue_comment(7, "body", None)


@pytest.mark.parametrize("issue_number,comment_id", [(0, None), (-1, None), (7, 0), (7, -5)])
def test_upsert_issue_comment_rejects_non_positive_ids(settings, issue_number, comment_id):
    """Reject a non-positive issue or comment id before it reaches the request path."""
    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"id": 1}))))
    with pytest.raises(ValueError):
        github.upsert_issue_comment(issue_number, "body", comment_id)


def test_find_comment_by_marker_returns_the_matching_id(settings):
    """The marker search returns the id of the one comment whose body contains it, none otherwise."""
    comments = [{"id": 1, "body": "unrelated"}, {"id": 2, "body": "hello <!-- devintrace-status job=abc --> world"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(handler)))
    assert github.find_comment_by_marker(9, "<!-- devintrace-status job=abc -->") == 2
    assert github.find_comment_by_marker(9, "<!-- devintrace-status job=zzz -->") is None


def test_find_comment_by_marker_paginates_until_a_short_page(settings):
    """A full first page is followed by a second, shorter page that ends the search."""
    pages = {1: [{"id": n, "body": "x"} for n in range(100)],
             2: [{"id": 999, "body": "<!-- devintrace-status job=abc -->"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=pages[page])

    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(handler)))
    assert github.find_comment_by_marker(9, "<!-- devintrace-status job=abc -->") == 999


def test_find_comment_by_marker_rejects_non_positive_number(settings):
    """Reject a non-positive PR/issue number before it reaches the request path."""
    github = GitHub(settings, httpx.Client(base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[]))))
    with pytest.raises(ValueError):
        github.find_comment_by_marker(0, "marker")
