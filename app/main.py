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
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.cases import Registry
from app.config import ROOT, Settings
from app.db import Store
from app.devin import launch_preflight
from app.metrics import metrics
from app.report import build_report

# ELI5: reject unusually large webhook bodies before they consume memory.
MAX_PAYLOAD_BYTES = 256 * 1024
# ELI5: only this human-applied label is allowed to spend a remediation attempt.
TRIGGER_LABEL = "devin-remediate"


# Just the pieces of GitHub's `issues` event we use. `strict` refuses "42" where an int is expected.
class RepositoryPayload(BaseModel):
    """The repository identity needed to bind an event to the configured fork."""
    model_config = ConfigDict(strict=True)
    # ELI5: strict typing stops a text value from pretending to be GitHub's numeric ID.
    id: int
    # ELI5: the readable name is checked beside the numeric ID to prevent lookalike repositories.
    full_name: str


class IssuePayload(BaseModel):
    """The positive issue number used to look up an approved case."""
    model_config = ConfigDict(strict=True)
    # ELI5: GitHub issue zero cannot name a real approved remediation.
    number: int = Field(gt=0)


class LabelPayload(BaseModel):
    """The label name that expresses the event trigger."""
    # ELI5: the admission route compares this value with the one trusted label.
    name: str


class IssueEvent(BaseModel):
    """The small validated GitHub event shape consumed by the admission route."""
    # ELI5: these four fields are all the control plane needs after signature verification.
    action: str
    repository: RepositoryPayload
    issue: IssuePayload
    label: LabelPayload


