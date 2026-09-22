"""Server-rendered, read-only product routes with progressively enhanced controls."""

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cases import Registry
from app.config import ROOT, Settings
from app.db import Store
from app.presentation import human_time, job_title, linked_verdict, recorded_evidence, workbench
from app.report import build_report


def install_views(app: FastAPI, settings: Settings, store: Store, registry: Registry) -> None:
    """Install presentation routes without exposing any execution or write action."""
    app.mount("/static", StaticFiles(directory=str(ROOT / "app/static")), name="static")
    templates = Jinja2Templates(directory=str(ROOT / "app/templates"))
    templates.env.filters.update(date=human_time, clock=lambda value: human_time(value, short=True),
                                 job_title=job_title, linked_verdict=linked_verdict)

    def page(request: Request, template: str, title: str, page_name: str,
             context: dict[str, Any] | None = None, status: int = 200) -> Response:
        """Keep common metadata and response policy consistent across HTML views."""
        return templates.TemplateResponse(
            request=request, name=template, status_code=status,
            context={"title": title, "page_name": page_name, "mode": settings.mode, **(context or {})},
            headers={"Cache-Control": "no-store"},
        )

    def report_context(request: Request) -> dict[str, Any]:
        """Build once per request, so the ledger and global totals share a snapshot."""
        # ELI5: normalize jobs, events, contracts and metrics once so the page cannot disagree with itself.
        report = build_report(settings, store, registry)
        query = request.query_params
        try:
            page_number = int(query.get("page", "1"))
        except (TypeError, ValueError):
            # ELI5: malformed query text should show the first page instead of breaking the read-only view.
            page_number = 1
        return {
            "report": report, "metrics": report["metrics"], "workflow": report["workflow"],
            "portfolio": report["cases"], "readiness": report["readiness"],
            "desk": workbench(report, query.get("q", ""), query.get("view", "all"),
                              query.get("kind", "all"), query.get("sort", "attention"), page_number),
        }

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> Response:
        """Explain the workflow using explicitly archived, checked-in evidence."""
        return page(request, "landing.html", "Independent proof for autonomous repair", "overview",
                    {"archive": recorded_evidence()})

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        """Filter and inspect persisted runs; never enqueue or contact a provider."""
        return page(request, "dashboard.html", "Evidence workbench", "dashboard", report_context(request))

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def details(request: Request, job_id: str) -> Response:
        """Put one run's verdict and comparison before its full event trail."""
        # ELI5: selected details use the same export-shaped snapshot as the ledger and totals.
        context = report_context(request)
        job = next((job for job in context["report"]["jobs"] if job["id"] == job_id), None)
        if not job:
            # ELI5: an unknown ID cannot select or borrow evidence from another recorded job.
            raise HTTPException(404, "Unknown job")
        case = next((case for case in context["portfolio"] if case["id"] == job["case_id"]), None)
        # Old records can outlive a registry entry. Never borrow another case's proof.
        if case is None:
            case = {"baseline": {"statement": "The original contract is no longer registered."},
                    "contract": "Original contract unavailable. Inspect the exported run before review.",
                    "acceptance": "Not available", "allowed_paths": [], "acceptance_test": None}
        context.update(selected_job=job, selected_case=case,
                       milestone_types={"QUEUED", "SESSION_ATTACHED", "PR_OPENED", "EVALUATED",
                                        "CORRECTION_SENT", "VERIFIED", "ESCALATED", "FAILED"})
        return page(request, "job.html", f"Run #{job['issue_number']} · {job_title(job)}", "job", context)

    @app.get("/cases", response_class=HTMLResponse)
    def contracts(request: Request) -> Response:
        """Expose approved contracts and live-readiness gates without changing them."""
        return page(request, "cases.html", "Approved contracts", "cases", report_context(request))

    @app.get("/evidence", response_class=HTMLResponse)
    def archive(request: Request) -> Response:
        """Keep the dated live reference separate from configured workspace storage."""
        return page(request, "evidence.html", "Recorded application proof", "evidence",
                    {"archive": recorded_evidence()})

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> Response:
        """Make operational records non-indexable; this is not an access control."""
        return Response("User-agent: *\nAllow: /$\nDisallow: /dashboard\nDisallow: /jobs/\n"
                        "Disallow: /cases\nDisallow: /report.json\nDisallow: /metrics\n",
                        media_type="text/plain")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        """Offer browser recovery without changing API/webhook error contracts."""
        if ("text/html" not in request.headers.get("accept", "")
                or request.method != "GET"
                or request.url.path.startswith(("/webhooks/", "/metrics", "/healthz", "/report.json", "/static/"))):
            return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
        return page(request, "error.html", "Evidence unavailable", "error", {
            "error_code": error.status_code,
            "error_title": "That evidence isn't here." if error.status_code == 404 else "This view is unavailable.",
            "error_message": "The link may be outdated or belong to another storage mode. Return to the workbench to find a recorded run.",
        }, status=error.status_code)

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request: Request, _error: SQLAlchemyError) -> Response:
        """A failed read is not an empty portfolio; never reveal database internals."""
        if ("text/html" not in request.headers.get("accept", "")
                or request.method != "GET"
                or request.url.path.startswith(("/webhooks/", "/metrics", "/healthz", "/report.json", "/static/"))):
            return JSONResponse({"detail": "Evidence storage unavailable"}, status_code=503)
        return page(request, "error.html", "Storage unavailable", "error", {
            "error_code": 503, "error_title": "The evidence store is unavailable.",
            "error_message": "No empty or successful result is inferred. Check the web process and storage, then retry the workbench.",
        }, status=503)
