"""Truthful, read-only evidence views shared by the dashboard and JSON export.

The report deliberately mirrors persisted control-plane facts.  It does not
invent cost, merge, or live-remediation outcomes when the database has none.

ELI5: this file turns the job diary into a safe story that both the page and
the download can tell without guessing what happened.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.cases import Case, Registry
from app.config import Settings
from app.db import Store
from app.devin import launch_preflight
from app.metrics import metrics_from_records
from app.models import Event, Job

# ELI5: this version label tells download consumers which report shape they received.
REPORT_SCHEMA = "devin-remediation-evidence/v1"


# ELI5: this helper turns timestamps into one portable UTC spelling.
def _iso(value: Any) -> str | None:
    """Return a stable UTC string, or null when a timestamp was never set."""
    # ELI5: keep a missing timestamp missing instead of inventing a date.
    if value is None:
        # ELI5: a missing database timestamp stays missing instead of becoming a fake date.
        return None
    # ELI5: database timestamps without a timezone are documented as UTC.
    if getattr(value, "tzinfo", None) is None:
        # ELI5: naive values from SQLite are documented as UTC, so attach that timezone first.
        return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    # ELI5: convert an already-zoned value to UTC for one portable export format.
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ELI5: this helper allows only secure provider links into the customer view.
def _safe_url(value: Any) -> str | None:
    """Allow only secure web links from provider records into clickable dashboard fields."""
    # ELI5: return the original secure URL, or null when the value is missing or unsafe.
    return value if isinstance(value, str) and value.startswith("https://") else None


# ELI5: this helper shapes one timeline event for safe JSON and HTML use.
def _event_record(event: Event) -> dict[str, Any]:
    """Make one append-only event safe to serialize and display to a customer."""
    # ELI5: keep the event identity so a reviewer can connect the timeline row to the job.
    return {
        "id": event.id,
        # ELI5: keep the parent job identity for export consumers.
        "job_id": event.job_id,
        # ELI5: retain the short event name, which explains the state-machine handoff.
        "type": event.event_type,
        # ELI5: normalize the timestamp so browsers see one UTC format.
        "timestamp": _iso(event.timestamp),
        # ELI5: expose only the sanitized coordinates approved by _safe_event_details.
        "details": _safe_event_details(event),
    }


# ELI5: this helper names the strongest evidence currently recorded for a job.
def evidence_level(mode: str, job: Job | None) -> str:
    """Classify how much evidence exists without treating a PR link as proof."""
    # ELI5: simulation labels stay synthetic even if their scripted state says verified.
    if mode == "SIMULATION":
        # ELI5: every simulation row is visibly synthetic, even when its scripted state says VERIFIED.
        return "SIMULATED"
    # ELI5: a case with no job has no execution evidence yet.
    if job is None:
        # ELI5: a registered case without a job has no execution evidence yet.
        return "NO_RUN"
    # ELI5: independent verification needs both the terminal status and tested SHA.
    if job.status == "VERIFIED" and job.validated_sha:
        # ELI5: live verification needs both the terminal status and the exact SHA that passed.
        return "INDEPENDENTLY_VERIFIED"
    # ELI5: a candidate link or SHA shows an artifact was observed, not approved.
    if job.candidate_sha or job.candidate_pr_url:
        # ELI5: a candidate artifact was seen, but it has not earned an independent verdict.
        return "CANDIDATE_OBSERVED"
    # ELI5: a session ID shows contact with Devin, not a successful repair.
    if job.devin_session_id:
        # ELI5: a saved session proves that Devin was observed, not that it is still running.
        return "SESSION_OBSERVED"
    # ELI5: an admitted job is durable intent only; no provider artifact is recorded yet.
    return "ADMITTED"


# ELI5: this helper keeps the small trusted validation matrix for a job.
def _validation(job: Job | None) -> dict[str, Any]:
    """Extract the small validation matrix that a reviewer needs first."""
    # ELI5: only a JSON object has the fields this report understands; all other shapes are not run.
    raw = job.validation if job and isinstance(job.validation, dict) else {}
    # ELI5: keep both the normalized state-machine result and the trusted application label recognizable.
    safe_outcomes = {"VERIFIED", "NORMAL_FAILED", "REGRESSION_SURVIVED", "APPLICATION_FAILED", "CONTRACT_FAILED", "SCOPE_REJECTED", "STALE_SHA", "INFRA_ERROR", "NOT_RUN", "REGRESSION"}
    # ELI5: prefer the durable state-machine column, then the safe validation summary, then NOT_RUN.
    outcome = (job.validation_status if job else None) or raw.get("outcome") or "NOT_RUN"
    valid_outcome = isinstance(outcome, str) and outcome in safe_outcomes
    # ELI5: accept either application field name used by older and newer persisted records.
    application_status = raw.get("application_status") or raw.get("application_outcome")
    # ELI5: older records may store the application result under a shorter key.
    if not application_status and isinstance(raw.get("application"), str):
        # ELI5: the validator's `application` field is the trusted contract result for application cases.
        application_status = raw["application"]
    # ELI5: these are the only application labels the report will display as trusted status.
    allowed_application = {"PASS", "REGRESSION", "CONTRACT_FAILED", "VERIFIED", "FAILED", "INFRA_ERROR", "NOT_RUN"}
    # ELI5: these are the only normal/mutant phase labels the report will display as trusted status.
    allowed_phase = {"PASS", "ASSERTION_FAILED", "NOT_RUN", "INFRA_ERROR", "INVALID_CONTROL", "NOT_VERIFIED", "NOT_APPLICABLE"}
    # ELI5: absent phase values become an honest NOT_RUN rather than an invented pass.
    normal = raw.get("normal") or "NOT_RUN"
    # ELI5: the mutant phase gets the same explicit missing-value treatment.
    mutant = raw.get("mutant") or "NOT_RUN"
    # ELI5: return only fields needed by the UI, with every unrecognized verdict downgraded.
    return {
        # ELI5: preserve a known state-machine outcome, or show that it was not classified.
        "outcome": outcome if valid_outcome else "UNCLASSIFIED",
        # ELI5: a summary is safe only when its outcome itself is on the allow-list.
        "summary": raw.get("summary") if valid_outcome and isinstance(raw.get("summary"), str) else None,
        # ELI5: keep a recognized normal phase, or make the uncertainty visible.
        "normal": normal if isinstance(normal, str) and normal in allowed_phase else "UNCLASSIFIED",
        # ELI5: keep a recognized mutant phase, or make the uncertainty visible.
        "mutant": mutant if isinstance(mutant, str) and mutant in allowed_phase else "UNCLASSIFIED",
        # ELI5: application cases get their contract label; test cases leave this empty.
        "application_status": application_status
        if isinstance(application_status, str) and application_status in allowed_application else None,
        # ELI5: the candidate SHA identifies the artifact the provider produced.
        "candidate_sha": job.candidate_sha if job else None,
        # ELI5: the validated SHA identifies the artifact the independent checker actually tested.
        "validated_sha": job.validated_sha if job else None,
        # ELI5: expose only a yes/no evidence marker, never raw subprocess output.
        "evidence_recorded": bool(raw.get("evidence")),
    }


# ELI5: this helper filters event details before they reach a customer-facing view.
def _safe_event_details(event: Event) -> dict[str, Any]:
    """Keep useful event coordinates while excluding raw provider or subprocess text."""
    # ELI5: these coordinates explain the workflow while excluding arbitrary provider text.
    allowed = {"outcome", "correction_count", "sha", "normal", "mutant", "session_id", "attempt", "stale_count", "application", "application_status"}
    # ELI5: only known labels may pass through a timeline status field.
    statuses = {"PASS", "ASSERTION_FAILED", "NOT_RUN", "INFRA_ERROR", "INVALID_CONTROL", "NOT_VERIFIED", "NOT_APPLICABLE",
                "VERIFIED", "NORMAL_FAILED", "REGRESSION_SURVIVED", "APPLICATION_FAILED", "SCOPE_REJECTED", "STALE_SHA",
                "REGRESSION", "CONTRACT_FAILED"}
    # ELI5: build a fresh safe map instead of mutating the database event.
    safe: dict[str, Any] = {}
    # ELI5: inspect each stored detail and copy only fields the report understands.
    details = event.details if isinstance(event.details, dict) else {}
    for key, value in details.items():
        # ELI5: skip fields that are outside the small public event vocabulary.
        if key not in allowed:
            # ELI5: unlisted fields may contain secrets, commands, or provider prose, so omit them.
            continue
        # ELI5: only allow known status words in fields that describe a verdict.
        if key in {"outcome", "normal", "mutant", "application", "application_status"} and (
            not isinstance(value, str) or value not in statuses
        ):
            # ELI5: an unknown status is less useful than exposing an unsafe string, so omit it.
            continue
        # ELI5: keep this one approved coordinate for the customer timeline.
        safe[key] = value
    # ELI5: return the sanitized event details to both HTML and JSON.
    return safe


# ELI5: this helper converts one database row into the dashboard's safe job record.
def _job_record(mode: str, job: Job, events: list[Event], case: Case | None = None) -> dict[str, Any]:
    """Convert one database job into a presentation and export record."""
    # ELI5: normalize validation once so every job view uses the same safe matrix.
    validation = _validation(job)
    # ELI5: old records default to test quality; new application cases carry their explicit kind.
    case_kind = getattr(case, "kind", "test_quality")
    # ELI5: return one export-shaped row with safe links, identifiers, and event records.
    return {
        # ELI5: the database ID makes the detail link stable.
        "id": job.id,
        # ELI5: retain the issue number that caused this durable job.
        "issue_number": job.issue_number,
        # ELI5: show which configured repository the issue belongs to.
        "repository": job.repository,
        # ELI5: keep the registry case key beside the provider job.
        "case_id": job.case_id,
        # ELI5: expose whether the case uses normal/mutant tests or an application contract.
        "case_kind": case_kind,
        # ELI5: give the UI a customer-friendly workflow label.
        "workflow_type": "APPLICATION_REGRESSION" if case_kind == "application" else "TEST_REPAIR",
        # ELI5: use the registered title, and a safe key fallback for old records.
        "case_title": case.title if case else job.case_id,
        # ELI5: preserve the durable state-machine status.
        "status": job.status,
        # ELI5: make synthetic verified rows visibly synthetic.
        "status_label": "SIMULATED VERIFIED" if mode == "SIMULATION" and job.status == "VERIFIED" else job.status,
        # ELI5: classify evidence without mistaking a link for independent proof.
        "evidence_level": evidence_level(mode, job),
        # ELI5: show how many corrections were recorded.
        "correction_count": job.correction_count,
        # ELI5: state the fixed one-correction policy used by the worker.
        "correction_limit": 1,
        # ELI5: replace raw failure text with a safe operator-review flag.
        "failure_state": "OPERATOR_REVIEW" if job.failure_reason else "NONE",
        # ELI5: retain a session ID for joins without exposing provider prose.
        "session_id": job.devin_session_id,
        # ELI5: only secure session links become clickable.
        "session_url": _safe_url(job.devin_session_url),
        # ELI5: keep the provider PR number as a harmless identifier.
        "candidate_pr_number": job.candidate_pr_number,
        # ELI5: only secure PR links become clickable.
        "candidate_pr_url": _safe_url(job.candidate_pr_url),
        # ELI5: show the exact candidate artifact presented for validation.
        "candidate_sha": job.candidate_sha,
        # ELI5: show the exact artifact that earned an independent verdict, when present.
        "validated_sha": job.validated_sha,
        # ELI5: keep the normalized top-level result easy for table consumers to read.
        "validation_status": validation["outcome"],
        # ELI5: keep the full safe normal/mutant or application matrix.
        "validation": validation,
        # ELI5: normalize creation time for export consumers.
        "created_at": _iso(job.created_at),
        # ELI5: normalize worker-start time for export consumers.
        "started_at": _iso(job.started_at),
        # ELI5: normalize observed-PR time for export consumers.
        "pr_created_at": _iso(job.pr_created_at),
        # ELI5: normalize terminal time for export consumers.
        "completed_at": _iso(job.completed_at),
        # ELI5: count persisted events without inventing missing timeline rows.
        "event_count": len(events),
        # ELI5: export the same sanitized events that the selected timeline renders.
        "events": [_event_record(event) for event in events],
        # ELI5: group safe navigation links under one predictable object.
        "links": {
            # ELI5: the issue URL identifies the source finding on the configured repository.
            "issue": f"https://github.com/{job.repository}/issues/{job.issue_number}",
            # ELI5: a missing or unsafe session URL becomes null.
            "session": _safe_url(job.devin_session_url),
            # ELI5: a missing or unsafe PR URL becomes null.
            "pull_request": _safe_url(job.candidate_pr_url),
        },
    }


# ELI5: this helper presents either synthetic proof or the registry's checked baseline.
def _case_proof(settings: Settings, registry: Registry, case: Case) -> dict[str, Any]:
    """Load live baseline proof while keeping simulation evidence explicitly synthetic."""
    # ELI5: simulation proof explains the demo while staying visibly non-live.
    if settings.mode == "SIMULATION":
        # ELI5: the demo uses scripted adapters, so this is a demonstration state, not a baseline claim.
        application = getattr(case, "kind", "test_quality") == "application"
        # ELI5: return synthetic labels and mark test phases not used by application contracts.
        return {
            # ELI5: this label says the baseline is a demo marker rather than a measured result.
            "outcome": "SIMULATED",
            # ELI5: application contracts do not run the normal test-quality phase.
            "normal": "NOT_APPLICABLE" if application else "PASS",
            # ELI5: application contracts do not run the mutant test-quality phase.
            "mutant": "NOT_APPLICABLE" if application else "PASS",
            # ELI5: keep the application baseline equally explicit about its synthetic nature.
            "application_status": "SIMULATED" if application else None,
            # ELI5: label every simulation proof as synthetic.
            "evidence_level": "SIMULATED",
            # ELI5: explain the boundary in the card and JSON export.
            "statement": "Synthetic demo state. No live baseline was measured.",
        }
    # ELI5: live proof is read through the registry so fingerprints and provenance are enforced.
    try:
        # ELI5: ask the registry to enforce the stored baseline fingerprint and provenance.
        proof = registry.evidence(case)
        if not isinstance(proof, dict):
            # ELI5: a valid JSON scalar or list is still not a usable proof record.
            raise TypeError("Baseline proof must be an object")
    # ELI5: stale or missing proof becomes a safe pending state instead of an exception page.
    except (AttributeError, OSError, TypeError, ValueError):
        # ELI5: represent missing or stale evidence without leaking the exception text.
        return {
            "outcome": "UNCONFIRMED",
            "normal": "NOT_RUN",
            "mutant": "NOT_RUN",
            "evidence_level": "BASELINE_PENDING",
            "statement": "Current baseline proof is unavailable or stale.",
        }
    # ELI5: export only the stable proof coordinates needed by a live operator.
    return {
        # ELI5: retain the registry's checked baseline verdict.
        "outcome": proof.get("outcome", "UNCONFIRMED"),
        # ELI5: preserve the normal phase classification when present.
        "normal": proof.get("normal", "NOT_RUN"),
        # ELI5: preserve the mutant phase classification when present.
        "mutant": proof.get("mutant", "NOT_RUN"),
        # ELI5: preserve only a recognized trusted application result.
        "application_status": (proof.get("application_status") or proof.get("application"))
        if (proof.get("application_status") or proof.get("application")) in {"PASS", "REGRESSION", "CONTRACT_FAILED", "VERIFIED", "FAILED", "INFRA_ERROR"} else None,
        # ELI5: this branch passed the registry's live baseline gate.
        "evidence_level": "BASELINE_CONFIRMED",
        # ELI5: keep the exact pinned source identity for audit comparison.
        "sha": proof.get("sha"),
        # ELI5: retain when the baseline proof was recorded.
        "recorded_at": proof.get("recorded_at"),
        # ELI5: retain the case fingerprint that binds the proof to its contract.
        "case_fingerprint": proof.get("case_fingerprint"),
        # ELI5: retain the harness fingerprint that binds the proof to its validator.
        "harness_fingerprint": proof.get("harness_fingerprint"),
        # ELI5: tell the operator why this proof is eligible for a live case.
        "statement": "Live baseline proof is current and fingerprinted.",
    }


# ELI5: this helper describes one approved case beside its newest observed job.
def _case_record(settings: Settings, registry: Registry, case: Case, jobs: list[Job]) -> dict[str, Any]:
    """Describe one approved workflow and its before/after evidence."""
    # ELI5: find this case's jobs so the portfolio can show its latest observed state.
    matching = [job for job in jobs if job.case_id == case.id]
    # ELI5: the newest persisted row is the best current candidate summary.
    latest = matching[-1] if matching else None
    # ELI5: baseline proof comes from the registry or an explicit simulation marker.
    proof = _case_proof(settings, registry, case)
    # ELI5: the contract and allowed paths tell a customer what the agent was allowed to touch.
    return {
        # ELI5: retain the immutable case key.
        "id": case.id,
        # ELI5: show the approved human-readable case title.
        "title": case.title,
        # ELI5: classify the portfolio card for the appropriate oracle labels.
        "workflow_type": "APPLICATION_REGRESSION" if getattr(case, "kind", "test_quality") == "application" else "TEST_REPAIR",
        # ELI5: preserve the raw case kind for export consumers.
        "case_kind": getattr(case, "kind", "test_quality"),
        # ELI5: show the intended PR base when the case pins one.
        "target_branch": getattr(case, "target_branch", None),
        # ELI5: identify the source repository approved by the registry.
        "source_repository": case.source_repository,
        # ELI5: retain the named regression or application challenge.
        "challenge": case.challenge,
        # ELI5: retain the registered test node IDs for test-quality cases.
        "test_ids": case.test_ids,
        # ELI5: show the only paths Devin may edit.
        "allowed_paths": case.allowed_paths,
        # ELI5: show the production paths the contract protects.
        "affected_paths": case.affected_paths,
        # ELI5: export the plain-English behavior contract.
        "contract": case.contract,
        # ELI5: export the accepted outcome description.
        "acceptance": case.acceptance,
        # ELI5: show the trusted application harness when this is an application case.
        "acceptance_test": getattr(case, "acceptance_test", None),
        # ELI5: show why an operator approved the case.
        "inspection": case.inspection,
        # ELI5: nest baseline proof so synthetic and live boundaries stay visible.
        "baseline": proof,
        # ELI5: link the portfolio card to its newest job when one exists.
        "latest_job_id": latest.id if latest else None,
        # ELI5: classify the newest job with the same evidence rules as the job table.
        "latest_evidence_level": evidence_level(settings.mode, latest),
        # ELI5: expose the newest safe oracle matrix beside the baseline.
        "latest_validation": _validation(latest),
        # ELI5: show waiting status when no candidate job exists yet.
        "latest_status": latest.status if latest else "AWAITING_CANDIDATE",
    }


# ELI5: this helper gives one setup gate a safe label, state, and explanation.
def _check(label: str, ready: bool, detail: str, *, state: str | None = None) -> dict[str, str]:
    """Build one readiness row without exposing secrets or credential values."""
    # ELI5: use an explicit state override when simulation needs NOT_EVALUATED or SIMULATION.
    return {"label": label, "state": state or ("PASS" if ready else "BLOCKED"), "detail": detail}


# ELI5: this helper summarizes live setup gates without making a provider call.
def pilot_readiness(settings: Settings, registry: Registry) -> dict[str, Any]:
    """Report operator-visible pilot gates using read-only configuration checks."""
    # ELI5: collect configuration errors once so the live readiness panel shares one answer.
    problems = settings.live_errors()
    # ELI5: simulation can show the gates, but it must never call itself production-ready.
    if settings.mode == "SIMULATION":
        # ELI5: show the two live gates as not evaluated instead of calling a demo production-ready.
        checks = [
            _check("Execution mode", False, "Simulation is intentionally isolated from paid calls.", state="SIMULATION"),
            _check("Configuration gates", False, "Run make doctor in LIVE mode before a pilot.", state="NOT_EVALUATED"),
        ]
        # ELI5: return the explicit simulation boundary and its safe readiness rows.
        return {
            # ELI5: identify this panel as simulation-only.
            "state": "SIMULATION",
            # ELI5: give the header a short human-readable label.
            "label": "Simulation only",
            # ELI5: explain why passing scripted jobs is not a live pilot gate.
            "message": "The demo proves orchestration behavior, not live remediation.",
            # ELI5: simulation deliberately has no configuration failure list.
            "problems": [],
            # ELI5: expose the two safe rows built above.
            "checks": checks,
        }

    # ELI5: the context bundle is a file gate that can be checked without a provider call.
    context_path = settings.storage / "context.json"
    # ELI5: require both a real checkout and an executable interpreter, matching launch_preflight.
    interpreter_ready = (settings.superset_repo_path.is_dir()
                         and settings.superset_python.is_file()
                         and os.access(settings.superset_python, os.X_OK))
    # ELI5: start with the safe file check before parsing any operator context.
    context_ready = context_path.is_file()  # ELI5: a present file still needs the shared semantic preflight.
    # ELI5: only run the deeper local preflight when its two required files exist.
    if interpreter_ready and context_ready:
        # ELI5: prefer issue-bound cases, while retaining a fallback for an empty issue map.
        active_cases = list({case.id: case for case in registry.by_issue.values()}.values())
        # ELI5: use the active bindings when present, otherwise check every registered case.
        cases_to_check = active_cases or list(registry.cases.values())
        # ELI5: every active case must pass the same local checks used before enqueueing.
        for active_case in cases_to_check:
            # ELI5: check each case independently so one failure can block readiness safely.
            try:
                # ELI5: this reads only local files and checks repository, branch, and resource identity.
                launch_preflight(settings, active_case, settings.github_repository)
            # ELI5: any malformed local context means this gate is not ready.
            except (AttributeError, OSError, TypeError, ValueError, KeyError):
                # ELI5: malformed or mismatched context blocks readiness without exposing raw details.
                context_ready = False
                # ELI5: stop checking after the first failed case because readiness is already blocked.
                break
    # ELI5: every registered case must have exactly one issue binding before launch.
    bindings_ready = set(settings.case_issues) == set(registry.cases)
    # ELI5: start optimistic, then turn this off when any baseline proof is stale or missing.
    baselines_ready = True
    # ELI5: one missing or stale baseline blocks the whole set of approved cases.
    for case in registry.cases.values():
        # ELI5: check this case's proof independently from the other cases.
        try:
            # ELI5: registry.evidence performs the current fingerprint and provenance check.
            registry.evidence(case)
        # ELI5: a single case without current proof blocks the aggregate baseline gate.
        except (AttributeError, OSError, TypeError, ValueError):
            # ELI5: one bad case blocks the aggregate baseline gate.
            baselines_ready = False
            # ELI5: stop once the aggregate answer is known to be blocked.
            break
    # ELI5: each row names one live preflight requirement and its safe explanatory text.
    checks = [
        _check("Live execution switch", settings.enable_live, "ENABLE_LIVE must be true."),
        _check("Dedicated repository", settings.github_repository.lower() != "apache/superset" and bool(settings.github_repository),
               "Use a configured fork and repository ID."),
        _check("Disposable validator", settings.allow_local_validation, "ALLOW_LOCAL_VALIDATION must be true."),
        _check("Superset interpreter", interpreter_ready, "Prepared evaluator interpreter is required."),
        _check("Devin context", context_ready, "Run bootstrap-context after confirming baselines."),
        _check("Case bindings", bindings_ready, "Every registered case needs one approved issue."),
        _check("Baseline evidence", baselines_ready, "Every approved case needs current confirmed proof."),
    ]
    # ELI5: a pilot is blocked when either settings or a visible readiness row says it is blocked.
    blocked_checks = [check["label"] for check in checks if check["state"] == "BLOCKED"]
    # ELI5: combine safe names so the operator sees why the aggregate state is blocked.
    all_problems = [*problems, *blocked_checks]
    # ELI5: live mode is ready only when every actual gate passes.
    return {
        # ELI5: distinguish configuration-ready from blocked state.
        "state": "CONFIGURATION_READY" if not all_problems else "BLOCKED",
        # ELI5: make the same state readable in the dashboard heading.
        "label": "Configuration gates pass" if not all_problems else "Pilot blocked",
        # ELI5: direct the operator to the command that repairs missing setup.
        "message": "Run make doctor before spending on a live pilot.",
        # ELI5: show only safe human-readable problem names from settings validation.
        "problems": all_problems,
        # ELI5: include the detailed gate rows for operator review.
        "checks": checks,
    }


# ELI5: this helper counts the durable handoffs that make up the workflow funnel.
def _workflow_steps(jobs: list[Job]) -> list[dict[str, Any]]:
    """Summarize the event-to-oracle path without inventing downstream review state."""
    # ELI5: count a session only when its ID was persisted.
    sessions = sum(bool(job.devin_session_id) for job in jobs)
    # ELI5: count a candidate only when a SHA or provider PR URL was persisted.
    candidates = sum(bool(job.candidate_sha or job.candidate_pr_url) for job in jobs)
    # ELI5: count an oracle handoff only when a durable validation field exists.
    evaluations = sum(bool(job.validation_status or job.validation) for job in jobs)
    # ELI5: count terminal verified states without implying a merge.
    verified = sum(job.status == "VERIFIED" for job in jobs)
    # ELI5: return five persisted handoff counters for the dashboard funnel.
    return [
        # ELI5: every job began as an approved finding.
        {"id": "admitted", "label": "Approved findings", "count": len(jobs), "detail": "Signed, allow-listed jobs"},
        # ELI5: this step records provider session identity.
        {"id": "sessions", "label": "Devin sessions", "count": sessions, "detail": "Session identity persisted"},
        # ELI5: this step records an exact candidate artifact or PR link.
        {"id": "candidates", "label": "Candidate PRs", "count": candidates, "detail": "PR or exact head observed"},
        # ELI5: this step records normal/mutant or application contract evaluation.
        {"id": "evaluated", "label": "Oracle evaluations", "count": evaluations, "detail": "Normal/mutant or application contract recorded"},
        # ELI5: this step records the terminal verified state only.
        {"id": "verified", "label": "Independently verified", "count": verified, "detail": "Human review still required"},
    ]


# ELI5: this helper builds the one report snapshot shared by HTML and JSON.
def build_report(settings: Settings, store: Store, registry: Registry) -> dict[str, Any]:
    """Build the complete JSON report used by the dashboard and download endpoint."""
    # ELI5: take one job snapshot so the dashboard and export agree.
    jobs = store.jobs()
    # ELI5: take the matching event snapshot for timeline and denominator calculations.
    events = store.events()
    # ELI5: group events by job so each normalized row can carry its own timeline.
    events_by_job: dict[str, list[Event]] = defaultdict(list)
    # ELI5: place each event under its parent job so detail pages can show its timeline.
    for event in events:
        # ELI5: put each append-only event under its persisted parent job.
        events_by_job[event.job_id].append(event)
    # ELI5: normalize every job once, including its registered case kind.
    job_records = [_job_record(settings.mode, job, events_by_job[job.id], registry.cases.get(job.case_id)) for job in jobs]
    # ELI5: return one export-shaped snapshot shared by HTML and /report.json.
    return {
        # ELI5: identify the stable consumer schema.
        "schema": REPORT_SCHEMA,
        # ELI5: tell consumers when this snapshot was generated in UTC.
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        # ELI5: keep the truth boundary next to every other report field.
        "truth": {
            # ELI5: identify the configured execution mode.
            "mode": settings.mode,
            # ELI5: make simulation status a boolean for simple consumers.
            "simulation": settings.mode == "SIMULATION",
            # ELI5: state exactly what the snapshot can prove.
            "statement": (
                "Synthetic adapters and local timings. No live remediation evidence."
                if settings.mode == "SIMULATION"
                else "Live control-plane records only; human review remains required."
            ),
        },
        # ELI5: calculate KPIs from the same job/event snapshot.
        "metrics": metrics_from_records(jobs, events),
        # ELI5: expose live setup gates without performing live work.
        "readiness": pilot_readiness(settings, registry),
        # ELI5: expose persisted event-to-oracle handoff counters.
        "workflow": {"mode": settings.mode, "steps": _workflow_steps(jobs)},
        # ELI5: include every allow-listed case contract and its latest proof.
        "cases": [_case_record(settings, registry, case, jobs) for case in registry.cases.values()],
        # ELI5: include the normalized job rows and safe timelines.
        "jobs": job_records,
    }
