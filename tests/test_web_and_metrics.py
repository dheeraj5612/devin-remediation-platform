from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from drp import metrics
from drp.config import Settings
from drp.jobs import create_job, transition
from drp.models import BaselineStatus, Finding, JobState, ValidationRun, Verdict
from drp.web.app import app


def _seed(finding: Finding, db_session: Session) -> str:
    finding.baseline_status = BaselineStatus.CONFIRMED
    finding.baseline_evidence = {
        "clean_original": {"passed": 1, "failed": 0},
        "mutant_original": {"passed": 1, "failed": 0},
        "target_status_clean": "passed",
        "target_status_mutant": "passed",
    }
    job, _ = create_job(db_session, finding, issue_number=1, delivery_id="d", triggered_by="u")
    for st in (
        JobState.SESSION_REQUESTED,
        JobState.SESSION_RUNNING,
        JobState.PR_READY,
        JobState.VALIDATING,
    ):
        transition(db_session, job, st)
    job.pr_url = "https://github.com/example/mathlib/pull/1"
    job.pr_number = 1
    job.pr_head_sha = "a" * 40
    db_session.add(
        ValidationRun(
            job_id=job.id,
            attempt=1,
            pr_head_sha="a" * 40,
            mutant_sha256=finding.mutant_sha256,
            verdict=Verdict.VERIFIED,
            scope_ok=True,
            target_test_present=True,
            clean_passed=True,
            mutant_detected=True,
            reason="ok",
            evidence={
                "clean_repaired": {"passed": 1, "failed": 0},
                "mutant_repaired": {"passed": 0, "failed": 1},
                "target_status_mutant": "failed",
                "changed_files": ["tests/test_normalize.py"],
            },
            artifacts_dir="/tmp/x",
        )
    )
    transition(db_session, job, JobState.VERIFIED, "verified")
    db_session.commit()
    return job.id


def test_metrics_and_prometheus(finding: Finding, db_session: Session) -> None:
    _seed(finding, db_session)
    m = metrics.compute(db_session)
    assert m.jobs_total == 1 and m.jobs_by_state["verified"] == 1
    assert m.verified_rate == 1.0 and m.rejection_rate == 0.0
    assert m.validations_by_verdict["verified"] == 1
    assert m.first_attempt_verified_rate == 1.0
    assert m.findings_by_baseline["confirmed"] == 1
    text = metrics.to_prometheus(m)
    assert 'drp_jobs{state="verified"} 1' in text
    assert "drp_verified_rate 1" in text


def test_dashboard_and_json_api(finding: Finding, db_session: Session, settings: Settings) -> None:
    job_id = _seed(finding, db_session)
    c = TestClient(app)
    assert c.get("/healthz").json()["status"] == "ok"

    jobs = c.get("/api/jobs").json()
    assert jobs[0]["id"] == job_id and jobs[0]["state"] == "verified"
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["validations"][0]["verdict"] == "verified"
    assert job["validations"][0]["mutant_detected"] is True
    assert [e["to"] for e in job["events"]][-1] == "verified"
    assert c.get("/api/jobs/nope").status_code == 404

    findings = c.get("/api/findings").json()
    assert findings[0]["id"] == finding.id and findings[0]["baseline_status"] == "confirmed"

    assert c.get("/api/metrics").json()["jobs_total"] == 1
    assert "drp_jobs_total 1" in c.get("/metrics").text

    html = c.get("/").text
    assert finding.id in html and "verified" in html
    page = c.get(f"/jobs/{job_id}").text
    assert "Independent validation" in page or "validation" in page.lower()
    assert "mutant" in page.lower() and "clean" in page.lower()
    assert finding.test_node_id in c.get(f"/findings/{finding.id}").text
