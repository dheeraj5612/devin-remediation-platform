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
    "superset-normalize-dttm-edge-cases": "Datetime edge cases",
}

# ELI5: name each Devin API call so a reader knows which endpoint ran and why.
PROVIDER_OPERATION_LABELS = {
    "attachment_upload": "Attachments API · upload",
    "session_create": "Sessions API · create",
    "session_poll": "Sessions API · poll",
    "list_reconcile": "Sessions API · list by tag",
    "correction_message": "Sessions API · message",
}
# ELI5: one short, presenter-style cue per operation, paraphrased from the Loom script.
PROVIDER_OPERATION_CUES = {
    "attachment_upload": "Uploads the case's baseline proof for Devin.",
    "session_create": "Starts one session capped at 3 ACUs, referencing playbook, note, attachment.",
    "session_poll": "Polls session status until Devin opens a PR.",
    "list_reconcile": "Recovers this job's session by tag after a restart.",
    "correction_message": "Sends one correction if the oracle rejects the PR.",
}
PROVIDER_OPERATION_ORDER = tuple(PROVIDER_OPERATION_LABELS)
# ELI5: name and cue the two once-per-repository bootstrap resources the same way.
BOOTSTRAP_RESOURCE_LABELS = {"playbook": "Playbooks API", "knowledge": "Knowledge API"}
BOOTSTRAP_RESOURCE_CUES = {
    "playbook": "How Devin works: reproduce, smallest fix, allowed files only, "
                "never edit the checker, never merge.",
    "knowledge": "Case rules from evals/cases.yaml: branch, baseline, allowed files.",
}


def human_time(value: str | None, short: bool = False) -> str:
    """Format persisted ISO timestamps in UTC, leaving missing dates explicit."""
    # ELI5: only a real string can be parsed; malformed stored values must not break a page.
    if value is None:
        return "Not recorded"
    if not isinstance(value, str) or not value:
        return "Time unavailable"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return parsed.strftime("%H:%M:%S UTC" if short else "%d %b %Y · %H:%M UTC")
    except (ValueError, TypeError):
        return "Time unavailable"


def job_title(job: dict[str, Any]) -> str:
    """Prefer a short domain label, retaining the full contract title in detail."""
    return SHORT_TITLES.get(job["case_id"], job["case_title"] or job["case_id"])


def _safe_nonnegative(value: Any, *, decimal: bool = False) -> int | float | None:
    """Keep aggregate values numeric before handing them to a template."""
    # ELI5: malformed report values become missing rather than visible fake measurements.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return round(value, 3) if decimal else value


def provider_api_card(summary: dict[str, Any] | None, bootstrap: dict[str, Any] | None = None,
                     mode: str | None = None, max_acu: int | None = None) -> dict[str, Any]:
    """Prepare a compact, safe provider activity card for dashboard templates."""
    # ELI5: tolerate older reports that predate provider operation summaries.
    summary = summary if isinstance(summary, dict) else {}
    selected_mode = summary.get("mode") if summary.get("mode") in {"LIVE", "SIMULATION"} else mode
    selected_mode = selected_mode or "NOT_RECORDED"
    event_count = _safe_nonnegative(summary.get("event_count")) or 0
    success_count = _safe_nonnegative(summary.get("success_count")) or 0
    event_count = int(event_count)
    success_count = min(int(success_count), event_count)
    latency_raw = summary.get("latency_ms") if isinstance(summary.get("latency_ms"), dict) else {}
    latency = {
        "count": int(_safe_nonnegative(latency_raw.get("count")) or 0),
        "median": _safe_nonnegative(latency_raw.get("median"), decimal=True),
        "average": _safe_nonnegative(latency_raw.get("average"), decimal=True),
    }
    operation_map = summary.get("operations") if isinstance(summary.get("operations"), dict) else {}
    operation_rows = []
    for key in PROVIDER_OPERATION_ORDER:
        raw = operation_map.get(key) if isinstance(operation_map.get(key), dict) else {}
        count = int(_safe_nonnegative(raw.get("event_count")) or 0)
        successes = min(int(_safe_nonnegative(raw.get("success_count")) or 0), count)
        raw_latency = raw.get("latency_ms") if isinstance(raw.get("latency_ms"), dict) else {}
        # ELI5: the session-create cue quotes the app's real configured spend cap, never a guess.
        cue = (f"Starts one session capped at {max_acu} ACUs, referencing playbook, note, attachment."
               if key == "session_create" and isinstance(max_acu, int) and not isinstance(max_acu, bool) and max_acu > 0
               else PROVIDER_OPERATION_CUES[key])
        operation_rows.append({
            "key": key,
            "label": PROVIDER_OPERATION_LABELS[key],
            "cue": cue,
            "event_count": count,
            "success_count": successes,
            "latency_ms": {
                "count": int(_safe_nonnegative(raw_latency.get("count")) or 0),
                "median": _safe_nonnegative(raw_latency.get("median"), decimal=True),
            },
        })
    # ELI5: an omitted bootstrap summary remains visibly unrecorded on every surface.
    raw_bootstrap = bootstrap if isinstance(bootstrap, dict) else summary.get("bootstrap")
    raw_bootstrap = raw_bootstrap if isinstance(raw_bootstrap, dict) else {}
    bootstrap_status = raw_bootstrap.get("status") if raw_bootstrap.get("status") in {"RECORDED", "PARTIAL", "NOT_RECORDED"} else "NOT_RECORDED"

    def resource(key: str) -> dict[str, str]:
        """Keep only the status and create/reuse action for one bootstrap resource."""
        # ELI5: IDs, names, and hashes never enter the template view model.
        raw = raw_bootstrap.get(key) if isinstance(raw_bootstrap.get(key), dict) else {}
        status = raw.get("status") if raw.get("status") in {"CONFIGURED", "NOT_RECORDED"} else "NOT_RECORDED"
        action = raw.get("action") if raw.get("action") in {"created", "reused", "NOT_RECORDED"} else "NOT_RECORDED"
        return {"status": status, "action": action}

    # ELI5: one row per bootstrap resource, always shown even when never recorded.
    bootstrap_rows = [
        {"key": key, "label": BOOTSTRAP_RESOURCE_LABELS[key], "cue": BOOTSTRAP_RESOURCE_CUES[key],
         **resource(key)}
        for key in ("playbook", "knowledge")
    ]

    return {
        "mode": selected_mode,
        "status": "RECORDED" if event_count else "NOT_RECORDED",
        "event_count": event_count,
        "success_count": success_count,
        "failure_count": max(0, event_count - success_count),
        "latency_ms": latency,
        "timing_label": "Synthetic zero-duration timing" if selected_mode == "SIMULATION" else "Recorded local elapsed time",
        "operation_rows": operation_rows,
        "bootstrap": {
            "status": bootstrap_status,
            "timestamp": raw_bootstrap.get("timestamp") if isinstance(raw_bootstrap.get("timestamp"), str) else "NOT_RECORDED",
            "playbook": resource("playbook"),
            "knowledge": resource("knowledge"),
            "rows": bootstrap_rows,
        },
    }


