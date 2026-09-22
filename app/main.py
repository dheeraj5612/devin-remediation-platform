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


# ELI5: these models keep only the GitHub fields needed to decide whether work is allowed.
class RepositoryPayload(BaseModel):
    """The repository identity needed to bind an event to the configured fork."""
    # ELI5: strict validation stops text that looks like a number from posing as an ID.
    model_config = ConfigDict(strict=True)
    # ELI5: strict typing stops a text value from pretending to be GitHub's numeric ID.
    id: int
    # ELI5: the readable name is checked beside the numeric ID to prevent lookalike repositories.
    full_name: str


# ELI5: this model keeps a positive issue number for the approved-case lookup.
class IssuePayload(BaseModel):
    """The positive issue number used to look up an approved case."""
    # ELI5: strict validation keeps the issue number in the same shape GitHub sent it.
    model_config = ConfigDict(strict=True)
    # ELI5: GitHub issue zero cannot name a real approved remediation.
    number: int = Field(gt=0)


# ELI5: this model carries the label that tells the app what GitHub action happened.
class LabelPayload(BaseModel):
    """The label name that expresses the event trigger."""
    # ELI5: the admission route compares this value with the one trusted label.
    name: str


# ELI5: this model is the complete small event shape accepted by the webhook.
class IssueEvent(BaseModel):
    """The small validated GitHub event shape consumed by the admission route."""
    # ELI5: these four fields are all the control plane needs after signature verification.
    action: str
    # ELI5: this nested identity must match the configured repository.
    repository: RepositoryPayload
    # ELI5: this nested number must map to an approved case.
    issue: IssuePayload
    # ELI5: this nested label must be the exact remediation trigger.
    label: LabelPayload


# ELI5: this helper authenticates a bounded request before the app trusts its JSON.
async def read_signed_body(request: Request, secret: str) -> bytes:
    """Read the raw body (bounded) and verify GitHub's HMAC over those exact bytes.

    The signature is computed over the bytes GitHub sent, so we must verify *before*
    parsing JSON; re-serializing could change whitespace and break the check.
    """
    # ELI5: collect the exact bytes so HMAC covers what GitHub actually sent.
    raw = bytearray()
    # ELI5: process the body in chunks so the size limit also protects streaming requests.
    async for chunk in request.stream():
        # ELI5: add each bounded network chunk to the body under review.
        raw.extend(chunk)
        # ELI5: stop before an oversized request can become a memory problem.
        if len(raw) > MAX_PAYLOAD_BYTES:
            # ELI5: report the size rejection without reading or parsing more bytes.
            raise HTTPException(413, "Payload too large")
    # ELI5: calculate the signature using the shared secret and the untouched body.
    expected = "sha256=" + hmac.new(secret.encode(), bytes(raw), hashlib.sha256).hexdigest()
    # ELI5: read the sender's claimed signature for a constant-time comparison.
    provided = request.headers.get("X-Hub-Signature-256", "")
    # ELI5: compare signatures in constant time so guesses do not reveal partial matches.
    if not hmac.compare_digest(expected.encode(), provided.encode("utf-8")):
        # ELI5: a mismatch means the request is not trusted enough to parse or persist.
        raise HTTPException(401, "Invalid signature")
    # ELI5: return only after authenticity and size checks both pass.
    return bytes(raw)


