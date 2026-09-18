"""Independent deterministic validation of a candidate PR.

The validator never reads Devin's claims. Given a PR head sha it proves, in a fresh worktree:

1. scope        - the PR only touches ``allowed_paths`` (test files); production code is
                  untouched so the pre-registered mutant is still meaningful.
2. clean pass   - the repaired test file passes on the PR head and the target test node is
                  still present (not deleted, renamed or skipped).
3. mutant fail  - the *exact* pre-registered mutant (sha256 pinned) applies on the PR head
                  and the repaired test file now fails, with the target test among the failures
                  (or, at minimum, some test in the file). That is the evidence that the
                  regression which escaped the original test is now detected.

Anything that prevents a clean yes/no (mutant no longer applies, scope violation, import
provenance broken, timeouts) is escalated rather than guessed.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from drp.config import Settings
from drp.models import Finding, Verdict
from drp.validator.gitops import GitError, GitRepo
from drp.validator.runner import environment_fingerprint, import_provenance, run_tests


@dataclass
class ValidationOutcome:
    verdict: Verdict
    reason: str
    pr_head_sha: str
    mutant_sha256: str
    scope_ok: bool = False
    target_test_present: bool = False
    clean_passed: bool | None = None
    mutant_detected: bool | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    artifacts_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "pr_head_sha": self.pr_head_sha,
            "mutant_sha256": self.mutant_sha256,
            "scope_ok": self.scope_ok,
            "target_test_present": self.target_test_present,
            "clean_passed": self.clean_passed,
            "mutant_detected": self.mutant_detected,
            "artifacts_dir": self.artifacts_dir,
            "evidence": self.evidence,
        }


def _path_allowed(path: str, allowed: list[str]) -> bool:
    return any(path == a or (a.endswith("/") and path.startswith(a)) for a in allowed)


def validate_candidate(
    *,
    finding: Finding,
    head_sha: str,
    settings: Settings,
    artifacts_root: Path,
    base_sha: str | None = None,
) -> ValidationOutcome:
    repo = GitRepo(settings.target_checkout)
    artifacts = artifacts_root / time.strftime("%Y%m%dT%H%M%SZ")
    artifacts.mkdir(parents=True, exist_ok=True)
    mutant = Path(finding.mutant_path)
    actual_digest = hashlib.sha256(mutant.read_bytes()).hexdigest()
    out = ValidationOutcome(
        verdict=Verdict.INFRA_ERROR,
        reason="",
        pr_head_sha=head_sha,
        mutant_sha256=finding.mutant_sha256,
        artifacts_dir=str(artifacts),
    )
    out.evidence["environment"] = environment_fingerprint(settings.target_python)

    if actual_digest != finding.mutant_sha256:
        out.verdict = Verdict.ESCALATED
        out.reason = "mutant patch on disk does not match the registered sha256"
        out.evidence["mutant_sha256_on_disk"] = actual_digest
        return out

    base = base_sha or finding.source_sha
    try:
        merge_base = repo.merge_base(base, head_sha)
        changed = repo.changed_files(merge_base, head_sha)
        out.evidence["merge_base"] = merge_base
        out.evidence["changed_files"] = changed
        out.evidence["diff_stat"] = repo.diff_stat(merge_base, head_sha)
    except GitError as exc:
        out.reason = f"git failure while inspecting PR: {exc}"
        return out

    if not changed:
        out.verdict = Verdict.REJECTED
        out.reason = "candidate PR contains no changes relative to its base"
        return out
    disallowed = [p for p in changed if not _path_allowed(p, finding.allowed_paths)]
    out.scope_ok = not disallowed
    if disallowed:
        out.verdict = Verdict.ESCALATED
        out.reason = f"PR modifies files outside the allowed test scope: {disallowed}"
        return out
    if finding.test_file not in changed:
        out.verdict = Verdict.REJECTED
        out.reason = f"PR does not modify the weak test file {finding.test_file}"
        return out

    with repo.worktree(head_sha, settings.artifacts_dir / "worktrees") as wt:
        prov = import_provenance(
            worktree=wt, python=settings.target_python, package=settings.target_package
        )
        out.evidence["provenance"] = prov
        if not prov["resolved_from_worktree"]:
            out.reason = "package not imported from isolated worktree"
            return out

        clean = run_tests(
            worktree=wt,
            command_template=finding.test_command,
            python=settings.target_python,
            test_file=finding.test_file,
            artifacts_dir=artifacts,
            label="clean_repaired",
            timeout_s=settings.test_timeout_s,
        )
        out.evidence["clean_repaired"] = clean.to_dict()
        if clean.timed_out:
            out.reason = "repaired test timed out on clean code"
            return out
        target_clean = clean.status_of(finding.test_node_id)
        out.target_test_present = target_clean is not None
        out.evidence["target_status_clean"] = target_clean
        if not out.target_test_present:
            out.verdict = Verdict.REJECTED
            out.reason = f"target test {finding.test_node_id} is missing after the repair"
            return out
        if target_clean == "skipped":
            out.verdict = Verdict.REJECTED
            out.reason = "target test is skipped on clean code"
            return out
        out.clean_passed = clean.all_passed
        if not out.clean_passed:
            out.verdict = Verdict.REJECTED
            out.reason = (
                f"repaired test file fails on clean code "
                f"(exit {clean.exit_code}, failed={clean.failed}, errors={clean.errors})"
            )
            return out

        applies, apply_log = repo.patch_applies(wt, mutant)
        out.evidence["mutant_apply_check"] = apply_log
        if not applies:
            out.verdict = Verdict.ESCALATED
            out.reason = "pre-registered mutant no longer applies on the PR head"
            return out
        repo.apply_patch(wt, mutant)
        out.evidence["mutant_diff_stat"] = repo.run("diff", "--stat", cwd=wt)

        mutated = run_tests(
            worktree=wt,
            command_template=finding.test_command,
            python=settings.target_python,
            test_file=finding.test_file,
            artifacts_dir=artifacts,
            label="mutant_repaired",
            timeout_s=settings.test_timeout_s,
        )
        out.evidence["mutant_repaired"] = mutated.to_dict()
        if mutated.timed_out:
            out.reason = "repaired test timed out with mutant applied"
            return out
        target_mutant = mutated.status_of(finding.test_node_id)
        out.evidence["target_status_mutant"] = target_mutant
        failing = sorted(k for k, v in mutated.cases.items() if v in ("failed", "error"))
        out.evidence["failing_with_mutant"] = failing

    if mutated.total == 0:
        out.reason = "no tests collected with mutant applied (collection error)"
        out.evidence["mutant_collection_error"] = True
        return out

    out.mutant_detected = target_mutant == "failed"
    if out.mutant_detected:
        out.verdict = Verdict.VERIFIED
        out.reason = (
            "repaired test passes on clean code and fails on the exact pre-registered "
            "controlled regression"
        )
    elif target_mutant == "error":
        out.verdict = Verdict.ESCALATED
        out.reason = "target test errors (rather than asserts) with the mutant applied"
    elif failing:
        out.verdict = Verdict.ESCALATED
        out.reason = (
            f"mutant is caught by other tests {failing[:5]} but not by the target test "
            f"{finding.test_node_id}"
        )
    else:
        out.verdict = Verdict.REJECTED
        out.reason = "controlled regression still escapes the repaired test file"
    return out
