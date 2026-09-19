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
        store.jobs()
        return {"status": "ok", "mode": settings.mode, "live_enabled": settings.enable_live}

    @app.get("/metrics")
    def metric_values() -> dict[str, Any]:
        return {"mode": settings.mode, **metrics(store)}

    def render(request: Request, selected: str | None = None) -> Response:
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
        secret = settings.github_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(503, "Webhook secret not configured")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise HTTPException(413, "Payload too large")
        signature = "sha256=" + hmac.new(secret.encode(), bytes(raw), hashlib.sha256).hexdigest()
        provided = request.headers.get("X-Hub-Signature-256", "").encode("utf-8")
        if not hmac.compare_digest(signature.encode(), provided):
            raise HTTPException(401, "Invalid signature")
        event_type = request.headers.get("X-GitHub-Event")
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
        if event.label.name != "devin-remediate":
            return {"status": "ignored"}
        if (event.repository.id != settings.github_repository_id
                or event.repository.full_name != settings.github_repository):
            raise HTTPException(403, "Repository not allowed")
        case = registry.by_issue.get(event.issue.number)
        if case is None:
            raise HTTPException(422, "Issue is not an approved case")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,200}", delivery):
            raise HTTPException(400, "Invalid delivery ID")
        if settings.mode == "LIVE":
            if settings.live_errors():
                raise HTTPException(503, "Live execution is disabled or incomplete; run make doctor")
            try:
                registry.evidence(case)
            except (ValueError, OSError):
                raise HTTPException(422, "Case has no current confirmed baseline") from None
        job, duplicate = await run_in_threadpool(store.enqueue, delivery, event.repository.full_name,
                                                 event.issue.number, case.id)
        return {"job_id": job.id, "status": job.status, "duplicate": duplicate}

    return app
