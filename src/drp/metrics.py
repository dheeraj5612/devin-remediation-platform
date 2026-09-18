"""Aggregate metrics computed from the job store (Prometheus text format + JSON)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from drp.models import (
    TERMINAL_STATES,
    BaselineStatus,
    Finding,
    JobState,
    RemediationJob,
    ValidationRun,
    Verdict,
    WebhookDelivery,
)


@dataclass
class Metrics:
    findings_total: int = 0
    findings_by_baseline: dict[str, int] = field(default_factory=dict)
    jobs_total: int = 0
    jobs_by_state: dict[str, int] = field(default_factory=dict)
    jobs_active: int = 0
    validations_total: int = 0
    validations_by_verdict: dict[str, int] = field(default_factory=dict)
    webhook_deliveries_total: int = 0
    verified_rate: float | None = None
    escalation_rate: float | None = None
    rejection_rate: float | None = None
    first_attempt_verified_rate: float | None = None
    median_time_to_session_s: float | None = None
    median_time_to_pr_s: float | None = None
    median_time_to_verdict_s: float | None = None
    median_validation_duration_s: float | None = None
    oldest_active_job_age_s: float | None = None
    attempts_per_terminal_job: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _seconds(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _median(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(median(clean), 1) if clean else None


def compute(session: Session) -> Metrics:
    m = Metrics()
    findings = session.scalars(select(Finding)).all()
    m.findings_total = len(findings)
    m.findings_by_baseline = {s.value: 0 for s in BaselineStatus}
    for f in findings:
        m.findings_by_baseline[f.baseline_status.value] += 1

    jobs = session.scalars(select(RemediationJob)).all()
    m.jobs_total = len(jobs)
    m.jobs_by_state = {s.value: 0 for s in JobState}
    for j in jobs:
        m.jobs_by_state[j.state.value] += 1
    active = [j for j in jobs if j.state not in TERMINAL_STATES]
    m.jobs_active = len(active)
    now = datetime.now(UTC)
    if active:
        m.oldest_active_job_age_s = round(
            max((now - j.created_at).total_seconds() for j in active), 1
        )

    terminal = [j for j in jobs if j.state in TERMINAL_STATES]
    if terminal:
        n = len(terminal)
        m.verified_rate = round(sum(j.state == JobState.VERIFIED for j in terminal) / n, 3)
        m.rejection_rate = round(sum(j.state == JobState.REJECTED for j in terminal) / n, 3)
        m.escalation_rate = round(sum(j.state == JobState.ESCALATED for j in terminal) / n, 3)
        m.first_attempt_verified_rate = round(
            sum(j.state == JobState.VERIFIED and j.attempt == 1 for j in terminal) / n, 3
        )
        m.attempts_per_terminal_job = round(sum(j.attempt for j in terminal) / n, 2)
    m.median_time_to_session_s = _median(
        [_seconds(j.created_at, j.session_created_at) for j in jobs]
    )
    m.median_time_to_pr_s = _median([_seconds(j.session_created_at, j.pr_ready_at) for j in jobs])
    m.median_time_to_verdict_s = _median([_seconds(j.created_at, j.finished_at) for j in terminal])

    validations = session.scalars(select(ValidationRun)).all()
    m.validations_total = len(validations)
    m.validations_by_verdict = {v.value: 0 for v in Verdict}
    for v in validations:
        m.validations_by_verdict[v.verdict.value] += 1
    m.median_validation_duration_s = _median(
        [_seconds(v.started_at, v.finished_at) for v in validations]
    )
    m.webhook_deliveries_total = len(session.scalars(select(WebhookDelivery)).all())
    return m


def to_prometheus(m: Metrics) -> str:
    lines: list[str] = []

    def gauge(name: str, value: float | int | None, labels: dict[str, str] | None = None) -> None:
        if value is None:
            return
        label_str = ",".join(f'{k}="{v}"' for k, v in (labels or {}).items())
        lines.append(f"drp_{name}{{{label_str}}} {value}" if label_str else f"drp_{name} {value}")

    lines.append("# TYPE drp_findings_total gauge")
    gauge("findings_total", m.findings_total)
    for k, v in m.findings_by_baseline.items():
        gauge("findings", v, {"baseline": k})
    lines.append("# TYPE drp_jobs gauge")
    gauge("jobs_total", m.jobs_total)
    gauge("jobs_active", m.jobs_active)
    for k, v in m.jobs_by_state.items():
        gauge("jobs", v, {"state": k})
    for k, v in m.validations_by_verdict.items():
        gauge("validations", v, {"verdict": k})
    gauge("validations_total", m.validations_total)
    gauge("webhook_deliveries_total", m.webhook_deliveries_total)
    gauge("verified_rate", m.verified_rate)
    gauge("rejection_rate", m.rejection_rate)
    gauge("escalation_rate", m.escalation_rate)
    gauge("first_attempt_verified_rate", m.first_attempt_verified_rate)
    gauge("attempts_per_terminal_job", m.attempts_per_terminal_job)
    gauge("median_time_to_session_seconds", m.median_time_to_session_s)
    gauge("median_time_to_pr_seconds", m.median_time_to_pr_s)
    gauge("median_time_to_verdict_seconds", m.median_time_to_verdict_s)
    gauge("median_validation_duration_seconds", m.median_validation_duration_s)
    gauge("oldest_active_job_age_seconds", m.oldest_active_job_age_s)
    return "\n".join(lines) + "\n"