async def read_signed_body(request: Request, secret: str) -> bytes:
    """Read the raw body (bounded) and verify GitHub's HMAC over those exact bytes.

    The signature is computed over the bytes GitHub sent, so we must verify *before*
    parsing JSON; re-serializing could change whitespace and break the check.
    """
    # ELI5: collect the exact bytes so HMAC covers what GitHub actually sent.
    raw = bytearray()
    async for chunk in request.stream():
        # ELI5: add each bounded network chunk to the body under review.
        raw.extend(chunk)
        if len(raw) > MAX_PAYLOAD_BYTES:
            # ELI5: stop before an oversized request can become a memory problem.
            raise HTTPException(413, "Payload too large")
    # ELI5: calculate the signature using the shared secret and the untouched body.
    expected = "sha256=" + hmac.new(secret.encode(), bytes(raw), hashlib.sha256).hexdigest()
    # ELI5: read the sender's claimed signature for a constant-time comparison.
    provided = request.headers.get("X-Hub-Signature-256", "")
    if not hmac.compare_digest(expected.encode(), provided.encode("utf-8")):  # constant-time compare
        # ELI5: a mismatch means the request is not trusted enough to parse or persist.
        raise HTTPException(401, "Invalid signature")
    # ELI5: return only after authenticity and size checks both pass.
    return bytes(raw)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the webhook, read-only report, and health endpoints for one settings snapshot."""
    # ELI5: each app instance gets one registry and one database, so routes share one source of truth.
    # ELI5: use the caller's configuration, or load the normal environment configuration.
    settings = settings or Settings()
    # ELI5: load approved cases and the database once so every route sees the same snapshot source.
    registry, store = Registry(settings), Store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Release the database engine when the web process shuts down."""
        # ELI5: the app does no work on startup, but it closes its SQLite handles on exit.
        yield
        store.engine.dispose()

    # ELI5: the FastAPI object wires routes while the lifespan closes the shared database later.
    app = FastAPI(title="Devin Remediation Platform", lifespan=lifespan)
    # ELI5: expose the read-only dependencies to tests and small local integrations.
    app.state.store, app.state.settings = store, settings
    # ELI5: render one checked-in template instead of assembling customer HTML in route code.
    templates = Jinja2Templates(directory=str(ROOT / "app/templates"))

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Add browser guardrails to every response without changing its body."""
        # ELI5: these headers tell a browser to avoid MIME guessing, referrer leakage, and framing.
        # ELI5: let the next route create the normal response first.
        response = await call_next(request)
        # ELI5: prevent the browser from guessing a different content type.
        response.headers["X-Content-Type-Options"] = "nosniff"
        # ELI5: keep URLs from this page out of another site's referrer header.
        response.headers["Referrer-Policy"] = "no-referrer"
        # ELI5: allow this page's own content and inline CSS, while blocking framing and foreign scripts.
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        # ELI5: return the protected response unchanged apart from its guardrail headers.
        return response

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        """Confirm that the configured database can be read."""
        # ELI5: a tiny read catches an unavailable database while reporting the selected mode.
        store.jobs()  # ELI5: a small read proves the database is reachable.
        # ELI5: report health plus mode so an operator knows which storage is being inspected.
        return {"status": "ok", "mode": settings.mode, "live_enabled": settings.enable_live}

    @app.get("/metrics")
    def metric_values() -> dict[str, Any]:
        """Return persisted metrics with the execution mode attached."""
        # ELI5: this endpoint is a read-only counter view, so it cannot launch remediation.
        # ELI5: calculate counters from persisted jobs and events without starting work.
        return {"mode": settings.mode, **metrics(store)}

    def render(request: Request, selected: str | None = None) -> Response:
        """Dashboard page; with `selected`, also the event timeline for that job."""
        # ELI5: the HTML page and JSON download use the same normalized evidence snapshot.
        # ELI5: normalize jobs, events, case contracts, and metrics once for this page.
        report = build_report(settings, store, registry)
        # ELI5: reuse the report's job records so the table and selected detail cannot disagree.
        jobs = report["jobs"]
        if selected and not any(job["id"] == selected for job in report["jobs"]):
            # ELI5: a made-up job ID gets a clear not-found response and cannot select another job.
            raise HTTPException(404, "Unknown job")
        # ELI5: select one normalized record, or no record for the portfolio page.
        selected_job = next((job for job in report["jobs"] if job["id"] == selected), None)
        # ELI5: pass both compatibility fields and the new report fields to the server-rendered view.
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            # ELI5: show whether this page came from simulation or live control-plane storage.
            "mode": settings.mode,
            "jobs": jobs,  # Keep the original context available to small local integrations.
            # ELI5: preserve the approved registry for integrations that still inspect case objects.
            "cases": registry.cases,
            # ELI5: use one metric snapshot for every KPI on the page.
            "metrics": report["metrics"],
            # ELI5: selected details come from the same snapshot as the table and JSON export.
            "events": selected_job["events"] if selected_job else [],
            # ELI5: keep the selected ID so links and template state remain stable.
            "selected": selected,
            # ELI5: give the detail panel the normalized case-aware job record.
            "selected_job": selected_job,
            # ELI5: keep the original baseline lookup for small integrations.
            "evidence": {case["id"]: case["baseline"] for case in report["cases"]},
            # ELI5: expose configuration status without exposing credentials.
            "live_enabled": settings.enable_live,
            # ELI5: provide the complete export-shaped report for the footer and future views.
            "report": report,
            # ELI5: case cards use the same case records as the downloadable report.
            "portfolio": report["cases"],
            # ELI5: the evidence table uses the same job rows as the selected detail.
            "job_records": report["jobs"],
            # ELI5: the funnel is made only from persisted workflow handoffs.
            "workflow": report["workflow"],
            # ELI5: readiness explains live gates without pretending simulation passed them.
            "readiness": report["readiness"],
        })

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        """Render the portfolio page from one normalized report snapshot."""
        # ELI5: the homepage is only a view; worker state changes happen elsewhere.
        return render(request)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def details(request: Request, job_id: str) -> Response:
        """Render one job's evidence and timeline using the same report model."""
        # ELI5: selecting a job changes what is shown, not what is executed.
        return render(request, job_id)

    @app.get("/report.json")
    def report_json() -> JSONResponse:
        """Download the same truthful evidence snapshot rendered by the dashboard."""
        # ELI5: this is a read-only audit handoff, so it never starts a worker or calls a provider.
        # ELI5: build the same read-only snapshot used by HTML before setting download headers.
        report = build_report(settings, store, registry)
        # ELI5: name the file with its mode so a downloaded simulation cannot be mistaken for live data.
        filename = f"devin-remediation-evidence-{settings.mode.lower()}.json"
        # ELI5: return JSON as an attachment without adding provider text or executing a job.
        return JSONResponse(
            content=report,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/webhooks/github", status_code=202)
    async def webhook(request: Request) -> dict[str, Any]:
        """Admission gates, in order. Every gate must pass before a job row is written."""
        # ELI5: retrieve the configured secret only inside the admission request.
        secret = settings.github_webhook_secret.get_secret_value()
        if not secret:
            # ELI5: refuse to accept spend-triggering events when authenticity is unconfigured.
            raise HTTPException(503, "Webhook secret not configured")
        raw = await read_signed_body(request, secret)  # 1. authentic + bounded

        event_type = request.headers.get("X-GitHub-Event")  # 2. only `issues` events matter
        if event_type == "ping":
            # ELI5: GitHub's health probe gets a response without entering remediation admission.
            return {"status": "pong"}
        if event_type != "issues":
            # ELI5: unrelated webhook families are acknowledged but never persisted as jobs.
            return {"status": "ignored"}
        try:
            # ELI5: parse only after HMAC verification, so untrusted JSON cannot bypass the signature gate.
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                # ELI5: the event must be a JSON object before Pydantic can validate its fields.
                raise ValueError("Object required")
            if payload.get("action") != "labeled":
                # ELI5: only a newly added label is the spend-triggering action in this app.
                return {"status": "ignored"}
            # ELI5: strict models reject missing, extra-shaped, or incorrectly typed admission fields.
            event = IssueEvent.model_validate(payload)
        except (ValueError, ValidationError):
            # ELI5: malformed issue events get one generic client error without leaking parser details.
            raise HTTPException(400, "Invalid issue event") from None
        if event.label.name != TRIGGER_LABEL:  # 3. the label is the human's explicit "spend money" opt-in
            # ELI5: a different label is harmless and must not create work.
            return {"status": "ignored"}
        if (event.repository.id != settings.github_repository_id
                or event.repository.full_name != settings.github_repository):  # 4. our fork, by ID and name
            # ELI5: reject events from another repository even when their shape is valid.
            raise HTTPException(403, "Repository not allowed")
        case = registry.by_issue.get(event.issue.number)  # 5. issue must be pre-bound to an approved case
        if case is None:
            # ELI5: an issue cannot choose its own case or allowed paths through its text.
            raise HTTPException(422, "Issue is not an approved case")
        delivery = request.headers.get("X-GitHub-Delivery", "")  # 6. delivery ID is our dedupe key
        if not re.fullmatch(r"[A-Za-z0-9-]{1,200}", delivery):
            # ELI5: reject malformed IDs before they enter the duplicate-delivery ledger.
            raise HTTPException(400, "Invalid delivery ID")
        if settings.mode == "LIVE":  # 7. config complete and baseline proof on disk
            if settings.live_errors():
                # ELI5: live work stays disabled until every operator setup gate passes.
                raise HTTPException(503, "Live execution is disabled or incomplete; run make doctor")
            try:
                # ELI5: use the same interpreter and bootstrap checks as the worker before enqueueing.
                launch_preflight(settings, case, event.repository.full_name)
            except (ValueError, OSError):
                # ELI5: missing local validation or context cannot become a queued paid job.
                raise HTTPException(503, "Live evaluator or Devin context is unavailable; run make doctor") from None
            try:
                # ELI5: require current baseline proof before a provider call can be admitted.
                registry.evidence(case)
            except (ValueError, OSError):
                # ELI5: stale or missing proof is a case-specific rejection, not a provider retry.
                raise HTTPException(422, "Case has no current confirmed baseline") from None
        # 8. durable enqueue (deduped); the worker picks it up from here.
        # ELI5: write the job once, keyed by GitHub's delivery ID, then let the worker execute it.
        job, duplicate = await run_in_threadpool(store.enqueue, delivery, event.repository.full_name,
                                                 event.issue.number, case.id)
        # ELI5: return only identifiers and status needed to follow the persisted job.
        return {"job_id": job.id, "status": job.status, "duplicate": duplicate}

    return app
