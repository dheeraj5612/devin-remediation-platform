"""FastAPI application: webhook receiver, JSON API, dashboard and Prometheus metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from drp import metrics as metrics_mod
from drp.config import get_settings
from drp.db import session_scope
from drp.github.webhook import router as webhook_router
from drp.models import Finding, RemediationJob

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, default=str)

app = FastAPI(title="Devin Remediation Platform", version="0.1.0")
app.include_router(webhook_router)


def _job_dict(job: RemediationJob, *, deep: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": job.id,
        "finding_id": job.finding_id,
        "issue_number": job.issue_number,
        "state": job.state.value,
        "attempt": job.attempt,
        "devin_session_id": job.devin_session_id,
        "devin_session_url": job.devin_session_url,
        "pr_url": job.pr_url,
        "pr_head_sha": job.pr_head_sha,
        "outcome_reason": job.outcome_reason,
        "error_count": job.error_count,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "session_created_at": job.session_created_at,
        "pr_ready_at": job.pr_ready_at,
        "finished_at": job.finished_at,
        "next_run_at": job.next_run_at,
    }
    if deep:
        d["events"] = [
            {
                "at": e.created_at,
                "from": e.from_state,
                "to": e.to_state,
                "message": e.message,
                "data": e.data,
            }
            for e in job.events
        ]
        d["validations"] = [
            {
                "id": v.id,
                "attempt": v.attempt,
                "pr_head_sha": v.pr_head_sha,
                "mutant_sha256": v.mutant_sha256,
                "verdict": v.verdict.value,
                "scope_ok": v.scope_ok,
                "target_test_present": v.target_test_present,
                "clean_passed": v.clean_passed,
                "mutant_detected": v.mutant_detected,
                "reason": v.reason,
                "evidence": v.evidence,
                "artifacts_dir": v.artifacts_dir,
                "started_at": v.started_at,
                "finished_at": v.finished_at,
            }
            for v in job.validations
        ]
    return d


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "id": f.id,
        "title": f.title,
        "repo": f.repo,
        "source_sha": f.source_sha,
        "test_node_id": f.test_node_id,
        "weakness_kind": f.weakness_kind,
        "baseline_status": f.baseline_status.value,
        "baseline_evidence": f.baseline_evidence,
        "mutant_sha256": f.mutant_sha256,
        "github_issue_number": f.github_issue_number,
        "github_issue_url": f.github_issue_url,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/findings")
def api_findings() -> list[dict[str, Any]]:
    with session_scope() as s:
        return [_finding_dict(f) for f in s.scalars(select(Finding).order_by(Finding.id))]


@app.get("/api/jobs")
def api_jobs() -> list[dict[str, Any]]:
    with session_scope() as s:
        jobs = s.scalars(select(RemediationJob).order_by(RemediationJob.created_at.desc()))
        return [_job_dict(j) for j in jobs]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(RemediationJob, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return _job_dict(job, deep=True)


@app.get("/api/metrics")
def api_metrics() -> dict[str, object]:
    with session_scope() as s:
        return metrics_mod.compute(s).to_dict()


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    with session_scope() as s:
        return metrics_mod.to_prometheus(metrics_mod.compute(s))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    settings = get_settings()
    with session_scope() as s:
        findings = [_finding_dict(f) for f in s.scalars(select(Finding).order_by(Finding.id))]
        jobs = [
            _job_dict(j)
            for j in s.scalars(select(RemediationJob).order_by(RemediationJob.created_at.desc()))
        ]
        m = metrics_mod.compute(s).to_dict()
    return env.get_template("dashboard.html").render(
        findings=findings, jobs=jobs, metrics=m, settings=settings
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str) -> str:
    with session_scope() as s:
        job = s.get(RemediationJob, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        data = _job_dict(job, deep=True)
        finding = _finding_dict(job.finding)
    return env.get_template("job.html").render(job=data, finding=finding)


@app.get("/findings/{finding_id}", response_class=HTMLResponse)
def finding_page(finding_id: str) -> str:
    with session_scope() as s:
        f = s.get(Finding, finding_id)
        if f is None:
            raise HTTPException(404, "finding not found")
        data = _finding_dict(f)
        data["weakness_description"] = f.weakness_description
        data["protected_behavior"] = f.protected_behavior
        data["mutant_description"] = f.mutant_description
        data["test_command"] = f.test_command
        data["allowed_paths"] = f.allowed_paths
        jobs = [_job_dict(j) for j in f.jobs]
    return env.get_template("finding.html").render(finding=data, jobs=jobs)
