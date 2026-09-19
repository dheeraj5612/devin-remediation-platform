import json

import httpx
import pytest
from pydantic import SecretStr

from app.devin import Devin, ProviderError, Session
from conftest import enqueue


def adapter(settings, handler):
    return Devin(settings, httpx.Client(base_url="https://api.devin.ai/v3/organizations/test/", transport=httpx.MockTransport(handler)))


def live_settings(settings):
    return settings.model_copy(update={"mode": "LIVE", "run_live": True, "devin_org_id": "test",
                                       "devin_api_key": SecretStr("secret-key"), "github_token": SecretStr("github-secret")})


def test_create_upload_and_context_fields(settings, store, cases):
    settings = live_settings(settings)
    (settings.data_dir / "context.json").write_text(json.dumps({"org_id": "test", "repository": "example/superset",
                                                              "playbook_id": "playbook", "note_id": "knowledge"}))
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/attachments"):
            assert "multipart/form-data" in request.headers["content-type"]
            assert b"secret-key" not in request.content
            return httpx.Response(200, json={"attachment_id": "a", "url": "https://attachments.example/evidence"})
        payload = json.loads(request.content)
        assert payload["repos"] == ["example/superset"]
        assert "repositories" not in payload
        assert payload["max_acu_limit"] == 5
        assert type(payload["max_acu_limit"]) is int
        assert payload["playbook_id"] == "playbook"
        assert payload["knowledge_ids"] == ["knowledge"]
        assert payload["attachment_urls"] == ["https://attachments.example/evidence"]
        assert payload["structured_output_required"] is True
        assert payload["resumable"] is True
        assert "-p probe" not in payload["prompt"]
        return httpx.Response(200, json={"session_id": "s", "status": "new", "url": "https://app.devin.ai/s"})

    devin = adapter(settings, handle)
    session = devin.create(store.get(enqueue(store)), cases["histogram-invalid"], {"confirmed": True})
    assert session.session_id == "s"
    assert len(seen) == 2


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (500, True), (503, True)])
def test_http_errors_redacted(settings, status, retryable):
    devin = adapter(settings, lambda _: httpx.Response(status, text="SECRET_RESPONSE"))
    with pytest.raises(ProviderError) as result:
        devin.get("s")
    assert result.value.retryable == retryable
    assert "SECRET_RESPONSE" not in str(result.value)


def test_timeout_and_malformed_response(settings):
    def timeout(request):
        raise httpx.ReadTimeout("secret", request=request)

    with pytest.raises(ProviderError, match="transport") as error:
        adapter(settings, timeout).get("s")
    assert error.value.retryable
    for response in [httpx.Response(200, text="broken"), httpx.Response(200, json=[]), httpx.Response(200, json={})]:
        with pytest.raises(ProviderError, match="Malformed"):
            adapter(settings, lambda _, response=response: response).get("s")


def test_get_and_same_session_correction(settings):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.method == "POST":
            assert json.loads(request.content)["message"].startswith("Independent evaluation")
            return httpx.Response(204)
        return httpx.Response(200, json={"session_id": "s", "status": "running", "status_detail": "finished"})

    devin = adapter(live_settings(settings), handle)
    assert devin.get("s").finished
    devin.correct("s", "Regression escaped")
    assert paths == ["/v3/organizations/test/sessions/s", "/v3/organizations/test/sessions/s/messages"]


def test_paginated_recovery_and_status(settings, store):
    job = store.get(enqueue(store))
    pages = []

    def handle(request):
        pages.append(request.url.params.get("after"))
        if not request.url.params.get("after"):
            return httpx.Response(200, json={"items": [], "has_next_page": True, "end_cursor": "next"})
        return httpx.Response(200, json={"items": [{"session_id": "s", "status": "running", "tags": [f"remediation:{job.id}"]}], "has_next_page": False})

    assert adapter(settings, handle).find(job).session_id == "s"
    assert pages == [None, "next"]
    assert Session(session_id="s", status="running", status_detail="finished").finished
    assert Session(session_id="s", status="suspended", status_detail="out_of_credits").failed


def test_bootstrap_reuses_context(settings, cases):
    posts = []

    def handle(request):
        path = request.url.path
        if request.method == "GET":
            items = [{"title": "superset-test-repair-v1", "playbook_id": "p"}] if path.endswith("playbooks") else []
            return httpx.Response(200, json={"items": items, "has_next_page": False})
        payload = json.loads(request.content)
        posts.append(path)
        assert set(payload) == {"name", "body", "trigger"}
        return httpx.Response(200, json={"note_id": "k"})

    assert adapter(live_settings(settings), handle).bootstrap(cases) == {"playbook_id": "p", "note_id": "k"}
    assert posts == ["/v3/organizations/test/knowledge/notes"]
