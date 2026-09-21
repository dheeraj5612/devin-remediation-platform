"""Devin API client against a mock transport: request shape, error sanitizing, pagination, bootstrap idempotency."""

import json

import httpx
import pytest

from app.cases import Registry
from app.devin import Devin, PLAYBOOK, RemoteError
from app.models import Job


def adapter(settings, handler):
    client = httpx.Client(base_url=f"https://api.devin.ai/v3/organizations/{settings.devin_org_id}/",
                          transport=httpx.MockTransport(handler))
    return Devin(settings, client)


def test_session_creation_uses_v3_context_evidence_and_budget(live):
    calls = []
    def handler(req):
        calls.append(req)
        assert req.headers["Authorization"] == "Bearer simulation-only"
        if req.url.path.endswith("/attachments"):
            assert "multipart/form-data" in req.headers["Content-Type"]
            assert b"baseline_proof" in req.content
            return httpx.Response(200, json={"url": "https://attachments.example/evidence"})
        body = json.loads(req.content)
        assert body["repos"] == [live.github_repository]
        assert body["max_acu_limit"] == live.devin_max_acu
        assert body["playbook_id"] == "playbook-test"
        assert body["knowledge_ids"] == ["note-test"]
        assert body["attachment_urls"] == ["https://attachments.example/evidence"]
        assert "drp-job-test" in body["tags"]
        assert body["structured_output_required"]
        assert "preserve" in body["prompt"].lower()
        return httpx.Response(200, json={"session_id": "devin-test", "status": "new", "url": "https://app.devin.ai/s/test"})
    devin = adapter(live, handler)
    case = Registry(live).cases["histogram-invalid-column"]
    job = Job(id="job-test", repository=live.github_repository, issue_number=101)
    assert devin.create_session(job, case).session_id == "devin-test"
    assert [req.url.path for req in calls] == [
        "/v3/organizations/org-test/attachments", "/v3/organizations/org-test/sessions"]


def test_get_session_and_correction(live):
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            assert json.loads(req.content) == {"message": "Scoped correction"}
            return httpx.Response(204)
        return httpx.Response(200, json={"session_id": "devin-test", "status": "suspended", "status_detail": "finished"})
    devin = adapter(live, handler)
    assert devin.get_session("devin-test").status == "suspended"
    devin.send_correction("devin-test", "Scoped correction")
    assert calls[-1] == ("POST", "/v3/organizations/org-test/sessions/devin-test/messages")


@pytest.mark.parametrize("code,retryable", [(401, False), (403, False), (429, True), (500, True), (503, True)])
def test_remote_errors_are_sanitized(live, code, retryable):
    devin = adapter(live, lambda req: httpx.Response(code, text="SECRET private prompt", headers={"Retry-After": "1000"}))
    with pytest.raises(RemoteError) as error:
        devin.get_session("test")
    assert error.value.retryable is retryable
    assert error.value.retry_after == 60
    assert "SECRET" not in str(error.value)


def test_timeout_is_not_automatically_retried(live):
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("secret", request=req)
    with pytest.raises(RemoteError, match="timeout"):
        adapter(live, handler).get_session("test")
    assert len(calls) == 1


@pytest.mark.parametrize("data", [{}, {"session_id": "bad/path", "status": "new"},
    {"session_id": "test", "status": "new", "url": "javascript:alert(1)"}])
def test_invalid_response_is_rejected(live, data):
    with pytest.raises(RemoteError, match="Invalid session"):
        adapter(live, lambda req: httpx.Response(200, json=data)).get_session("test")


def test_invalid_json(live):
    with pytest.raises(RemoteError, match="JSON"):
        adapter(live, lambda req: httpx.Response(200, text="not-json")).get_session("test")


def test_session_reconciliation_follows_cursors(live):
    def handler(req):
        if "after" not in req.url.params:
            return httpx.Response(200, json={"items": [], "has_next_page": True, "end_cursor": "cursor-2"})
        assert req.url.params["after"] == "cursor-2"
        return httpx.Response(200, json={"items": [{"session_id": "found", "status": "running", "tags": ["drp-job"]}],
                                         "has_next_page": False})
    assert adapter(live, handler).find_session("job").session_id == "found"


def test_duplicate_tag_and_broken_cursor_fail_closed(live):
    record = {"session_id": "found", "status": "running", "tags": ["drp-job"]}
    devin = adapter(live, lambda req: httpx.Response(200, json={"items": [record, record]}))
    with pytest.raises(RemoteError, match="Multiple sessions"):
        devin.find_session("job")
    devin = adapter(live, lambda req: httpx.Response(200, json={"items": [], "has_next_page": True, "end_cursor": "same"}))
    with pytest.raises(RemoteError, match="cursor"):
        devin.find_session("job")


def test_context_bootstrap_is_reusable_and_uses_notes_endpoint(live):
    resources = {"playbooks": [], "knowledge/notes": []}
    writes = []
    def handler(req):
        path = req.url.path.split("org-test/")[1]
        if req.method == "GET":
            return httpx.Response(200, json={"items": resources[path], "has_next_page": False})
        body = json.loads(req.content)
        key = "playbook_id" if path == "playbooks" else "note_id"
        resources[path].append({**body, key: f"new-{key}"})
        writes.append(path)
        return httpx.Response(200, json=resources[path][-1])
    devin = adapter(live, handler)
    first = devin.bootstrap(Registry(live))
    assert first == devin.bootstrap(Registry(live))
    assert writes == ["playbooks", "knowledge/notes"]
    assert resources["playbooks"][0]["body"] == PLAYBOOK
    assert "build_sqlalchemy_uri" not in resources["playbooks"][0]["body"]


def test_context_rejects_drift(live):
    devin = adapter(live, lambda req: httpx.Response(200, json={"items": [
        {"title": "superset-test-repair-v1", "body": "unexpected", "playbook_id": "id"}]}))
    with pytest.raises(ValueError, match="differs"):
        devin.bootstrap(Registry(live))


def test_http_client_is_org_scoped_and_credential_not_in_url(live):
    devin = Devin(live)
    assert str(devin.client.base_url) == "https://api.devin.ai/v3/organizations/org-test/"
    assert devin.client.headers["Authorization"].startswith("Bearer ")
    assert devin.client.follow_redirects is False
    devin.client.close()