def run_story(job: dict[str, Any]) -> dict[str, Any]:
    """Build safe 'how this run worked' fields from persisted job data only.

    ELI5: every field is either a real recorded fact, taken straight from the
    job dict the report already produced, or the literal string "Not recorded".
    Nothing here is invented.
    """
    links = job.get("links") or {}
    validation = job.get("validation") or {}
    after = validation.get("application_status") if job.get("case_kind") == "application" else validation.get("mutant")
    sha = job.get("candidate_sha")
    return {
        "issue_number": job.get("issue_number"),
        "issue_url": links.get("issue"),
        "case_id": job.get("case_id") or "Not recorded",
        "session_url": links.get("session"),
        "pr_number": job.get("candidate_pr_number"),
        "pr_url": links.get("pull_request"),
        "short_sha": sha[:8] if isinstance(sha, str) and sha else None,
        "after": after or "Not recorded",
        "status": job.get("status") or "Not recorded",
        "label_to_verified": job.get("label_to_verified") or "Not recorded",
    }


def group_matches(job: dict[str, Any], view: str) -> bool:
    """Match terminal or active states without treating every non-pass as failed."""
    status = linked_verdict(job)
    return (view == "all" or (view == "attention" and status in TERMINAL_ATTENTION)
            or (view == "verified" and status == "VERIFIED")
            or (view == "active" and status not in TERMINAL_ATTENTION | {"VERIFIED"}))


def workbench(report: dict[str, Any], query: str = "", view: str = "all",
              kind: str = "all", sort: str = "attention", page: int = 1,
              max_acu: int | None = None) -> dict[str, Any]:
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
        # ELI5: split verified runs by workflow kind for the dashboard's plain-language breakdown.
        "verified_test_quality": sum(group_matches(job, "verified") and job["case_kind"] == "test_quality"
                                      for job in report["jobs"]),
        "verified_application": sum(group_matches(job, "verified") and job["case_kind"] == "application"
                                     for job in report["jobs"]),
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
        # ELI5: keep the dashboard card read-only and derived from the canonical report.
        "provider_api": provider_api_card(report.get("provider_api"), mode=report.get("truth", {}).get("mode"),
                                          max_acu=max_acu),
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
        # ELI5: accept only a positive integer PR number, never a boolean or a display placeholder.
        pr_number = candidate["pr_number"]
        if (isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1
                or record["workflow"] != "LIVE" or record["case_id"] != "import-unparseable-yaml"
                or baseline["outcome"] != "CONFIRMED" or baseline["application_status"] != "REGRESSION"
                or baseline["provenance"] is not True or job["provenance"] is not True
                or job["validation_status"] != "VERIFIED" or job["status"] != "VERIFIED" or job["application_status"] != "PASS"
                or candidate["validated_sha"] != sha or not valid_sha(sha) or not valid_sha(baseline["sha"])):
            return None
        return {
            "recorded_at": record["recorded_at_utc"], "sha": sha,
            "baseline_sha": baseline["sha"], "baseline": baseline["application_status"],
            "application": job["application_status"], "corrections": job["correction_count"],
            "started_at": job["timestamps_utc_as_stored"]["started_at"], "completed_at": job["timestamps_utc_as_stored"]["completed_at"],
            "pr": pr_number,
            "pr_url": f"https://github.com/dheeraj5612/superset/pull/{pr_number}",
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
