"""Metrics for the dashboard and `/metrics`, computed from the job table and event log.

ELI5: counts of how many jobs we tried, how many Devin got right the first
time, how many were saved by the single correction, how long things took, and
how many duplicate webhooks we swallowed. Every rate ships with its
denominator so a "1 of 1" is never mistaken for a "100 of 100".
"""

from statistics import median
from typing import Any

from app.db import Store
from app.models import Event, Job, TERMINAL

# ELI5: only these evaluation outcomes belong in the repair-quality denominator.
ASSESSED_OUTCOMES = {"VERIFIED", "NORMAL_FAILED", "REGRESSION_SURVIVED", "APPLICATION_FAILED", "REGRESSION"}

# ELI5: these are the provider actions that the adapter is allowed to expose as trace rows.
DEVIN_API_OPERATIONS = (
    "attachment_upload",
    "session_create",
    "session_poll",
    "list_reconcile",
    "correction_message",
)
# ELI5: only these provider outcomes are safe enough for aggregate reporting.
DEVIN_API_OUTCOMES = {"SUCCEEDED", "FOUND", "NOT_FOUND", "AMBIGUOUS", "FAILED", "RETRYABLE_ERROR"}
# ELI5: FOUND is a successful reconciliation because it recovered the existing session identity.
DEVIN_API_SUCCESS_OUTCOMES = {"SUCCEEDED", "FOUND"}


# ELI5: this helper reads the store once and returns the dashboard's counters.
def metrics(store: Store) -> dict:
    """Return customer-facing counts whose numerator and denominator stay explicit."""
    # ELI5: pass the same jobs and events to one pure calculator for a consistent snapshot.
    return metrics_from_records(store.jobs(), store.events(), mode=store.mode)


# ELI5: this helper calculates every metric from one consistent job/event snapshot.
def _latency_summary(values: list[int]) -> dict[str, int | float | None]:
    """Summarize bounded provider latencies without inventing values for missing rows."""
    # ELI5: an empty latency list stays visibly unrecorded in the report.
    if not values:
        return {"count": 0, "min": None, "max": None, "average": None, "median": None}
    # ELI5: round averages and medians to readable milliseconds while preserving exact min and max.
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "average": round(sum(values) / len(values), 3),
        "median": round(median(values), 3),
    }


def provider_api_metrics(events: list[Event], mode: str | None = None,
                         job_ids: set[str] | None = None) -> dict[str, Any]:
    """Aggregate persisted Devin operation events for one mode and optional job set.

    The provider adapter already writes a redacted event shape. This second boundary
    validates the shape before counting it so old or malformed rows cannot become
    invented API history in a report.
    """
    # ELI5: keep only events owned by the requested job snapshot when one was supplied.
    records: list[tuple[str, str, int | None]] = []
    for event in events:
        if event.event_type != "DEVIN_API_OPERATION":
            continue
        if job_ids is not None and event.job_id not in job_ids:
            continue
        details = event.details if isinstance(event.details, dict) else {}
        operation = details.get("operation_key")
        # ELI5: require the stable operation key and its display name to agree.
        if (not isinstance(operation, str) or operation not in DEVIN_API_OPERATIONS
                or details.get("operation") != operation):
            continue
        event_mode = details.get("mode")
        # ELI5: mode-specific reports never count a row that is labelled for another mode.
        if mode is not None and event_mode != mode:
            continue
        outcome = details.get("outcome")
        if not isinstance(outcome, str) or outcome not in DEVIN_API_OUTCOMES:
            continue
        latency = details.get("latency_ms")
        safe_latency = latency if isinstance(latency, int) and not isinstance(latency, bool) and latency >= 0 else None
        records.append((operation, outcome, safe_latency))

    # ELI5: initialize every supported operation so a zero remains distinct from missing data.
    operation_rows: dict[str, dict[str, Any]] = {}
    for operation in DEVIN_API_OPERATIONS:
        matching = [record for record in records if record[0] == operation]
        latencies = [record[2] for record in matching if record[2] is not None]
        outcomes: dict[str, int] = {}
        for _, outcome, _ in matching:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        successes = sum(outcome in DEVIN_API_SUCCESS_OUTCOMES for _, outcome, _ in matching)
        operation_rows[operation] = {
            "event_count": len(matching),
            "success_count": successes,
            "outcome_counts": outcomes,
            "latency_ms": _latency_summary(latencies),
        }

    # ELI5: expose aggregate counts beside per-operation rows for simple JSON consumers.
    latencies = [record[2] for record in records if record[2] is not None]
    success_count = sum(outcome in DEVIN_API_SUCCESS_OUTCOMES for _, outcome, _ in records)
    event_count = len(records)
    return {
        "mode": mode or "NOT_RECORDED",
        "status": "RECORDED" if event_count else "NOT_RECORDED",
        "event_count": event_count,
        "success_count": success_count,
        "failure_count": event_count - success_count,
        "operation_count": len({operation for operation, _, _ in records}),
        "latency_ms": _latency_summary(latencies),
        # ELI5: simulation timing is instrumentation from the fake adapter, not provider performance.
        "latency_basis": "synthetic" if mode == "SIMULATION" else "local_elapsed" if mode == "LIVE" else "NOT_RECORDED",
        "operations": operation_rows,
    }