# ELI5: this factory wires one configured registry, database, and set of read-only views.
def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the webhook, read-only report, and health endpoints for one settings snapshot."""
    # ELI5: use the caller's configuration, or load the normal environment configuration.
    settings = settings or Settings()
    # ELI5: load approved cases and the database once so every route sees the same snapshot source.
    registry, store = Registry(settings), Store(settings)

    # ELI5: this lifecycle hook keeps the shared database open only for the app lifetime.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Release the database engine when the web process shuts down."""
        # ELI5: yield control while the server is running before cleanup begins.
        yield
        # ELI5: close database connections so the process releases its file handles.
        store.engine.dispose()

    # ELI5: the FastAPI object wires routes while the lifespan closes the shared database later.
    app = FastAPI(title="Devin Remediation Platform", lifespan=lifespan)
    # ELI5: expose the read-only dependencies to tests and small local integrations.
    app.state.store, app.state.settings = store, settings
    # ELI5: render one checked-in template instead of assembling customer HTML in route code.
    templates = Jinja2Templates(directory=str(ROOT / "app/templates"))

    # ELI5: this middleware adds the same browser safety headers to every route.
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Add browser guardrails to every response without changing its body."""
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

    # ELI5: this endpoint lets an operator check storage without starting work.
    @app.get("/healthz")
    def health() -> dict[str, Any]:
        """Confirm that the configured database can be read."""
        # ELI5: a tiny read catches an unavailable database while reporting the selected mode.
        store.jobs()  # ELI5: a small read proves the database is reachable.
        # ELI5: report health plus mode so an operator knows which storage is being inspected.
        return {"status": "ok", "mode": settings.mode, "live_enabled": settings.enable_live}

    # ELI5: this endpoint exposes persisted counters without changing anything.
    @app.get("/metrics")
    def metric_values() -> dict[str, Any]:
        """Return persisted metrics with the execution mode attached."""
        # ELI5: calculate counters from persisted jobs and events without starting work.
        return {"mode": settings.mode, **metrics(store)}

    # ELI5: this helper renders the shared snapshot for either the portfolio or one job.
    def render(request: Request, selected: str | None = None) -> Response:
        """Dashboard page; with `selected`, also the event timeline for that job."""
        # ELI5: normalize jobs, events, case contracts, and metrics once for this page.
        report = build_report(settings, store, registry)
        # ELI5: reuse the report's job records so the table and selected detail cannot disagree.
        jobs = report["jobs"]
        # ELI5: reject a requested job before trying to render details for it.
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

    # ELI5: this route renders the portfolio view for the browser.
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        """Render the portfolio page from one normalized report snapshot."""
        # ELI5: the homepage is only a view; worker state changes happen elsewhere.
        return render(request)

    # ELI5: this route renders one persisted job's details.
    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def details(request: Request, job_id: str) -> Response:
        """Render one job's evidence and timeline using the same report model."""
        # ELI5: selecting a job changes what is shown, not what is executed.
        return render(request, job_id)

    # ELI5: this route downloads the same snapshot shown in the dashboard.
    @app.get("/report.json")
    def report_json() -> JSONResponse:
        """Download the same truthful evidence snapshot rendered by the dashboard."""
        # ELI5: build the same read-only snapshot used by HTML before setting download headers.
        report = build_report(settings, store, registry)
        # ELI5: name the file with its mode so a downloaded simulation cannot be mistaken for live data.
        filename = f"devin-remediation-evidence-{settings.mode.lower()}.json"
        # ELI5: return JSON as an attachment without adding provider text or executing a job.
        return JSONResponse(
            content=report,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ELI5: this route admits only authenticated, allow-listed GitHub issue labels.
    @app.post("/webhooks/github", status_code=202)
    async def webhook(request: Request) -> dict[str, Any]:
        """Admission gates, in order. Every gate must pass before a job row is written."""
        # ELI5: retrieve the configured secret only inside the admission request.
        secret = settings.github_webhook_secret.get_secret_value()
        # ELI5: refuse spend-triggering requests until the shared secret is configured.
        if not secret:
            # ELI5: refuse to accept spend-triggering events when authenticity is unconfigured.
            raise HTTPException(503, "Webhook secret not configured")
        # ELI5: verify the raw body before any JSON parsing or job creation.
        raw = await read_signed_body(request, secret)

        # ELI5: only issue events can request a remediation job.
        event_type = request.headers.get("X-GitHub-Event")
        # ELI5: answer GitHub's connectivity probe without entering the admission flow.
        if event_type == "ping":
            # ELI5: GitHub's health probe gets a response without entering remediation admission.
            return {"status": "pong"}
        # ELI5: acknowledge unrelated event families without persisting work.
        if event_type != "issues":
            # ELI5: unrelated webhook families are acknowledged but never persisted as jobs.
            return {"status": "ignored"}
        # ELI5: parse and validate only after the signature gate has passed.
        try:
            # ELI5: parse only after HMAC verification, so untrusted JSON cannot bypass the signature gate.
            payload = json.loads(raw)
            # ELI5: require a JSON object before checking the event's named fields.
            if not isinstance(payload, dict):
                # ELI5: the event must be a JSON object before Pydantic can validate its fields.
                raise ValueError("Object required")
            # ELI5: only adding the trigger label can start the approved workflow.
            if payload.get("action") != "labeled":
                # ELI5: only a newly added label is the spend-triggering action in this app.
                return {"status": "ignored"}
            # ELI5: strict models reject missing, extra-shaped, or incorrectly typed admission fields.
            event = IssueEvent.model_validate(payload)
        except (ValueError, ValidationError):
            # ELI5: malformed issue events get one generic client error without leaking parser details.
            raise HTTPException(400, "Invalid issue event") from None
        # ELI5: require the exact human-applied label that opts into spending an attempt.
        if event.label.name != TRIGGER_LABEL:
            # ELI5: a different label is harmless and must not create work.
            return {"status": "ignored"}
        # ELI5: match both repository identity fields so another repository cannot look like our fork.
        if (event.repository.id != settings.github_repository_id
                or event.repository.full_name != settings.github_repository):
            # ELI5: reject events from another repository even when their shape is valid.
            raise HTTPException(403, "Repository not allowed")
        # ELI5: look up the issue in the allow-listed registry instead of trusting issue text.
        case = registry.by_issue.get(event.issue.number)
        # ELI5: an unknown issue cannot select a case or allowed path by itself.
        if case is None:
            # ELI5: an issue cannot choose its own case or allowed paths through its text.
            raise HTTPException(422, "Issue is not an approved case")
        # ELI5: use GitHub's delivery ID as the durable duplicate-delivery key.
        delivery = request.headers.get("X-GitHub-Delivery", "")
        # ELI5: accept only a short, simple delivery key for the duplicate ledger.
        if not re.fullmatch(r"[A-Za-z0-9-]{1,200}", delivery):
            # ELI5: reject malformed IDs before they enter the duplicate-delivery ledger.
            raise HTTPException(400, "Invalid delivery ID")
        # ELI5: live mode runs extra setup and baseline gates before anything is queued.
        if settings.mode == "LIVE":
            # ELI5: all live configuration gates must pass before paid work is admitted.
            if settings.live_errors():
                # ELI5: live work stays disabled until every operator setup gate passes.
                raise HTTPException(503, "Live execution is disabled or incomplete; run make doctor")
            # ELI5: check local validation context before a live provider session is allowed.
            try:
                # ELI5: use the same interpreter and bootstrap checks as the worker before enqueueing.
                launch_preflight(settings, case, event.repository.full_name)
            except (ValueError, OSError):
                # ELI5: missing local validation or context cannot become a queued paid job.
                raise HTTPException(503, "Live evaluator or Devin context is unavailable; run make doctor") from None
            # ELI5: check current baseline evidence before a live provider session is allowed.
            try:
                # ELI5: require current baseline proof before a provider call can be admitted.
                registry.evidence(case)
            except (ValueError, OSError):
                # ELI5: stale or missing proof is a case-specific rejection, not a provider retry.
                raise HTTPException(422, "Case has no current confirmed baseline") from None
        # ELI5: write the job once, keyed by GitHub's delivery ID, then let the worker execute it.
        job, duplicate = await run_in_threadpool(store.enqueue, delivery, event.repository.full_name,
                                                 event.issue.number, case.id)
        # ELI5: return only the identifiers needed to follow this durable job.
        return {"job_id": job.id, "status": job.status, "duplicate": duplicate}

    # ELI5: hand the fully wired application back to the process that will serve it.
    return app
