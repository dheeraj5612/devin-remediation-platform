"""Web process: the GitHub webhook receiver plus the read-only dashboard/metrics pages.

ELI5: GitHub knocks on `/webhooks/github` when someone adds the `devin-remediate`
label to an issue. We check the knock is really from GitHub (HMAC signature),
that it is about *our* fork and an *approved* issue, and that we already hold
baseline proof for that case. Only then do we write a job row. Nothing here
talks to Devin; the worker process does that.
"""

import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.cases import Registry
from app.config import ROOT, Settings
from app.db import Store
from app.metrics import metrics

MAX_PAYLOAD_BYTES = 256 * 1024
TRIGGER_LABEL = "devin-remediate"


# Just the pieces of GitHub's `issues` event we use. `strict` refuses "42" where an int is expected.
class RepositoryPayload(BaseModel):
    model_config = ConfigDict(strict=True)
    id: int
    full_name: str


class IssuePayload(BaseModel):
    model_config = ConfigDict(strict=True)
    number: int = Field(gt=0)


class LabelPayload(BaseModel):
    name: str


class IssueEvent(BaseModel):
    action: str
    repository: RepositoryPayload
    issue: IssuePayload
    label: LabelPayload


async def read_signed_body(request: Request, secret: str) -> bytes:
    """Read the raw body (bounded) and verify GitHub's HMAC over those exact bytes.

    The signature is computed over the bytes GitHub sent, so we must verify *before*
    parsing JSON; re-serializing could change whitespace and break the check.
    """
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise HTTPException(413, "Payload too large")
    expected = "sha256=" + hmac.new(secret.encode(), bytes(raw), hashlib.sha256).hexdigest()
    provided = request.headers.get("X-Hub-Signature-256", "")
    if not hmac.compare_digest(expected.encode(), provided.encode("utf-8")):  # constant-time compare
        raise HTTPException(401, "Invalid signature")
    return bytes(raw)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    registry, store = Registry(settings), Store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        store.engine.dispose()

    app = FastAPI(title="Devin Remediation Platform", lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    templates = Jinja2Templates(directory=str(ROOT / "app/templates"))

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        return response

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        store.jobs()  # proves the database is reachable
        return {"status": "ok", "mode": settings.mode, "live_enabled": settings.enable_live}

    @app.get("/metrics")
    def metric_values() -> dict[str, Any]:
        return {"mode": settings.mode, **metrics(store)}

    def render(request: Request, selected: str | None = None) -> Response:
        """Dashboard page; with `selected`, also the event timeline for that job."""
        jobs = store.jobs()
        if selected and not any(job.id == selected for job in jobs):
            raise HTTPException(404, "Unknown job")
        evidence = {}
        for case in registry.cases.values():
            try:
                evidence[case.id] = registry.evidence(case) if settings.mode == "LIVE" else {
                    "outcome": "SIMULATED", "normal": "PASS", "mutant": "PASS"}
            except (ValueError, OSError):
                evidence[case.id] = {"outcome": "UNCONFIRMED", "normal": "NOT_RUN", "mutant": "NOT_RUN"}
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "mode": settings.mode, "jobs": jobs, "cases": registry.cases, "metrics": metrics(store),
            "events": store.events(selected) if selected else [], "selected": selected,
            "evidence": evidence, "live_enabled": settings.enable_live,
        })

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        return render(request)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def details(request: Request, job_id: str) -> Response:
        return render(request, job_id)

    @app.post("/webhooks/github", status_code=202)
    async def webhook(request: Request) -> dict[str, Any]:
        """Admission gates, in order. Every gate must pass before a job row is written."""
        secret = settings.github_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(503, "Webhook secret not configured")
        raw = await read_signed_body(request, secret)  # 1. authentic + bounded

        event_type = request.headers.get("X-GitHub-Event")  # 2. only `issues` events matter
        if event_type == "ping":
            return {"status": "pong"}
        if event_type != "issues":
            return {"status": "ignored"}
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Object required")
            if payload.get("action") != "labeled":
                return {"status": "ignored"}
            event = IssueEvent.model_validate(payload)
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid issue event") from None
        if event.label.name != TRIGGER_LABEL:  # 3. the label is the human's explicit "spend money" opt-in
            return {"status": "ignored"}
        if (event.repository.id != settings.github_repository_id
                or event.repository.full_name != settings.github_repository):  # 4. our fork, by ID and name
            raise HTTPException(403, "Repository not allowed")
        case = registry.by_issue.get(event.issue.number)  # 5. issue must be pre-bound to an approved case
        if case is None:
            raise HTTPException(422, "Issue is not an approved case")
        delivery = request.headers.get("X-GitHub-Delivery", "")  # 6. delivery ID is our dedupe key
        if not re.fullmatch(r"[A-Za-z0-9-]{1,200}", delivery):
            raise HTTPException(400, "Invalid delivery ID")
        if settings.mode == "LIVE":  # 7. config complete and baseline proof on disk
            if settings.live_errors():
                raise HTTPException(503, "Live execution is disabled or incomplete; run make doctor")
            try:
                registry.evidence(case)
            except (ValueError, OSError):
                raise HTTPException(422, "Case has no current confirmed baseline") from None
        # 8. durable enqueue (deduped); the worker picks it up from here.
        job, duplicate = await run_in_threadpool(store.enqueue, delivery, event.repository.full_name,
                                                 event.issue.number, case.id)
        return {"job_id": job.id, "status": job.status, "duplicate": duplicate}

    return app
