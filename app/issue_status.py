"""Render one durable job as a single GitHub issue (or PR) status comment.

ELI5: engineers watch remediation progress on the GitHub issue itself instead
of opening the dashboard. This module only builds markdown text; it never
calls the network. The orchestrator decides when to post or edit it. Every
line is paraphrased from persisted Job columns and store events only; missing
data is omitted or spelled out as "not recorded", never invented.
"""

from app.cases import Case, Registry
from app.db import Store
from app.metrics import provider_api_metrics
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

# ELI5: turn the trusted case kind into the two-word story label the card shows.
_KIND_LABEL = {"application": "application contract", "test_quality": "test repair"}


def marker(job_id: str) -> str:
    """Return the hidden HTML marker used to find and edit this same card later."""
    # ELI5: a plain HTML comment is invisible on GitHub but greppable in the raw comment body.
    return f"<!-- devintrace-status job={job_id} -->"


def _case_line(job: Job, registry: Registry | None) -> list[str]:
    """Describe the pinned case and, when the registry is available, its allowed files."""
    # ELI5: without a registry we still know the case id, just not its kind or scope.
    case = registry.cases.get(job.case_id) if registry else None
    if case is None:
        return [f"**Case:** `{job.case_id}`"]
    kind = _KIND_LABEL.get(case.kind, case.kind)
    allowed = ", ".join(f"`{path}`" for path in case.allowed_paths)
    return [f"**Case:** `{job.case_id}` ({kind}); allowed files: {allowed}"]


def _duration_label(seconds: float) -> str:
    """Format elapsed seconds as a short "4m 22s" style label."""
    # ELI5: round once so a fraction of a second never shows up in the story.
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _label_to_verified(store: Store, job_id: str) -> str | None:
    """Return the QUEUED -> VERIFIED duration label, or None when either event is missing."""
    # ELI5: scan the durable diary for the two milestones instead of trusting any cached figure.
    queued_at = verified_at = None
    for entry in store.events(job_id):
        if entry.event_type == "QUEUED" and queued_at is None:
            queued_at = entry.timestamp
        elif entry.event_type == "VERIFIED" and verified_at is None:
            verified_at = entry.timestamp
    if queued_at is None or verified_at is None:
        return None
    return _duration_label((verified_at - queued_at).total_seconds())


def _how_it_ran(job: Job, store: Store) -> list[str]:
    """Paraphrase the recorded webhook admission and Devin API usage as short bullets."""
    events = store.events(job.id)
    api = provider_api_metrics(events, mode=job.mode)
    ops = api["operations"]

    def count(operation: str) -> int:
        return ops.get(operation, {}).get("event_count", 0)

    uploads = count("attachment_upload")
    sessions_created = count("session_create")
    polls = count("session_poll")
    recoveries = count("list_reconcile")
    corrections = min(job.correction_count, 1)

    sessions_line = f"Sessions ({sessions_created} created, {polls} polls, corrections {corrections} of 1)"
    if recoveries:
        sessions_line += ", list-by-tag recovery used"

    return [
        "**How it ran:**",
        "- Labeled `devin-remediate`; signed webhook saved one durable job",
        "- Devin APIs used:",
        "  - Playbooks (how Devin works; set once per repo): configured for this repo",
        "  - Knowledge (case rules; set once per repo): configured for this repo",
        f"  - Attachments ({uploads} upload{'s' if uploads != 1 else ''}): baseline proof",
        f"  - {sessions_line}",
    ]


def _independent_proof(job: Job, case: Case | None, registry: Registry | None) -> list[str]:
    """Paraphrase the validator's verdict without repeating raw evaluator vocabulary."""
    validation = _validation(job)
    lines: list[str] = []
    if case is not None and case.kind == "application":
        baseline_status = None
        if registry is not None:
            try:
                baseline_status = registry.evidence(case).get("application")
            except Exception:  # ELI5: missing or stale baseline proof just means it is not recorded here.
                baseline_status = None
        candidate_status = validation["application_status"]
        if baseline_status and candidate_status:
            lines.append(f"- Baseline `{baseline_status}` -> candidate `{candidate_status}`")
        elif candidate_status:
            lines.append(f"- Candidate result: `{candidate_status}`")
    else:
        if validation["mutant"] == "ASSERTION_FAILED":
            lines.append("- Injected regression caught: yes")
        elif validation["mutant"] == "PASS":
            lines.append("- Injected regression caught: no")
    if validation["evidence_recorded"]:
        lines.append("- Oracle ran on the PR's exact commit")
    if job.candidate_sha and job.validated_sha and job.candidate_sha == job.validated_sha:
        lines.append("- Candidate SHA matches validated SHA")
    if not lines:
        return []
    return ["**Independent proof:**", *lines]


def render_card(job: Job, store: Store, registry: Registry | None = None,
                header: str = "### DevinTrace status") -> str:
    """Build the full markdown body for one job's status comment or PR card."""
    meaning = _STATE_MEANING.get(job.status, "Needs human attention")
    case = registry.cases.get(job.case_id) if registry else None
    lines = [
        marker(job.id),
        header,
        f"**Current state:** `{job.status}` — {meaning}",
        *_case_line(job, registry),
    ]
    # ELI5: link Devin's session only when the provider actually gave us one.
    if job.devin_session_url:
        lines.append(f"**Devin session:** {job.devin_session_url}")
    # ELI5: show the pull request link and a short, non-sensitive commit prefix.
    if job.candidate_pr_url:
        sha = job.candidate_sha or job.validated_sha
        short_sha = f" @ `{sha[:12]}`" if sha else ""
        lines.append(f"**Pull request:** {job.candidate_pr_url}{short_sha}")
    lines.extend(_how_it_ran(job, store))
    lines.extend(_independent_proof(job, case, registry))
    label = _label_to_verified(store, job.id)
    if label:
        lines.append(f"**Label to verified:** {label}")
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