# ELI5: keep the report-facing name obvious while sharing the same validated calculator.
provider_api_summary = provider_api_metrics


def metrics_from_records(jobs: list[Job], events: list[Event], mode: str | None = None) -> dict:
    """Calculate metrics from one already-read job/event snapshot for consistent exports."""
    # ELI5: the HTML and JSON report reuse these same rows, so their totals cannot drift mid-render.
    job_ids = {job.id for job in jobs}  # ELI5: event counts must describe only the supplied job snapshot.
    # Jobs whose first candidate got a real verdict: the denominator for "first-pass" success.
    # ELI5: collect jobs whose first candidate received a real repair verdict.
    assessed = {
        event.job_id
        for event in events
        if event.job_id in job_ids
        and event.event_type == "EVALUATED"
        and isinstance(event.details, dict)
        and event.details.get("correction_count") == 0
        and isinstance(event.details.get("outcome"), str)
        and event.details.get("outcome") in ASSESSED_OUTCOMES
    }
    # Jobs where we actually sent the one correction: the denominator for "correction recovery".
    # ELI5: collect jobs where the worker actually sent its one bounded correction.
    corrected = {event.job_id for event in events if event.job_id in job_ids and event.event_type == "CORRECTION_SENT"}
    # Latencies are measured from the webhook (job.created_at), i.e. what a human waited.
    # ELI5: measure webhook-to-PR time only for jobs that persisted a PR timestamp.
    pr_times = [
        (job.pr_created_at - job.created_at).total_seconds()
        for job in jobs
        if job.pr_created_at is not None and job.created_at is not None
    ]
    # ELI5: measure webhook-to-verification time only for terminal verified jobs.
    verified_times = [
        (job.completed_at - job.created_at).total_seconds()
        for job in jobs
        if job.status == "VERIFIED" and job.completed_at is not None and job.created_at is not None
    ]
    # ELI5: count every state, including active and escalated jobs.
    status_counts = {}
    # ELI5: visit each job once to count its durable state.
    for job in jobs:
        # ELI5: increment the bucket for this job's durable state.
        status_counts[job.status] = status_counts.get(job.status, 0) + 1
    # ELI5: count every top-level validation outcome, including NOT_RUN.
    validation_counts = {}
    # ELI5: visit each job once to count its durable validation outcome.
    for job in jobs:
        # ELI5: replace a missing database verdict with an explicit not-run label.
        outcome = job.validation_status or "NOT_RUN"
        # ELI5: increment the bucket for this validation outcome.
        validation_counts[outcome] = validation_counts.get(outcome, 0) + 1
    # ELI5: use the job mode when callers did not provide one, while tolerating small test doubles.
    report_mode = mode or next((getattr(job, "mode", None) for job in jobs if getattr(job, "mode", None)), None)
    # ELI5: return counters with explicit denominators and persisted status distributions.
    return {
        # ELI5: count every admitted job.
        "jobs_total": len(jobs),
        # ELI5: count jobs with a recorded worker start.
        "attempted": sum(job.started_at is not None for job in jobs),
        # ELI5: count jobs that are not terminal yet.
        "active": sum(job.status not in TERMINAL for job in jobs),
        # ELI5: count terminal verified jobs.
        "verified": sum(job.status == "VERIFIED" for job in jobs),
        # ELI5: count failed or escalated jobs for operator attention.
        "unsuccessful": sum(job.status in {"FAILED", "ESCALATED"} for job in jobs),
        # ELI5: count verified jobs that needed no correction.
        "first_pass": sum(job.status == "VERIFIED" and job.correction_count == 0 for job in jobs),
        # ELI5: expose the evaluated first-attempt denominator beside first_pass.
        "first_pass_denominator": len(assessed),
        # ELI5: count verified jobs whose correction event was recorded.
        "correction_recovery": sum(job.status == "VERIFIED" and job.id in corrected for job in jobs),
        # ELI5: expose the correction-sent denominator beside correction_recovery.
        "correction_denominator": len(corrected),
        # ELI5: show the median wait to observe a candidate PR, if any exist.
        "median_event_to_pr_seconds": round(median(pr_times), 3) if pr_times else None,
        # ELI5: show the median wait to independent verification, if any exist.
        "median_event_to_verified_seconds": round(median(verified_times), 3) if verified_times else None,
        # ELI5: count duplicate webhook deliveries suppressed by the ledger.
        "duplicate_deliveries": sum(event.event_type == "DUPLICATE_DELIVERY" for event in events),
        # ELI5: count duplicate execution attempts separately from duplicate webhooks.
        "duplicate_executions": sum(event.event_type == "DUPLICATE_EXECUTION" for event in events),
        # ELI5: expose every durable job state for the portfolio.
        "status_counts": status_counts,
        # ELI5: expose every durable validation result for the oracle panel.
        "validation_counts": validation_counts,
        # ELI5: expose redacted provider activity beside the existing job metrics.
        "provider_api": provider_api_metrics(events, report_mode, job_ids=job_ids),
    }
