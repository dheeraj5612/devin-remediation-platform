from app.cases import baseline_evidence, Case
from app.config import Settings
from app.db import Store
from app.devin import ProviderError
from app.github import ScopeError
from app.models import Job, now, TERMINAL
from app.validator import Verdict

MAX_CORRECTIONS = 1


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, cases: dict[str, Case], devin, github, validator):
        self.settings, self.store, self.cases = settings, store, cases
        self.devin, self.github, self.validator = devin, github, validator

    def fail(self, job: Job, reason: str, status: str = "FAILED") -> None:
        self.store.change(job.id, status, status=status, failure_reason=reason, details={"reason": reason})

    def tick(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job.status in TERMINAL or job.next_poll_at > now():
            return
        if now() - job.created_at > self.settings.job_timeout_seconds:
            self.fail(job, "Job deadline exceeded; inspect the existing session before retrying",
                      "ESCALATED" if job.correction_count else "FAILED")
            return
        case = self.cases[job.case_id]
        try:
            if job.status == "QUEUED":
                evidence = baseline_evidence(case, self.settings.data_dir) if job.mode == "LIVE" else {"simulation": True}
                # Persist before the non-idempotent POST. An unknown outcome is reconciled, never blindly retried.
                job = self.store.change(job.id, "LAUNCH_INTENT", status="DEVIN_RUNNING", launch_intent=True,
                                        evidence={"before_normal": "PASS", "before_mutant": "PASS · ESCAPED"})
                session = self.devin.create(job, case, evidence)
                self.store.change(job.id, "SESSION_ATTACHED", devin_session_id=session.session_id,
                                  devin_session_url=session.url, transient_failures=0)
                return
            if not job.devin_session_id:
                session = self.devin.find(job)
                if session is None:
                    raise ProviderError("Launch outcome unresolved; no matching session is visible", retryable=True)
                self.store.change(job.id, "SESSION_RECOVERED", devin_session_id=session.session_id,
                                  devin_session_url=session.url, transient_failures=0)
                return
            if job.status == "CORRECTING" and not job.correction_sent:
                self.fail(job, "Correction delivery outcome unknown; manual reconciliation required", "ESCALATED")
                return
            session = self.devin.get(job.devin_session_id)
            if session.failed:
                self.fail(job, "Devin session failed or reached its usage limit")
                return
            if not session.finished:
                self.store.change(job.id, "PROVIDER_WAIT", next_poll_at=now() + self.settings.poll_seconds,
                                  details={"status": session.status, "detail": session.status_detail})
                return
            candidate = self.github.discover(job, session, case)
            if candidate is None or (job.status == "CORRECTING" and candidate.sha == job.candidate_sha):
                self.store.change(job.id, "AWAITING_NEW_CANDIDATE", next_poll_at=now() + self.settings.poll_seconds)
                return
            if job.status in {"DEVIN_RUNNING", "CORRECTING"}:
                job = self.store.change(job.id, "PR_OPENED", status="PR_OPENED", candidate_pr_number=candidate.number,
                                        candidate_pr_url=candidate.url, candidate_sha=candidate.sha)
            job = self.store.change(job.id, "VALIDATION_STARTED", status="VALIDATING", candidate_sha=candidate.sha,
                                    details={"sha": candidate.sha})
            verdict = self.validator.evaluate(case, candidate)
            if not self.github.unchanged(candidate):
                verdict = Verdict("INFRA", "PR head changed during validation; no verdict applies to its current head")
            job = self.store.change(job.id, "VALIDATION_RESULT", validation_status=verdict.outcome,
                                    evidence={**job.evidence, **verdict.evidence}, transient_failures=0,
                                    details={"outcome": verdict.outcome, "reason": verdict.reason, "sha": candidate.sha})
            if verdict.outcome == "VERIFIED":
                self.store.change(job.id, "VERIFIED", status="VERIFIED", failure_reason=None)
            elif verdict.outcome == "INFRA":
                self.fail(job, verdict.reason)
            elif job.correction_count == MAX_CORRECTIONS:
                self.fail(job, verdict.reason, "ESCALATED")
            else:
                self.store.change(job.id, "CORRECTION_INTENT", status="CORRECTING", correction_count=1,
                                  correction_sent=False, failure_reason=verdict.reason)
                self.devin.correct(job.devin_session_id, verdict.reason)
                self.store.change(job.id, "CORRECTION_SENT", correction_sent=True,
                                  next_poll_at=now() + self.settings.poll_seconds)
        except ProviderError as error:
            job = self.store.get(job_id)
            failures = job.transient_failures + 1
            if error.retryable and failures <= 3:
                self.store.change(job.id, "PROVIDER_RETRY", transient_failures=failures,
                                  next_poll_at=now() + max(self.settings.poll_seconds, 2 ** failures),
                                  details={"attempt": failures, "reason": str(error)})
            else:
                self.fail(job, str(error))
        except (ScopeError, ValueError, OSError) as error:
            job = self.store.get(job_id)
            reason = str(error) if isinstance(error, ScopeError) else "Invalid local configuration or missing baseline/context evidence"
            self.fail(job, reason)
