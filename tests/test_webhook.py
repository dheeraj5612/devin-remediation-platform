"""Webhook admission tests for signatures, routing, limits, and deduplication.

ELI5: these requests are the front door. The tests check that only a signed,
approved issue can become durable work and that malformed input fails closed.
"""

import hashlib  # Compute the same SHA-256 signature GitHub sends.
import hmac  # Compare signed bytes without timing leaks.
import json  # Modify the synthetic webhook payload for routing cases.

import pytest  # Parameterize rejected signatures and routing variants.
from fastapi.testclient import TestClient  # Exercise the FastAPI application in-process.

from app.main import create_app  # Build a fresh app for live gate checks.
from app.simulation import signed_event  # Create a valid signed issue event fixture.


def post(client, settings, change=None, header_change=None):
    """Post a valid signed issue event after applying optional payload or header changes."""

    body, headers = signed_event(settings, 101, "delivery-101")  # Start from the canonical simulation event.
    if change:
        payload = json.loads(body)  # Decode the event so the caller can mutate routing fields.
        change(payload)  # Apply the requested payload mutation.
        body = json.dumps(payload).encode()  # Re-serialize the exact bytes sent to the app.
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(  # Re-sign changed bytes with the fixture secret.
            settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    headers.update(header_change or {})  # Apply optional header mutations after signing.
    return client.post("/webhooks/github", content=body, headers=headers)  # Send the synthetic webhook.


def test_signed_delivery_and_deduplication(client, settings):
    """Create one job for a first delivery and suppress both duplicate forms."""

    first = post(client, settings)  # Admit the original delivery.
    duplicate = post(client, settings)  # Replay the same delivery ID.
    logical = post(client, settings, header_change={"X-GitHub-Delivery": "different-delivery"})  # Replay the issue with a new ID.
    assert first.status_code == 202  # The first signed event is accepted.
    assert first.json()["status"] == "QUEUED"  # Admission creates a queued job.
    assert not first.json()["duplicate"]  # The first event is not a duplicate.
    assert duplicate.json()["job_id"] == logical.json()["job_id"] == first.json()["job_id"]  # All deliveries point to one job.
    assert duplicate.json()["duplicate"] and logical.json()["duplicate"]  # Both replay forms are marked duplicate.
    assert len(client.app.state.store.jobs()) == 1  # The durable store contains one job.
    assert client.get("/metrics").json()["duplicate_deliveries"] == 1  # Metrics count the exact redelivery.


@pytest.mark.parametrize("signature", ["", "sha1=bad", "sha256=bad", "sha256=" + "0" * 64])
def test_bad_signature(client, settings, signature):
    """Reject empty, legacy, or incorrect HMAC signatures before admission."""

    assert post(client, settings, header_change={"X-Hub-Signature-256": signature}).status_code == 401  # Invalid signatures are unauthorized.
    assert not client.app.state.store.jobs()  # No job is written for an unauthorized request.


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
    """Apply repository, issue, action, label, and payload routing rules."""

    assert post(client, settings, change).status_code == status  # Match the expected admission response.
    assert not client.app.state.store.jobs()  # The parameterized filter cases do not enqueue work.


def test_ping_and_unrelated_event(client, settings):
    """Return a health response for ping and ignore unrelated GitHub event types."""

    assert post(client, settings, header_change={"X-GitHub-Event": "ping"}).json() == {"status": "pong"}  # Answer GitHub's ping handshake.
    assert post(client, settings, header_change={"X-GitHub-Event": "push"}).json() == {"status": "ignored"}  # Ignore events outside issue routing.


@pytest.mark.parametrize("delivery", ["", "a/b", "a" * 201])
def test_missing_or_unsafe_delivery(client, settings, delivery):
    """Require a bounded, safe delivery ID before creating an idempotency record."""

    assert post(client, settings, header_change={"X-GitHub-Delivery": delivery}).status_code == 400  # Reject missing, path-like, and oversized IDs.


def test_raw_bytes_are_signed(client, settings):
    """Verify the HMAC over raw request bytes rather than parsed JSON."""

    body, headers = signed_event(settings, 101, "delivery")  # Build a correctly signed payload.
    assert client.post("/webhooks/github", content=body + b" ", headers=headers).status_code == 401  # A trailing byte invalidates the signature.


def test_oversized_body(client):
    """Reject a request body larger than the configured admission limit."""

    assert client.post("/webhooks/github", content=b"x" * 262145).status_code == 413  # Fail before JSON parsing or storage.


def test_malformed_signed_json(client, settings):
    """Reject validly signed bytes that do not contain JSON."""

    raw = b"not-json"  # Use malformed content while keeping the bytes deterministic.
    signature = hmac.new(settings.github_webhook_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()  # Sign those exact bytes.
    response = client.post("/webhooks/github", content=raw,  # Send the malformed but authenticated request.
                           headers={"X-Hub-Signature-256": f"sha256={signature}", "X-GitHub-Event": "issues"})
    assert response.status_code == 400  # Parsing failure is a client error.


def test_live_requires_confirmation_and_explicit_enable(live):
    """Require explicit live enablement and fresh baseline evidence at admission."""

    with TestClient(create_app(live)) as client:  # Build the live app with the fixture's valid gates.
        assert post(client, live).status_code == 202  # A fully prepared live request is accepted.
    live.enable_live = False  # Remove the explicit spending confirmation.
    with TestClient(create_app(live)) as client:  # Rebuild startup state with live disabled.
        assert post(client, live).status_code == 503  # The webhook is unavailable without confirmation.
    live.enable_live = True  # Restore confirmation for the baseline proof check.
    for proof in (live.storage / "baselines").glob("*.json"):  # Remove every stored baseline artifact.
        proof.unlink()  # Make the readiness evidence stale or missing.
    with TestClient(create_app(live)) as client:  # Rebuild the app with missing proof.
        assert post(client, live).status_code == 422  # Admission rejects an unproven case.


def test_live_rejects_missing_interpreter_before_enqueue(live):
    """Reject live admission when the independent evaluator is unavailable."""
    # ELI5: make the interpreter gate fail while leaving credentials and proof valid.
    live.superset_python = live.data_dir / "missing-python"
    with TestClient(create_app(live)) as client:
        response = post(client, live)
    assert response.status_code == 503
    assert not client.app.state.store.jobs()


def test_live_rejects_incomplete_context_before_enqueue(live):
    """Reject live admission when reusable Devin context is incomplete."""
    # ELI5: keep valid JSON but remove one required provider resource identifier.
    context_path = live.storage / "context.json"
    context = json.loads(context_path.read_text())
    context.pop("note_id")
    context_path.write_text(json.dumps(context))
    with TestClient(create_app(live)) as client:
        response = post(client, live)
    assert response.status_code == 503
    assert not client.app.state.store.jobs()


def test_health_dashboard_and_unknown_job(client):
    """Expose health and dashboard pages while returning 404 for an unknown job."""

    assert client.get("/healthz").json()["status"] == "ok"  # Health endpoint reports the simulation service is alive.
    page = client.get("/")  # Load the dashboard page.
    assert page.status_code == 200  # Dashboard rendering succeeds.
    assert "SIMULATION" in page.text  # The evidence mode stays visible to the operator.
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]  # Clickjacking protection remains active.
    assert client.get("/jobs/does-not-exist").status_code == 404  # Unknown jobs do not leak details.
