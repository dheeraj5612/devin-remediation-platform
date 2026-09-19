import base64

import httpx
import pytest

from app.devin import Session
from app.github import GitHub, ScopeError
from conftest import enqueue


def adapter(settings, job, case, mutate=None):
    path = case.allowed_paths[0]
    sha = "a" * 40
    pr = {"state": "open", "draft": False, "body": f"Remediation job: {job.id}", "changed_files": 1,
          "base": {"repo": {"id": 1}, "ref": "remediation-demo", "sha": case.baseline_sha},
          "head": {"repo": {"id": 1}, "ref": f"remediation/{job.id}", "sha": sha}}
    changed = [{"filename": path, "status": "modified", "additions": 5, "deletions": 2}]
    blob = {"type": "file", "encoding": "base64", "size": 10,
            "content": base64.b64encode(b"source text").decode()}
    if mutate:
        mutate(pr, changed, blob)

    def handle(request):
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={"merge_base_commit": {"sha": case.baseline_sha}, "files": changed})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=changed)
        if "/git/commits/" in request.url.path:
            assert request.url.path.endswith(sha)
            return httpx.Response(200, json={"tree": {"sha": "tree-0"}})
        if "/git/trees/" in request.url.path:
            index = int(request.url.path.rsplit("-", 1)[1])
            last = index == len(path.split("/")) - 1
            mode = ("120000" if blob["type"] == "symlink" else "100644") if last else "040000"
            return httpx.Response(200, json={"tree": [{"path": path.split("/")[index], "mode": mode, "sha": f"tree-{index + 1}"}]})
        if "/git/blobs/" in request.url.path:
            return httpx.Response(200, json=blob)
        return httpx.Response(200, json=pr)

    return GitHub(settings, httpx.Client(base_url="https://api.github.com/repos/example/superset/", transport=httpx.MockTransport(handle)))


def test_exact_sha_and_scope(settings, store, cases):
    job = store.get(enqueue(store))
    case = cases[job.case_id]
    github = adapter(settings, job, case)
    session = Session(session_id="s", status="exit", pull_requests=[{"pr_url": "https://github.com/example/superset/pull/4"}])
    candidate = github.discover(job, session, case)
    assert candidate.sha == "a" * 40
    assert candidate.files == {case.allowed_paths[0]: "source text"}
    assert github.unchanged(candidate)


@pytest.mark.parametrize("problem", ["repository", "branch", "baseline", "marker", "workflow", "rename", "large", "symlink", "empty"])
def test_scope_rejected_before_execution(settings, store, cases, problem):
    job = store.get(enqueue(store))
    case = cases[job.case_id]

    def mutate(pr, changed, blob):
        if problem == "repository": pr["head"]["repo"]["id"] = 99
        if problem == "branch": pr["head"]["ref"] = "different"
        if problem == "baseline": pr["base"]["sha"] = "b" * 40
        if problem == "marker": pr["body"] = ""
        if problem == "workflow": changed[0]["filename"] = ".github/workflows/ci.yml"
        if problem == "rename": changed[0]["status"] = "renamed"
        if problem == "large": changed[0]["additions"] = 201
        if problem == "symlink": blob["type"] = "symlink"
        if problem == "empty": changed.clear()

    github = adapter(settings, job, case, mutate)
    session = Session(session_id="s", status="exit", pull_requests=[{"pr_url": "https://github.com/example/superset/pull/4"}])
    with pytest.raises(ScopeError):
        github.discover(job, session, case)


@pytest.mark.parametrize("url", ["https://evil.example/pull/1", "https://github.com/apache/superset/pull/1", "file:///etc/passwd"])
def test_untrusted_pr_urls_are_not_fetched(settings, store, cases, url):
    job = store.get(enqueue(store))
    github = adapter(settings, job, cases[job.case_id])
    session = Session(session_id="s", status="exit", structured_output={"pr_url": url})
    with pytest.raises(ScopeError):
        github.discover(job, session, cases[job.case_id])
