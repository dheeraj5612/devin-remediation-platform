"""Read-only view models. Filtering never changes the canonical evidence report."""

import json
import re
from pathlib import Path
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

ARCHIVE_PATH = Path(__file__).with_name("archive") / "live-application.json"

# These are presentation groups, not new persisted workflow states.
TERMINAL_ATTENTION = {"ESCALATED", "FAILED", "INCOMPLETE"}
VIEWS = ("all", "attention", "verified", "active")
KINDS = ("all", "application", "test_quality")
SHORT_TITLES = {
    "import-unparseable-yaml": "Malformed YAML import",
    "histogram-invalid-column": "Invalid histogram column",
    "schema-missing-engine": "Missing schema engine",
}


def human_time(value: str | None, short: bool = False) -> str:
    """Format persisted ISO timestamps in UTC, leaving missing dates explicit."""
    if not value:
        return "Not recorded"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return parsed.strftime("%H:%M:%S UTC" if short else "%d %b %Y · %H:%M UTC")
    except (ValueError, TypeError):
        return "Time unavailable"


def job_title(job: dict[str, Any]) -> str:
    """Prefer a short domain label, retaining the full contract title in detail."""
    return SHORT_TITLES.get(job["case_id"], job["case_title"] or job["case_id"])


def group_matches(job: dict[str, Any], view: str) -> bool:
    """Match terminal or active states without treating every non-pass as failed."""
    status = linked_verdict(job)
    return (view == "all" or (view == "attention" and status in TERMINAL_ATTENTION)
            or (view == "verified" and status == "VERIFIED")
            or (view == "active" and status not in TERMINAL_ATTENTION | {"VERIFIED"}))


def workbench(report: dict[str, Any], query: str = "", view: str = "all",
              kind: str = "all", sort: str = "attention", page: int = 1) -> dict[str, Any]:
    """Build a bounded, URL-addressable view; global metrics remain clearly global."""
    query = query.strip()[:200]
    view = view if view in VIEWS else "all"
    kind = kind if kind in KINDS else "all"
    sort = sort if sort in {"attention", "newest", "oldest"} else "attention"
    matching = []
    for job in report["jobs"]:
        haystack = " ".join(str(job.get(key) or "") for key in (
            "id", "issue_number", "case_id", "case_title", "candidate_sha", "repository"))
        if query.casefold() in haystack.casefold() and (kind == "all" or job["case_kind"] == kind):
            matching.append(job)
    counts = {name: sum(group_matches(job, name) for job in matching) for name in VIEWS}
    rows = [job for job in matching if group_matches(job, view)]
    rows.sort(key=lambda job: (job["created_at"] or "", job["id"]), reverse=sort != "oldest")
    if sort == "attention":
        # Stable sort retains newest-first within each priority group.
        rows.sort(key=lambda job: 0 if group_matches(job, "attention") else 1)
    total = len(rows)
    page_size = 20
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    params = {"q": query, "view": view, "kind": kind, "sort": sort}

    def url(**changes: Any) -> str:
        """Encode user text rather than interpolating it into navigation links."""
        return "/dashboard?" + urlencode({**params, **changes})

    return {
        "verified_count": sum(group_matches(job, "verified") for job in report["jobs"]),
        "attention_count": sum(group_matches(job, "attention") for job in report["jobs"]),
        "unlinked_count": sum(linked_verdict(job) == "INCOMPLETE" for job in report["jobs"]),
        **params, "rows": rows[(page - 1) * page_size:page * page_size],
        "counts": counts, "total": total, "page": page, "pages": pages,
        "start": (page - 1) * page_size + 1 if total else 0,
        "end": min(page * page_size, total),
        "tabs": [{"id": name, "label": label, "count": counts[name], "url": url(view=name)}
                 for name, label in (("all", "All runs"), ("attention", "Needs attention"),
                                     ("verified", "Verified"), ("active", "In progress"))],
        "previous": url(page=page - 1), "next": url(page=page + 1),
        "return_query": urlencode({**params, "page": page}),
        "filtered": bool(query or view != "all" or kind != "all"),
    }


def recorded_evidence() -> dict[str, Any] | None:
    """Read the checked-in, dated run; it is never blended into workspace metrics.

    Fail closed on incomplete evidence instead of rendering a misleading green
    result. URLs are constructed from the known public repository, not JSON URLs.
    """
    try:
        record = json.loads(ARCHIVE_PATH.read_text())
        candidate, job, baseline = record["candidate"], record["job"], record["baseline"]
        sha = candidate["head_sha"]
        if (record["workflow"] != "LIVE" or record["case_id"] != "import-unparseable-yaml"
                or baseline["outcome"] != "CONFIRMED" or baseline["application_status"] != "REGRESSION"
                or not baseline["provenance"] or not job["provenance"]
                or job["validation_status"] != "VERIFIED" or job["status"] != "VERIFIED" or job["application_status"] != "PASS"
                or candidate["validated_sha"] != sha or not valid_sha(sha) or not valid_sha(baseline["sha"])):
            return None
        return {
            "recorded_at": record["recorded_at_utc"], "sha": sha,
            "baseline_sha": baseline["sha"], "baseline": baseline["application_status"],
            "application": job["application_status"], "corrections": job["correction_count"],
            "started_at": job["timestamps_utc_as_stored"]["started_at"], "completed_at": job["timestamps_utc_as_stored"]["completed_at"],
            "pr": candidate["pr_number"],
            "pr_url": f"https://github.com/dheeraj5612/superset/pull/{int(candidate['pr_number'])}",
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def valid_sha(value: Any) -> bool:
    """Accept complete lowercase Git commit IDs, never placeholders or display abbreviations."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def linked_verdict(job: dict[str, Any]) -> str:
    """Do not endorse a VERIFIED state without an exact candidate/validation link."""
    if job["status"] == "VERIFIED" and (not valid_sha(job.get("candidate_sha"))
                                        or job.get("validated_sha") != job.get("candidate_sha")):
        return "INCOMPLETE"
    return job["status"]
