import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.cases import baseline_evidence, load_cases
from app.config import Settings
from app.db import Store


class RepositoryPayload(BaseModel):
    id: int
    full_name: str


class IssuePayload(BaseModel):
    number: int = Field(gt=0)


class LabelPayload(BaseModel):
    name: str


class LabelEvent(BaseModel):
    action: str
    repository: RepositoryPayload
    issue: IssuePayload
    label: LabelPayload


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    store = store or Store(settings.database_path)
    cases = load_cases()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        store.engine.dispose()

    app = FastAPI(title="Devin Remediation Platform", lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.filters["timestamp"] = lambda value: datetime.fromtimestamp(value, UTC).strftime("%H:%M:%S UTC") if value else "Not available"

    @app.get("/health")
    def health():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "mode": settings.mode, "live_enabled": settings.run_live}

    @app.post("/webhooks/github", status_code=202)
    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "Payload too large")
        expected = "sha256=" + hmac.new(settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
        signature = request.headers.get("x-hub-signature-256", "")
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "Invalid webhook signature")
        event = request.headers.get("x-github-event")
        if event == "ping":
            return {"ignored": "ping"}
        if event != "issues":
            return {"ignored": "event"}
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Not an object")
            if payload.get("action") != "labeled":
                return {"ignored": "action"}
            parsed = LabelEvent.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise HTTPException(400, "Malformed issues event") from error
        if parsed.label.name != "devin-remediate":
            return {"ignored": "label"}
        if parsed.repository.id != settings.github_repository_id or parsed.repository.full_name != settings.github_repository:
            raise HTTPException(403, "Repository is not allowed")
        delivery_id = request.headers.get("x-github-delivery", "")
        if not delivery_id or len(delivery_id) > 128:
            raise HTTPException(400, "Missing or invalid delivery ID")
        case = next((cases[key] for key, issue in settings.case_issues.items() if issue == parsed.issue.number and key in cases), None)
        if case is None:
            raise HTTPException(422, "Issue is not mapped to an approved case")
        if settings.mode == "LIVE":
            try:
                settings.require_live()
                baseline_evidence(case, settings.data_dir)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        job_id, duplicate = await run_in_threadpool(
            store.enqueue, delivery_id, settings.mode, settings.github_repository, parsed.issue.number, case.id,
        )
        return {"job_id": job_id, "duplicate": duplicate}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "mode": settings.mode, "metrics": store.metrics(settings.mode), "jobs": store.jobs(settings.mode), "cases": cases,
        })

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def detail(request: Request, job_id: str):
        try:
            job = store.get(job_id)
        except KeyError as error:
            raise HTTPException(404, "Job not found") from error
        if job.mode != settings.mode:
            raise HTTPException(404, "Job not found")
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "mode": settings.mode, "metrics": store.metrics(settings.mode), "jobs": [job], "cases": cases,
            "timeline": store.events(job_id),
        })

    return app
