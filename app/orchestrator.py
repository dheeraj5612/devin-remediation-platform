"""`Orchestrator`: advances one job one step at a time, safely, forever-resumable.

ELI5: the worker calls `step(job_id)` over and over. Each call looks at the
job's status, does exactly one thing (launch Devin, poll it, validate the PR,
send the one correction), writes the result to the database, and returns.
Because every side effect is recorded *before* the expensive call that
follows it, a crash at any point leaves enough breadcrumbs to resume without
paying for a second Devin session.
"""

from contextlib import contextmanager
from typing import Any, Iterator

from app.cases import Registry
from app.config import Settings
from app.db import Store
from app.devin import AmbiguousSessionError, Devin, RemoteError, SessionState
from app.github import Candidate, GitHub
from app.models import Job, TERMINAL, now
from app.validator import Validator


class Orchestrator:
    """Advance durable jobs while keeping provider actions bounded and resumable."""

    def __init__(self, settings: Settings, store: Store, devin: Devin, github: GitHub, validator: Validator) -> None:
        """Wire the durable store and external adapters used by one resumable worker."""
        # ELI5: keep the policy, database, and provider handles together for every step.
        self.settings, self.store = settings, store
        # ELI5: remember the three outside services so one worker can coordinate them.
        self.devin, self.github, self.validator = devin, github, validator
        # ELI5: load the immutable case list once so webhooks and validation share policy.
        self.registry = Registry(settings)

    @contextmanager
    def _provider_trace(self, job_id: str) -> Iterator[None]:
        """Persist safe Devin operation summaries for one job without changing provider behavior."""
        # ELI5: real Devin and the simulation expose the same short-lived tracing hook.
        trace = getattr(self.devin, "operation_trace", None)
        if not callable(trace):
            # ELI5: old test doubles can still run; they simply have no provider-level trace.
            yield
            return

        def record(operation: str, outcome: str, details: dict[str, Any]) -> None:
            """Allow-list the callback payload before appending it to the job timeline."""
            # ELI5: only known operation names can become provider telemetry rows.
            allowed_operations = {"attachment_upload", "session_create", "session_poll",
                                  "list_reconcile", "correction_message"}
            if operation not in allowed_operations:
                return
            # ELI5: keep the outcome vocabulary small so UI consumers can count it safely.
            allowed_outcomes = {"SUCCEEDED", "FOUND", "NOT_FOUND", "AMBIGUOUS", "FAILED", "RETRYABLE_ERROR"}
            safe_outcome = outcome if outcome in allowed_outcomes else "FAILED"
            # ELI5: the event stores a stable key and mode; Event.timestamp is the canonical timestamp.
            safe_details: dict[str, Any] = {
                "operation": operation,
                "operation_key": operation,
                "outcome": safe_outcome,
                "mode": self.settings.mode,
            }
            # ELI5: copy only bounded, non-content facts supplied by the adapter.
            latency = details.get("latency_ms")
            if isinstance(latency, int) and latency >= 0:
                safe_details["latency_ms"] = latency
            session_id = details.get("session_id")
            if isinstance(session_id, str) and len(session_id) <= 120 and session_id.replace("-", "").replace("_", "").isalnum():
                safe_details["session_id"] = session_id
            if isinstance(details.get("attachment_url_present"), bool):
                safe_details["attachment_url_present"] = details["attachment_url_present"]
            # ELI5: append one immutable row without changing job state or idempotency flags.
            self.store.change(job_id, "DEVIN_API_OPERATION", details=safe_details)

        # ELI5: install the recorder only for this job's provider call, then restore the prior one.
        with trace(record):
            yield

    def resume(self) -> None:
        """Called once at worker start: note that we picked up unfinished jobs (nothing is recreated)."""
        # ELI5: inspect every saved job because a crash can leave work in any non-terminal state.
        for job in self.store.jobs():
            # ELI5: only jobs with a session need a resume breadcrumb; finished jobs stay untouched.
            if job.status not in TERMINAL and job.devin_session_id:
                # ELI5: record recovery before the next poll so the timeline proves the restart happened.
                self.store.change(job.id, "WORKER_RESUMED", details={"session_id": job.devin_session_id})

    def step(self, job_id: str) -> None:
        """Do the next thing for this job based on its status, then schedule the next look."""
        # ELI5: reload from durable storage so this decision uses the latest state after a restart.
        job = self.store.get(job_id)
        # ELI5: terminal jobs need no more provider calls, so repeated scheduling is harmless.
        if job.status in TERMINAL:
            # ELI5: a finished job needs no transition or provider call.
            return
        # ELI5: a stuck job eventually stops automatically and waits for a human decision.
        if job.started_at and (now() - job.started_at).total_seconds() > self.settings.job_timeout_seconds:
            # ELI5: persist the deadline result before returning so another worker cannot revive it.
            self.store.change(job.id, "DEADLINE_EXCEEDED", status="ESCALATED",
                              failure_reason="Job deadline exceeded; inspect or terminate the existing Devin session")
            # ELI5: an escalated deadline cannot be advanced by this tick.
            return
        # ELI5: provider errors are handled separately because retries must be bounded and idempotent.
        try:
            # ELI5: queue admission becomes a running job and starts the wall-clock deadline.
            if job.status == "QUEUED":
                # ELI5: record the queued-to-running transition before any launch decision.
                self.store.change(job.id, "DEVIN_RUNNING", status="DEVIN_RUNNING", started_at=now())
            # ELI5: a running job without a session still needs its one launch attempt.
            elif job.status == "DEVIN_RUNNING" and not job.devin_session_id:
                # ELI5: delegate the first provider creation through the idempotent launch gate.
                self.launch(job)
            # ELI5: a discovered pull request moves into the independent validation phase.
            elif job.status == "PR_OPENED":
                # ELI5: mark validation intent before evaluating the pinned candidate.
                self.store.change(job.id, "VALIDATING", status="VALIDATING")
            # ELI5: validation runs only after the job has explicitly entered that phase.
            elif job.status == "VALIDATING":
                # ELI5: run the independent validator only after entering VALIDATING.
                self.evaluate(job)
            # ELI5: a failed first verdict gets the single allowed correction message.
            elif job.status == "CORRECTING" and not job.correction_acknowledged:
                # ELI5: send the one correction only while it remains unacknowledged.
                self.correct(job)
            else:
                # ELI5: all remaining active states poll the same existing Devin session.
                self.poll(job)  # DEVIN_RUNNING with a session, or CORRECTING waiting for a new commit
        except RemoteError as exc:
            # Provider trouble. Three flavours, all bounded to 3 attempts:
            # ELI5: reload because another process may have recorded a provider attempt meanwhile.
            current = self.store.get(job.id)
            # ELI5: count this failure from durable state rather than an in-memory counter.
            failures = current.api_failures + 1
            # ELI5: duplicate session identity is a permanent reconciliation decision, never a retry.
            if isinstance(exc, AmbiguousSessionError):
                self.store.change(job.id, "RECONCILIATION_REQUIRED", status="ESCALATED", api_failures=failures,
                                  failure_reason="Multiple sessions match this job tag; manual reconciliation required")
                return
            # ELI5: an uncertain launch is reconciled by tag and never blindly posted twice.
            if not current.devin_session_id and current.launch_requested and failures <= 3:
                # We POSTed "create session" and lost the answer. Do NOT POST again; next step reconciles by tag.
                self.store.change(job.id, "LAUNCH_UNCERTAIN", api_failures=failures,
                                  failure_reason="Session creation uncertain; reconcile by job tag, never recreate")
            # ELI5: retry only provider failures that the adapter marked safe to repeat.
            elif exc.retryable and failures <= 3:
                # 429/5xx/network: back off exponentially (or as long as Retry-After says) and try again.
                # ELI5: record the retry before delaying so recovery knows why the job is paused.
                self.store.change(job.id, "PROVIDER_RETRY", api_failures=failures,
                                  details={"category": str(exc), "attempt": failures})
                # ELI5: wait longer after each failure, while respecting the provider's requested delay.
                self.store.defer(job.id, max(exc.retry_after, 2 ** failures))
                # ELI5: leave the job deferred so the next scheduled tick can retry.
                return
            else:
                # ELI5: after the retry budget ends, stop automated calls and mark the job failed.
                self.store.change(job.id, "PROVIDER_ERROR", status="FAILED", api_failures=failures,
                                  failure_reason=str(exc))
        except (ValueError, OSError, KeyError):
            # ELI5: configuration defects are not transient provider failures, so never spend retries on them.
            self.store.change(job.id, "CONFIGURATION_ERROR", status="FAILED",
                              failure_reason="Invalid configuration, baseline evidence, or context; run make doctor")
        # ELI5: schedule the next state-machine tick after every non-terminal action.
        self.store.defer(job.id, self.settings.poll_seconds)

    def launch(self, job: Job) -> None:
        """Create the Devin session exactly once.

        `launch_requested` is written *before* the POST. If we come back here with it already
        set, the previous attempt's reply was lost: look the session up by tag instead of
        creating another one; if it truly doesn't exist, escalate to a human.
        """
        # ELI5: a saved launch intent means a previous POST may have succeeded without returning its reply.
        if job.launch_requested:
            # ELI5: look up the same session tag before considering any new provider call.
            with self._provider_trace(job.id):
                session = self.devin.find_session(job.id)
            # ELI5: no matching session is unsafe to recreate automatically, so ask for inspection.
            if session is None:
                # ELI5: escalate when reconciliation cannot prove a session exists.
                self.store.change(job.id, "RECONCILIATION_REQUIRED", status="ESCALATED",
                                  failure_reason="No saved session after a launch intent; inspect Devin before retrying manually")
                # ELI5: no session can be safely attached after an ambiguous launch.
                return
        else:
            # ELI5: write the intent first so a crash cannot turn one job into two sessions.
            self.store.change(job.id, "LAUNCH_REQUESTED", launch_requested=True)
            # ELI5: create the session only after the durable idempotency marker exists.
            with self._provider_trace(job.id):
                session = self.devin.create_session(job, self.registry.cases[job.case_id])
        # ELI5: attach whichever original session was found or created to this durable job.
        self.store.change(job.id, "SESSION_ATTACHED", devin_session_id=session.session_id,
                          devin_session_url=session.url, provider_status=session.status,
                          api_failures=0, failure_reason=None)

    def candidate(self, job: Job, number: int) -> Candidate:
        """Fetch a PR using the case's branch when it has a source-specific baseline."""
        # ELI5: read the approved case branch so GitHub and validation enforce the same scope.
        branch = self.registry.cases[job.case_id].target_branch
        # ELI5: old test-quality cases retain the global branch while application cases use their pinned branch.
        return self.github.candidate(number, branch) if branch else self.github.candidate(number)

    def discover(self, job: Job, state: SessionState) -> Candidate | None:
        """Discover a PR while enforcing the same per-case branch used in validation."""
        # ELI5: discover only a PR on the approved case branch so validation sees the same scope.
        branch = self.registry.cases[job.case_id].target_branch
        # ELI5: return the allow-listed candidate shape while preserving legacy branch defaults.
        return self.github.discover(state, branch) if branch else self.github.discover(state)

    def poll(self, job: Job) -> None:
        """Ask Devin how the session is doing; move to PR_OPENED when a PR in our repo appears."""
        # ELI5: ask the provider for the current state of the already-attached session.
        with self._provider_trace(job.id):
            state = self.devin.get_session(job.devin_session_id)
        # ELI5: a different session identity could leak another job's work into this result.
        if state.session_id != job.devin_session_id:
            # ELI5: never let one session's status update another job.
            raise RemoteError("Session identity mismatch")
        # ELI5: combine provider fields into the status string shown in the audit timeline.
        provider = f"{state.status}/{state.status_detail or ''}"
        # ELI5: reset transient API failures after every successful provider response.
        if provider != job.provider_status or job.api_failures:
            # ELI5: save provider status and clear failures even when the status is unchanged.
            self.store.change(job.id, "PROVIDER_STATE", provider_status=provider, api_failures=0)
        # ELI5: discover only an allow-listed PR tied to this job's case branch.
        candidate = self.discover(job, state)
        # ELI5: accept a new PR or a new commit after correction; the same old SHA is not new evidence.
        if candidate and (job.status != "CORRECTING" or candidate.sha != job.candidate_sha):
            # New PR, or a new commit on the PR after our correction request.
            self.store.change(job.id, "PR_OPENED", status="PR_OPENED", candidate_pr_number=candidate.number,
                              candidate_pr_url=candidate.url, candidate_sha=candidate.sha,
                              pr_created_at=job.pr_created_at or now(), api_failures=0)
        # ELI5: after correction, wait when Devin still reports the old candidate.
        elif job.status == "CORRECTING" and candidate:
            return  # Same SHA after a correction: wait for a new candidate, bounded by the job deadline.
        # ELI5: a stopped or approval-blocked session without a PR needs human handling.
        elif state.status in {"error", "exit"} or state.status_detail in {"finished", "waiting_for_user", "waiting_for_approval"}:
            # ELI5: escalate a finished or blocked session with no candidate to inspect.
            self.store.change(job.id, "NO_CANDIDATE", status="ESCALATED",
                              failure_reason="Session stopped or needs human input without a candidate PR")
        # ELI5: inspect non-inactivity suspension as a possible budget or approval problem.
        elif state.status == "suspended" and state.status_detail not in {None, "inactivity"}:
            # ELI5: a non-inactivity suspension can be a budget or approval problem, not a retryable poll.
            self.store.change(job.id, "SESSION_SUSPENDED", status="ESCALATED",
                              failure_reason="Session suspended; inspect provider budget or approval state")

    def changed_head(self, job: Job, candidate: Candidate) -> None:
        """The PR got a new commit while we were looking. Re-validate the new SHA, but not forever."""
        # ELI5: count each changed head so a moving PR cannot consume validation forever.
        count = job.stale_count + 1
        # ELI5: after three changes, stop and ask a human to inspect the unstable PR.
        if count > 3:
            # ELI5: stop after repeated moving heads instead of validating an unstable PR.
            self.store.change(job.id, "HEAD_UNSTABLE", status="ESCALATED", validation_status="STALE_SHA",
                              validation=None, validated_sha=None, failure_reason="PR head changed repeatedly")
        else:
            # ELI5: record the new SHA as needing a fresh validation pass.
            self.store.change(job.id, "STALE_SHA", status="PR_OPENED", candidate_sha=candidate.sha,
                              stale_count=count, validation_status="STALE_SHA", validation=None, validated_sha=None)

    def evaluate(self, job: Job) -> None:
        """Run the independent validator on the pinned SHA and act on its verdict."""
        # ELI5: fetch the current PR head using the case's approved branch.
        candidate = self.candidate(job, job.candidate_pr_number)
        # ELI5: do not validate a head that changed after the previous poll.
        if candidate.sha != job.candidate_sha:
            # ELI5: send a new head back through the bounded stale-SHA path.
            self.changed_head(job, candidate)
            # ELI5: wait for the next validated head rather than using stale evidence.
            return
        # ELI5: ask the validator for an independent verdict on the exact candidate.
        result = self.validator.validate(job, self.registry.cases[job.case_id], candidate)
        # ELI5: persist the application status alongside normal/mutant so recovery can explain the verdict.
        self.store.change(job.id, "EVALUATED", validation_status=result.outcome, validation=result.to_dict(),
                          details={"outcome": result.outcome, "correction_count": job.correction_count,
                                   "sha": candidate.sha, "normal": result.normal, "mutant": result.mutant,
                                   "application": result.application})
        # ELI5: a validator-detected head change invalidates this attempt.
        if result.outcome == "STALE_SHA":
            # ELI5: a validator-reported stale head gets one more discovery pass before testing.
            self.changed_head(job, self.candidate(job, candidate.number))
        # ELI5: a verified result still needs the final exact-head check.
        elif result.outcome == "VERIFIED":
            # Re-check the head one last time so VERIFIED always names the commit that actually passed.
            current = self.candidate(job, candidate.number)
            # ELI5: the final head check prevents a later commit from borrowing an earlier verdict.
            if current.sha != candidate.sha:
                # ELI5: route the changed head back to bounded stale-SHA handling.
                self.changed_head(job, current)
                # ELI5: do not claim the old SHA passed after the PR moved.
                return
            # ELI5: persist the verified SHA only after all checks pass.
            self.store.change(job.id, "VERIFIED", status="VERIFIED", validated_sha=candidate.sha, failure_reason=None)
        # ELI5: repair failures receive the single correction budget.
        elif result.repair_failure and job.correction_count == 0:
            # The repair is wrong (test fails on good code, or still misses the regression): one retry.
            self.store.change(job.id, "CORRECTING", status="CORRECTING", correction_count=1)
        else:
            # ELI5: infrastructure failures are FAILED; assessed unresolved repairs are ESCALATED.
            status = "FAILED" if result.outcome == "INFRA_ERROR" else "ESCALATED"
            # ELI5: classify infrastructure as FAILED and unresolved repairs as ESCALATED.
            self.store.change(job.id, status, status=status, failure_reason=result.summary)

    def correct(self, job: Job) -> None:
        """Send the single correction message into the same session (same lost-reply guard as `launch`)."""
        # ELI5: a saved correction intent means the previous message may already have arrived.
        if job.correction_requested:
            # ELI5: escalate instead of resending when correction delivery is uncertain.
            self.store.change(job.id, "CORRECTION_UNCERTAIN", status="ESCALATED",
                              failure_reason="Correction acknowledgement missing; inspect the same session, do not resend automatically")
            # ELI5: no second correction is safe without human reconciliation.
            return
        # ELI5: record correction intent before sending the provider message.
        self.store.change(job.id, "CORRECTION_REQUESTED", correction_requested=True)
        # ELI5: tell Devin the exact verdict and remind it that the evaluator is trusted and off-limits.
        message = (
            f"Independent validation of {job.candidate_sha} returned {job.validation_status}. "
            "The trusted acceptance contract must pass on correct application behavior. "
            "Review the evidence attachment and update the same PR with a new commit, within the original scope. "
            "Do not modify the evaluator or disable tests. This is the only correction attempt."
        )
        # ELI5: send one correction through the existing session, never create a second session.
        with self._provider_trace(job.id):
            self.devin.send_correction(job.devin_session_id, message)
        # ELI5: acknowledge the send only after the provider call returns successfully.
        self.store.change(job.id, "CORRECTION_SENT", correction_acknowledged=True)
