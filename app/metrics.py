"""Metrics for the dashboard and `/metrics`, computed from the job table and event log.

ELI5: counts of how many jobs we tried, how many Devin got right the first
time, how many were saved by the single correction, how long things took, and
how many duplicate webhooks we swallowed. Every rate ships with its
denominator so a "1 of 1" is never mistaken for a "100 of 100".
"""

from statistics import median

from app.db import Store
from app.models import Event, Job, TERMINAL

# ELI5: only these evaluation outcomes belong in the repair-quality denominator.
ASSESSED_OUTCOMES = {"VERIFIED", "NORMAL_FAILED", "REGRESSION_SURVIVED", "APPLICATION_FAILED", "REGRESSION"}


# ELI5: this helper reads the store once and returns the dashboard's counters.
def metrics(store: Store) -> dict:
    """Return customer-facing counts whose numerator and denominator stay explicit."""
    # ELI5: pass the same jobs and events to one pure calculator for a consistent snapshot.
    return metrics_from_records(store.jobs(), store.events())


# ELI5: this helper calculates every metric from one consistent job/event snapshot.
def metrics_from_records(jobs: list[Job], events: list[Event]) -> dict:
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
    }
