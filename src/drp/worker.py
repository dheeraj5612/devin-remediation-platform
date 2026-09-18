"""Stage 7-11: the durable worker that drives a remediation job through its state machine.

Every step persists its result before moving on, and every state is re-drivable, so a crash at
any point is recovered by simply re-claiming the job:

    QUEUED -> SESSION_REQUESTED -> SESSION_RUNNING -> PR_READY -> VALIDATING
                                        ^                              |
                                        +--- feedback (attempt < max) --+
                                                                       v
                                            VERIFIED | REJECTED | ESCALATED
"""

from __future__ import annotations

import logging
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from drp.config import Settings, get_settings
from drp.db import session_scope
from drp.devin.client import DevinClient, DevinClientProtocol, DevinSession, FakeDevinClient
from drp.devin.prompt import STRUCTURED_OUTPUT_SCHEMA, build_feedback, build_prompt, job_tag
from drp.github.client import GitHubClient, PullRequest, parse_pr_url
from drp.jobs import claim_next, note, record_error, transition
from drp.models import Finding, JobState, RemediationJob, ValidationRun, Verdict
from drp.validator.gitops import GitRepo
from drp.validator.validate import ValidationOutcome, validate_candidate

log = logging.getLogger("drp.worker")


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        devin: DevinClientProtocol | None = None,
        github: GitHubClient | None = None,
        *,
        owner: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.devin = devin or self._default_devin()
        self.github = github or GitHubClient(self.settings)
        self.owner = owner or f"{socket.gethostname()}-{id(self)}"

    def _default_devin(self) -> DevinClientProtocol:
        if self.settings.devin_mode == "fake":
            return FakeDevinClient()
        return DevinClient(self.settings)

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        log.info("worker %s started", self.owner)
        while True:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_interval_s)

    def run_once(self) -> bool:
        """Claim and advance one job. Returns False when there was nothing to do."""
        with session_scope() as session:
            job = claim_next(session, self.owner, self.settings.lease_seconds)
            if job is None:
                return False
            job_id = job.id
            state = job.state
        try:
            with session_scope() as session:
                job = session.get(RemediationJob, job_id)
                assert job is not None
                self.step(session, job)
        except Exception as exc:  # noqa: BLE001 - any failure is recorded on the job
            log.exception("job %s failed in state %s", job_id, state.value)
            with session_scope() as session:
                job = session.get(RemediationJob, job_id)
                assert job is not None
                record_error(
                    session,
                    job,
                    f"{type(exc).__name__}: {exc}",
                    max_errors=5,
                    backoff_s=self.settings.worker_poll_interval_s * 2,
                )
        return True

    def step(self, session: Session, job: RemediationJob) -> None:
        handler = {
            JobState.QUEUED: self._start_session,
            JobState.SESSION_REQUESTED: self._start_session,
            JobState.SESSION_RUNNING: self._poll_session,
            JobState.PR_READY: self._validate,
            JobState.VALIDATING: self._validate,  # lease expired mid-validation: redo it
        }.get(job.state)
        if handler is None:
            return
        handler(session, job)

    # ----------------------------------------------------------------- steps
    def _start_session(self, session: Session, job: RemediationJob) -> None:
        finding = job.finding
        tag = job_tag(job.id)
        if job.state == JobState.QUEUED:
            transition(session, job, JobState.SESSION_REQUESTED, "requesting Devin session")
            session.commit()
        existing = self.devin.find_session_by_tag(tag)
        if existing is not None:
            devin_session = existing
            note(session, job, f"reusing existing Devin session {existing.session_id} (idempotent)")
        else:
            devin_session = self.devin.create_session(
                prompt=build_prompt(finding, job),
                title=f"[DRP] repair weak test {finding.test_node_id.split('::', 1)[1]}",
                tags=[tag, f"drp-finding-{finding.id}", "devin-remediation-platform"],
                repos=[finding.repo],
                structured_output_schema=STRUCTURED_OUTPUT_SCHEMA,
                max_acu_limit=self.settings.devin_max_acu,
            )
        job.devin_session_id = devin_session.session_id
        job.devin_session_url = devin_session.url
        transition(
            session,
            job,
            JobState.SESSION_RUNNING,
            f"Devin session {devin_session.session_id} running",
            {"session_url": devin_session.url},
            delay_s=self.settings.devin_poll_interval_s,
        )
        self._comment(
            finding,
            job,
            f"Remediation job `{job.id}` started. Devin session: {devin_session.url}\n\n"
            f"The candidate PR will be validated independently against the clean revision and "
            f"the pre-registered controlled regression before any result is trusted.",
        )

    def _poll_session(self, session: Session, job: RemediationJob) -> None:
        assert job.devin_session_id
        finding = job.finding
        ds = self.devin.get_session(job.devin_session_id)
        note(
            session,
            job,
            f"poll: status={ds.status} detail={ds.status_detail} prs={len(ds.pull_requests)}",
        )
        pr = self._candidate_pr(ds, finding, job)
        if pr is not None:
            already_validated = any(v.pr_head_sha == pr.head_sha for v in job.validations)
            if not already_validated:
                job.pr_url, job.pr_number, job.pr_head_sha = pr.url, pr.number, pr.head_sha
                transition(
                    session,
                    job,
                    JobState.PR_READY,
                    f"candidate PR {pr.url} at {pr.head_sha[:12]}",
                    {"pr": pr.__dict__},
                )
                return
        if ds.is_finished:
            if pr is None:
                so = ds.structured_output or {}
                transition(
                    session,
                    job,
                    JobState.ESCALATED,
                    "Devin session finished without a candidate PR: "
                    f"{so.get('status')} {so.get('blocked_reason') or so.get('summary') or ''}",
                    {"structured_output": so},
                )
            else:
                transition(
                    session,
                    job,
                    JobState.ESCALATED,
                    "Devin session finished without pushing new commits after feedback",
                )
            return
        if ds.is_error:
            transition(session, job, JobState.ESCALATED, "Devin session errored")
            return
        if ds.is_blocked:
            transition(
                session,
                job,
                JobState.ESCALATED,
                f"Devin session is blocked ({ds.status}/{ds.status_detail}); needs a human",
                {"session_url": ds.url},
            )
            return
        started = job.session_created_at or job.created_at
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if elapsed > self.settings.devin_session_timeout_s:
            transition(
                session, job, JobState.ESCALATED, f"Devin session timed out after {elapsed:.0f}s"
            )
            return
        job.next_run_at = datetime.now(UTC) + timedelta(seconds=self.settings.devin_poll_interval_s)
        job.lease_until = None
        job.lease_owner = None

    def _candidate_pr(
        self, ds: DevinSession, finding: Finding, job: RemediationJob
    ) -> PullRequest | None:
        for url in ds.pr_urls:
            try:
                repo, number = parse_pr_url(url)
            except ValueError:
                continue
            if repo != finding.repo:
                continue
            pr = self.github.get_pull(finding.repo, number)
            if pr.base_repo != finding.repo or pr.base_ref != self.settings.target_default_branch:
                continue
            if pr.state != "open":
                continue
            return pr
        return None

    def _validate(self, session: Session, job: RemediationJob) -> None:
        finding = job.finding
        assert job.pr_number and job.pr_head_sha
        if job.state == JobState.PR_READY:
            transition(session, job, JobState.VALIDATING, f"validating {job.pr_head_sha[:12]}")
            session.commit()
        repo = GitRepo(self.settings.target_checkout)
        repo.fetch_pr_head("origin", job.pr_number)
        repo.fetch_commit("origin", finding.source_sha)
        outcome = validate_candidate(
            finding=finding,
            head_sha=job.pr_head_sha,
            settings=self.settings,
            artifacts_root=self.settings.artifacts_dir / "validations" / job.id,
            base_sha=f"origin/{self.settings.target_default_branch}",
        )
        run = ValidationRun(
            job_id=job.id,
            attempt=job.attempt,
            pr_head_sha=outcome.pr_head_sha,
            mutant_sha256=outcome.mutant_sha256,
            verdict=outcome.verdict,
            scope_ok=outcome.scope_ok,
            target_test_present=outcome.target_test_present,
            clean_passed=outcome.clean_passed,
            mutant_detected=outcome.mutant_detected,
            reason=outcome.reason,
            evidence=outcome.evidence,
            artifacts_dir=outcome.artifacts_dir,
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        self._apply_verdict(session, job, outcome)

    def _apply_verdict(
        self, session: Session, job: RemediationJob, outcome: ValidationOutcome
    ) -> None:
        finding = job.finding
        summary = render_validation_comment(job, outcome)
        if outcome.verdict == Verdict.VERIFIED:
            transition(session, job, JobState.VERIFIED, outcome.reason, outcome.to_dict())
            self._comment(finding, job, summary)
            if job.pr_number:
                self._comment_pr(finding, job.pr_number, summary)
            return
        if outcome.verdict == Verdict.INFRA_ERROR:
            record_error(
                session,
                job,
                outcome.reason,
                max_errors=3,
                backoff_s=self.settings.worker_poll_interval_s * 4,
            )
            if not job.state.terminal:
                transition(
                    session,
                    job,
                    JobState.PR_READY,
                    f"retry validation: {outcome.reason}",
                    delay_s=30,
                )
            return
        if (
            outcome.verdict == Verdict.REJECTED
            and job.attempt < self.settings.max_remediation_attempts
        ):
            assert job.devin_session_id
            feedback = build_feedback(outcome.reason, outcome.evidence)
            self.devin.send_message(job.devin_session_id, feedback)
            job.attempt += 1
            transition(
                session,
                job,
                JobState.SESSION_RUNNING,
                f"rejected: {outcome.reason}; feedback sent, attempt {job.attempt}",
                outcome.to_dict(),
                delay_s=self.settings.devin_poll_interval_s,
            )
            self._comment(finding, job, summary)
            return
        final = JobState.REJECTED if outcome.verdict == Verdict.REJECTED else JobState.ESCALATED
        transition(session, job, final, outcome.reason, outcome.to_dict())
        self._comment(finding, job, summary)
        if job.pr_number:
            self._comment_pr(finding, job.pr_number, summary)

    # -------------------------------------------------------------- helpers
    def _comment(self, finding: Finding, job: RemediationJob, body: str) -> None:
        try:
            self.github.comment(finding.repo, job.issue_number, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not comment on issue #%s: %s", job.issue_number, exc)

    def _comment_pr(self, finding: Finding, pr_number: int, body: str) -> None:
        try:
            self.github.comment(finding.repo, pr_number, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not comment on PR #%s: %s", pr_number, exc)


def _fmt_run(run: dict[str, Any] | None) -> str:
    if not run:
        return "not run"
    return (
        f"exit `{run['exit_code']}` - {run['passed']} passed, {run['failed']} failed, "
        f"{run['errors']} errors, {run['skipped']} skipped"
    )


def render_validation_comment(job: RemediationJob, outcome: ValidationOutcome) -> str:
    ev = outcome.evidence
    icon = {
        Verdict.VERIFIED: "VERIFIED",
        Verdict.REJECTED: "REJECTED",
        Verdict.ESCALATED: "ESCALATED",
        Verdict.INFRA_ERROR: "INFRA ERROR",
    }[outcome.verdict]
    return f"""### Independent validation - **{icon}** (job `{job.id}`, attempt {job.attempt})

{outcome.reason}

| check | result |
| --- | --- |
| PR head | `{outcome.pr_head_sha}` |
| scope limited to allowed test paths | {"yes" if outcome.scope_ok else "NO"} |
| target test present & not skipped | {"yes" if outcome.target_test_present else "NO"} |
| repaired file on clean code | {_fmt_run(ev.get("clean_repaired"))} |
| repaired file with pre-registered regression (`{outcome.mutant_sha256[:12]}`) | {_fmt_run(ev.get("mutant_repaired"))} |
| target test status with regression applied | `{ev.get("target_status_mutant")}` |

Changed files: {", ".join(f"`{p}`" for p in ev.get("changed_files", [])) or "-"}
Artifacts (junit + logs): `{outcome.artifacts_dir}`
"""  # noqa: E501


def validate_ref_offline(finding: Finding, ref: str, settings: Settings) -> ValidationOutcome:
    """Run the validator against an arbitrary git ref (used by the CLI for dry runs)."""
    repo = GitRepo(settings.target_checkout)
    sha = repo.rev_parse(ref)
    return validate_candidate(
        finding=finding,
        head_sha=sha,
        settings=settings,
        artifacts_root=settings.artifacts_dir / "validations" / f"manual-{sha[:12]}",
    )
