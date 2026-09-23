"""Render one durable job as a single GitHub issue status comment.

ELI5: engineers watch remediation progress on the GitHub issue itself instead
of opening the dashboard. This module only builds markdown text; it never
calls the network. The orchestrator decides when to post or edit it.
"""

from app.db import Store
from app.models import Job
from app.report import _validation

# ELI5: one short, plain-English line per job status; unknown statuses fall back safely.
_STATE_MEANING = {
    "QUEUED": "Queued",
    "DEVIN_RUNNING": "Devin working",
    "PR_OPENED": "PR opened",
    "VALIDATING": "Independently validating",
    "CORRECTING": "Devin working (correction)",
    "VERIFIED": "Verified",
    "FAILED": "Needs human attention",
    "ESCALATED": "Needs human attention",
}


def render_card(job: Job) -> str:
    """Build the full markdown body for one job's status comment."""
    # ELI5: reuse the same small trusted validation matrix the dashboard report shows.
    validation = _validation(job)
    meaning = _STATE_MEANING.get(job.status, "Needs human attention")
    lines = [
        "### DevinTrace status",
        f"**Current state:** `{job.status}` — {meaning}",
    ]
    # ELI5: link Devin's session only when the provider actually gave us one.
    if job.devin_session_url:
        lines.append(f"**Devin session:** {job.devin_session_url}")
    # ELI5: show the pull request link and a short, non-sensitive commit prefix.
    if job.candidate_pr_url:
        sha = job.candidate_sha or job.validated_sha
        short_sha = f" @ `{sha[:12]}`" if sha else ""
        lines.append(f"**Pull request:** {job.candidate_pr_url}{short_sha}")
    # ELI5: describe the independent verdict using the same fields the dashboard trusts.
    if validation["normal"] == validation["mutant"] == "NOT_RUN" and validation["application_status"] in {None, "NOT_RUN"}:
        lines.append("**Validator result:** pending")
    elif validation["normal"] == validation["mutant"] == "NOT_RUN":
        lines.append(f"**Validator result:** application contract `{validation['application_status']}`")
    else:
        lines.append(
            f"**Validator result:** original behavior {validation['normal']}, "
            f"injected regression caught: {validation['mutant']}"
        )
    lines.append(f"**Corrections sent:** {job.correction_count}")
    lines.append(f"**Job id:** `{job.id}`")
    lines.append(
        "\n_Human review is the final gate. VERIFIED means the registered contract "
        "passed, not that the change is merged._"
    )
    return "\n".join(lines)


def latest_comment_id(store: Store, job_id: str) -> int | None:
    """Find the comment id saved by the most recent successful status comment, if any."""
    # ELI5: events are stored oldest-first, so scan from the newest to find the live comment.
    for entry in reversed(store.events(job_id)):
        if entry.event_type == "ISSUE_STATUS_COMMENTED":
            comment_id = entry.details.get("comment_id")
            # ELI5: only trust a well-formed comment id saved by this same code path.
            if isinstance(comment_id, int) and not isinstance(comment_id, bool) and comment_id > 0:
                return comment_id
            return None
    return None
