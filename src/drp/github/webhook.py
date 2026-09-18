"""Stage 5/6: GitHub webhook receiver gated on the ``devin-remediate`` label.

Security / idempotency properties:

* HMAC-SHA256 signature (``X-Hub-Signature-256``) is verified with a constant-time compare
  before the payload is parsed; unsigned requests are rejected when a secret is configured.
* Every delivery id (``X-GitHub-Delivery``) is persisted once; redeliveries are acknowledged
  but never create a second job.
* Only ``issues`` events with ``action == "labeled"`` for the trigger label, on the configured
  repository, for an issue that carries a platform finding marker, create a job - and only if
  no job is already active for that finding.
* The handler only writes a row and returns; all slow work happens in the worker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from drp.config import Settings, get_settings
from drp.db import session_scope
from drp.github.client import parse_finding_marker
from drp.jobs import create_job
from drp.models import BaselineStatus, Finding, WebhookDelivery

router = APIRouter()


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def handle_issue_event(
    session: Session, settings: Settings, payload: dict[str, Any], delivery_id: str
) -> tuple[str, str | None]:
    """Pure decision function; returns (outcome, job_id)."""
    action = payload.get("action")
    if action != "labeled":
        return f"ignored: action={action}", None
    label = (payload.get("label") or {}).get("name")
    if label != settings.trigger_label:
        return f"ignored: label={label}", None
    repo = (payload.get("repository") or {}).get("full_name")
    if repo != settings.target_repo:
        return f"ignored: repository={repo}", None
    issue = payload.get("issue") or {}
    number = issue.get("number")
    if not isinstance(number, int):
        return "ignored: no issue number", None
    if issue.get("state") == "closed":
        return "ignored: issue closed", None
    finding_id = parse_finding_marker(issue.get("body"))
    finding = session.get(Finding, finding_id) if finding_id else None
    if finding is None:
        return f"ignored: issue #{number} carries no known finding marker", None
    if finding.baseline_status != BaselineStatus.CONFIRMED:
        return f"ignored: finding {finding.id} baseline is {finding.baseline_status.value}", None
    if finding.github_issue_number is None:
        finding.github_issue_number = number
        finding.github_issue_url = issue.get("html_url")
    sender = (payload.get("sender") or {}).get("login")
    job, created = create_job(
        session, finding, issue_number=number, delivery_id=delivery_id, triggered_by=sender
    )
    return ("job created" if created else "duplicate: job already active"), job.id


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")
    if not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    with session_scope() as session:
        seen = session.get(WebhookDelivery, x_github_delivery)
        if seen is not None:
            return {"ok": True, "outcome": "duplicate delivery", "job_id": seen.job_id}
        if x_github_event == "ping":
            outcome, job_id = "pong", None
        elif x_github_event == "issues":
            outcome, job_id = handle_issue_event(session, settings, payload, x_github_delivery)
        else:
            outcome, job_id = f"ignored: event={x_github_event}", None
        session.add(
            WebhookDelivery(
                delivery_id=x_github_delivery,
                event=x_github_event,
                action=payload.get("action"),
                outcome=outcome,
                job_id=job_id,
            )
        )
    return {"ok": True, "outcome": outcome, "job_id": job_id}
