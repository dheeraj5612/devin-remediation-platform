"""Every webhook admission gate: signature, size, event/label filters, repo identity, issue binding, dedupe."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.simulation import signed_event


def post(client, settings, change=None, header_change=None):
    body, headers = signed_event(settings, 101, "delivery-101")
    if change:
        payload = json.loads(body)
        change(payload)
        body = json.dumps(payload).encode()
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
            settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    headers.update(header_change or {})
    return client.post("/webhooks/github", content=body, headers=headers)


def test_signed_delivery_and_deduplication(client, settings):
    first = post(client, settings)
    duplicate = post(client, settings)
    logical = post(client, settings, header_change={"X-GitHub-Delivery": "different-delivery"})
    assert first.status_code == 202
    assert first.json()["status"] == "QUEUED"
    assert not first.json()["duplicate"]
    assert duplicate.json()["job_id"] == logical.json()["job_id"] == first.json()["job_id"]
    assert duplicate.json()["duplicate"] and logical.json()["duplicate"]
    assert len(client.app.state.store.jobs()) == 1
    assert client.get("/metrics").json()["duplicate_deliveries"] == 1


@pytest.mark.parametrize("signature", ["", "sha1=bad", "sha256=bad", "sha256=" + "0" * 64])
def test_bad_signature(client, settings, signature):
    assert post(client, settings, header_change={"X-Hub-Signature-256": signature}).status_code == 401
    assert not client.app.state.store.jobs()


@pytest.mark.parametrize("change,status", [
    (lambda body: body["repository"].update(id=43), 403),
    (lambda body: body["repository"].update(full_name="attacker/superset"), 403),
    (lambda body: body["issue"].update(number=999), 422),
    (lambda body: body["issue"].update(number="101"), 400),
    (lambda body: body.update(action="opened"), 202),
    (lambda body: body["label"].update(name="Devin-remediate"), 202),
    (lambda body: body.pop("issue"), 400),
])
def test_routing_filters(client, settings, change, status):
    assert post(client, settings, change).status_code == status
    assert not client.app.state.store.jobs()


def test_ping_and_unrelated_event(client, settings):
    assert post(client, settings, header_change={"X-GitHub-Event": "ping"}).json() == {"status": "pong"}
    assert post(client, settings, header_change={"X-GitHub-Event": "push"}).json() == {"status": "ignored"}


@pytest.mark.parametrize("delivery", ["", "a/b", "a" * 201])
def test_missing_or_unsafe_delivery(client, settings, delivery):
    assert post(client, settings, header_change={"X-GitHub-Delivery": delivery}).status_code == 400


def test_raw_bytes_are_signed(client, settings):
    body, headers = signed_event(settings, 101, "delivery")
    assert client.post("/webhooks/github", content=body + b" ", headers=headers).status_code == 401


def test_oversized_body(client):
    assert client.post("/webhooks/github", content=b"x" * 262145).status_code == 413


def test_malformed_signed_json(client, settings):
    raw = b"not-json"
    signature = hmac.new(settings.github_webhook_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    response = client.post("/webhooks/github", content=raw,
                           headers={"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "issues"})
    assert response.status_code == 400


def test_live_requires_confirmation_and_explicit_enable(live):
    with TestClient(create_app(live)) as client:
        assert post(client, live).status_code == 202
    live.enable_live = False
    with TestClient(create_app(live)) as client:
        assert post(client, live).status_code == 503
    live.enable_live = True
    for proof in (live.storage / "baselines").glob("*.json"):
        proof.unlink()
    with TestClient(create_app(live)) as client:
        assert post(client, live).status_code == 422


def test_health_dashboard_and_unknown_job(client):
    assert client.get("/healthz").json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "SIMULATION" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/jobs/does-not-exist").status_code == 404
