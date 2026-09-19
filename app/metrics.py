"""Metrics for the dashboard and `/metrics`, computed from the job table and event log.

ELI5: counts of how many jobs we tried, how many Devin got right the first
time, how many were saved by the single correction, how long things took, and
how many duplicate webhooks we swallowed. Every rate ships with its
denominator so a "1 of 1" is never mistaken for a "100 of 100".
"""

from statistics import median

from app.db import Store
from app.models import TERMINAL

# First-attempt outcomes that say something about the *repair* (not about our infrastructure).
ASSESSED_OUTCOMES = {"VERIFIED", "NORMAL_FAILED", "REGRESSION_SURVIVED"}


def metrics(store: Store) -> dict:
    jobs, events = store.jobs(), store.events()
    # Jobs whose first candidate got a real verdict: the denominator for "first-pass" success.
    assessed = {event.job_id for event in events if event.event_type == "EVALUATED"
                and event.details.get("correction_count") == 0
                and event.details.get("outcome") in ASSESSED_OUTCOMES}
    # Jobs where we actually sent the one correction: the denominator for "correction recovery".
    corrected = {event.job_id for event in events if event.event_type == "CORRECTION_SENT"}
    # Latencies are measured from the webhook (job.created_at), i.e. what a human waited.
    pr_times = [(job.pr_created_at - job.created_at).total_seconds() for job in jobs if job.pr_created_at]
    verified_times = [(job.completed_at - job.created_at).total_seconds() for job in jobs if job.status == "VERIFIED"]
    return {
        "attempted": sum(job.started_at is not None for job in jobs),
        "active": sum(job.status not in TERMINAL for job in jobs),
        "verified": sum(job.status == "VERIFIED" for job in jobs),
        "unsuccessful": sum(job.status in {"FAILED", "ESCALATED"} for job in jobs),
        "first_pass": sum(job.status == "VERIFIED" and job.correction_count == 0 for job in jobs),
        "first_pass_denominator": len(assessed),
        "correction_recovery": sum(job.status == "VERIFIED" and job.id in corrected for job in jobs),
        "correction_denominator": len(corrected),
        "median_event_to_pr_seconds": round(median(pr_times), 3) if pr_times else None,
        "median_event_to_verified_seconds": round(median(verified_times), 3) if verified_times else None,
        "duplicate_deliveries": sum(event.event_type == "DUPLICATE_DELIVERY" for event in events),
        "duplicate_executions": sum(event.event_type == "DUPLICATE_EXECUTION" for event in events),
    }
