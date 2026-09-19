import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.demo import signed_event
from app.main import create_app


def send(client, settings, payload, event="issues", delivery="unique"):
    content = json.dumps(payload).encode()
    signature = hmac.new(settings.github_webhook_secret.get_secret_value().encode(), content, hashlib.sha256).hexdigest()
    return client.post("/webhooks/github", content=content, headers={
        "X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
    })


def test_signed_event_durable_and_idempotent(settings, store):
    with TestClient(create_app(settings, store)) as client:
        body, headers = signed_event(settings, 101, "first")
        first = client.post("/webhooks/github", content=body, headers=headers)
        assert first.status_code == 202
        assert first.json()["duplicate"] is False
        duplicate = client.post("/webhooks/github", content=body, headers=headers)
        assert duplicate.json() == {"job_id": first.json()["job_id"], "duplicate": True}
        headers["X-GitHub-Delivery"] = "another-delivery"
        assert client.post("/webhooks/github", content=body, headers=headers).json()["duplicate"]
    assert len(store.jobs("SIMULATION")) == 1
    assert store.jobs("SIMULATION")[0].status == "QUEUED"
    assert store.metrics("SIMULATION")["duplicates"] == 1


@pytest.mark.parametrize("change,status", [
    ({"repository": {"id": 2, "full_name": "example/superset"}}, 403),
    ({"repository": {"id": 1, "full_name": "apache/superset"}}, 403),
    ({"issue": {"number": 999}}, 422),
    ({"issue": {"number": -1}}, 400),
    ({"action": "opened"}, 202),
    ({"label": {"name": "Devin-remediate"}}, 202),
])
def test_webhook_filters(settings, store, change, status):
    body, _ = signed_event(settings, 101, "first")
    payload = {**json.loads(body), **change}
    with TestClient(create_app(settings, store)) as client:
        assert send(client, settings, payload).status_code == status
    assert not store.jobs("SIMULATION")


def test_signature_ping_and_invalid_json(settings, store):
    with TestClient(create_app(settings, store)) as client:
        assert client.post("/webhooks/github", content=b"{}").status_code == 401
        body, headers = signed_event(settings, 101, "first")
        assert client.post("/webhooks/github", content=body + b" ", headers=headers).status_code == 401
        assert send(client, settings, {}, "ping").json() == {"ignored": "ping"}
        assert send(client, settings, [], "issues").status_code == 400
        assert send(client, settings, {}, "push").json() == {"ignored": "event"}
        assert client.post("/webhooks/github", content=b"x" * 262145).status_code == 413


def test_live_gate_has_no_paid_side_effect(settings, store):
    live = settings.model_copy(update={"mode": "LIVE", "run_live": False})
    with TestClient(create_app(live, store)) as client:
        body, headers = signed_event(live, 101, "live")
        response = client.post("/webhooks/github", content=body, headers=headers)
        assert response.status_code == 409
    assert not store.jobs("LIVE")
