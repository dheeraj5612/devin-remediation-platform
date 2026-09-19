"""`Orchestrator`: advances one job one step at a time, safely, forever-resumable.

ELI5: the worker calls `step(job_id)` over and over. Each call looks at the
job's status, does exactly one thing (launch Devin, poll it, validate the PR,
send the one correction), writes the result to the database, and returns.
Because every side effect is recorded *before* the expensive call that
follows it, a crash at any point leaves enough breadcrumbs to resume without
paying for a second Devin session.
"""

from app.cases import Registry
from app.config import Settings
from app.db import Store
from app.devin import Devin, RemoteError
from app.github import Candidate, GitHub
from app.models import Job, TERMINAL, now
from app.validator import Validator


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, devin: Devin, github: GitHub, validator: Validator) -> None:
        self.settings, self.store = settings, store
        self.devin, self.github, self.validator = devin, github, validator
        self.registry = Registry(settings)

    def resume(self) -> None:
        """Called once at worker start: note that we picked up unfinished jobs (nothing is recreated)."""
        for job in self.store.jobs():
            if job.status not in TERMINAL and job.devin_session_id:
                self.store.change(job.id, "WORKER_RESUMED", details={"session_id": job.devin_session_id})

    def step(self, job_id: str) -> None:
        """Do the next thing for this job based on its status, then schedule the next look."""
        job = self.store.get(job_id)
        if job.status in TERMINAL:
            return
        if job.started_at and (now() - job.started_at).total_seconds() > self.settings.job_timeout_seconds:
            self.store.change(job.id, "DEADLINE_EXCEEDED", status="ESCALATED",
                              failure_reason="Job deadline exceeded; inspect or terminate the existing Devin session")
            return
        try:
            if job.status == "QUEUED":
                self.store.change(job.id, "DEVIN_RUNNING", status="DEVIN_RUNNING", started_at=now())
            elif job.status == "DEVIN_RUNNING" and not job.devin_session_id:
                self.launch(job)
            elif job.status == "PR_OPENED":
                self.store.change(job.id, "VALIDATING", status="VALIDATING")
            elif job.status == "VALIDATING":
                self.evaluate(job)
            elif job.status == "CORRECTING" and not job.correction_acknowledged:
                self.correct(job)
            else:
                self.poll(job)  # DEVIN_RUNNING with a session, or CORRECTING waiting for a new commit
        except RemoteError as exc:
            # Provider trouble. Three flavours, all bounded to 3 attempts:
            current = self.store.get(job.id)
            failures = current.api_failures + 1
            if not current.devin_session_id and current.launch_requested and failures <= 3:
                # We POSTed "create session" and lost the answer. Do NOT POST again; next step reconciles by tag.
                self.store.change(job.id, "LAUNCH_UNCERTAIN", api_failures=failures,
                                  failure_reason="Session creation uncertain; reconcile by job tag, never recreate")
            elif exc.retryable and failures <= 3:
                # 429/5xx/network: back off exponentially (or as long as Retry-After says) and try again.
                self.store.change(job.id, "PROVIDER_RETRY", api_failures=failures,
                                  details={"category": str(exc), "attempt": failures})
                self.store.defer(job.id, max(exc.retry_after, 2 ** failures))
                return
            else:
                self.store.change(job.id, "PROVIDER_ERROR", status="FAILED", api_failures=failures,
                                  failure_reason=str(exc))
        except (ValueError, OSError, KeyError):
            # Our own misconfiguration (bad baseline, missing context, illegal transition): stop, don't retry.
            self.store.change(job.id, "CONFIGURATION_ERROR", status="FAILED",
                              failure_reason="Invalid configuration, baseline evidence, or context; run make doctor")
        self.store.defer(job.id, self.settings.poll_seconds)

    def launch(self, job: Job) -> None:
        """Create the Devin session exactly once.

        `launch_requested` is written *before* the POST. If we come back here with it already
        set, the previous attempt's reply was lost: look the session up by tag instead of
        creating another one; if it truly doesn't exist, escalate to a human.
        """
        if job.launch_requested:
            session = self.devin.find_session(job.id)
            if session is None:
                self.store.change(job.id, "RECONCILIATION_REQUIRED", status="ESCALATED",
                                  failure_reason="No saved session after a launch intent; inspect Devin before retrying manually")
                return
        else:
            self.store.change(job.id, "LAUNCH_REQUESTED", launch_requested=True)
            session = self.devin.create_session(job, self.registry.cases[job.case_id])
        self.store.change(job.id, "SESSION_ATTACHED", devin_session_id=session.session_id,
                          devin_session_url=session.url, provider_status=session.status,
                          api_failures=0, failure_reason=None)

    def poll(self, job: Job) -> None:
        """Ask Devin how the session is doing; move to PR_OPENED when a PR in our repo appears."""
        state = self.devin.get_session(job.devin_session_id)
        if state.session_id != job.devin_session_id:
            raise RemoteError("Session identity mismatch")
        provider = f"{state.status}/{state.status_detail or ''}"
        if provider != job.provider_status:
            self.store.change(job.id, "PROVIDER_STATE", provider_status=provider, api_failures=0)
        candidate = self.github.discover(state)
        if candidate and (job.status != "CORRECTING" or candidate.sha != job.candidate_sha):
            # New PR, or a new commit on the PR after our correction request.
            self.store.change(job.id, "PR_OPENED", status="PR_OPENED", candidate_pr_number=candidate.number,
                              candidate_pr_url=candidate.url, candidate_sha=candidate.sha,
                              pr_created_at=job.pr_created_at or now(), api_failures=0)
        elif job.status == "CORRECTING" and candidate:
            return  # Same SHA after a correction: wait for a new candidate, bounded by the job deadline.
        elif state.status in {"error", "exit"} or state.status_detail in {"finished", "waiting_for_user", "waiting_for_approval"}:
            self.store.change(job.id, "NO_CANDIDATE", status="ESCALATED",
                              failure_reason="Session stopped or needs human input without a candidate PR")
        elif state.status == "suspended" and state.status_detail not in {None, "inactivity"}:
            self.store.change(job.id, "SESSION_SUSPENDED", status="ESCALATED",
                              failure_reason="Session suspended; inspect provider budget or approval state")

    def changed_head(self, job: Job, candidate: Candidate) -> None:
        """The PR got a new commit while we were looking. Re-validate the new SHA, but not forever."""
        count = job.stale_count + 1
        if count > 3:
            self.store.change(job.id, "HEAD_UNSTABLE", status="ESCALATED", failure_reason="PR head changed repeatedly")
        else:
            self.store.change(job.id, "STALE_SHA", status="PR_OPENED", candidate_sha=candidate.sha,
                              stale_count=count, validation_status="STALE_SHA")

    def evaluate(self, job: Job) -> None:
        """Run the independent validator on the pinned SHA and act on its verdict."""
        candidate = self.github.candidate(job.candidate_pr_number)
        if candidate.sha != job.candidate_sha:
            self.changed_head(job, candidate)
            return
        result = self.validator.validate(job, self.registry.cases[job.case_id], candidate)
        self.store.change(job.id, "EVALUATED", validation_status=result.outcome, validation=result.to_dict(),
                          details={"outcome": result.outcome, "correction_count": job.correction_count,
                                   "sha": candidate.sha, "normal": result.normal, "mutant": result.mutant})
        if result.outcome == "STALE_SHA":
            self.changed_head(job, self.github.candidate(candidate.number))
        elif result.outcome == "VERIFIED":
            # Re-check the head one last time so VERIFIED always names the commit that actually passed.
            current = self.github.candidate(candidate.number)
            if current.sha != candidate.sha:
                self.changed_head(job, current)
                return
            self.store.change(job.id, "VERIFIED", status="VERIFIED", validated_sha=candidate.sha, failure_reason=None)
        elif result.repair_failure and job.correction_count == 0:
            # The repair is wrong (test fails on good code, or still misses the regression): one retry.
            self.store.change(job.id, "CORRECTING", status="CORRECTING", correction_count=1)
        else:
            status = "FAILED" if result.outcome == "INFRA_ERROR" else "ESCALATED"
            self.store.change(job.id, status, status=status, failure_reason=result.summary)

    def correct(self, job: Job) -> None:
        """Send the single correction message into the same session (same lost-reply guard as `launch`)."""
        if job.correction_requested:
            self.store.change(job.id, "CORRECTION_UNCERTAIN", status="ESCALATED",
                              failure_reason="Correction acknowledgement missing; inspect the same session, do not resend automatically")
            return
        self.store.change(job.id, "CORRECTION_REQUESTED", correction_requested=True)
        message = (
            f"Independent validation of {job.candidate_sha} returned {job.validation_status}. "
            "The designated test must pass on correct behavior and detect the registered regression. "
            "Review the evidence attachment and update the same PR with a new commit, within the original scope. "
            "Do not modify the evaluator or disable tests. This is the only correction attempt."
        )
        self.devin.send_correction(job.devin_session_id, message)
        self.store.change(job.id, "CORRECTION_SENT", correction_acknowledged=True)
