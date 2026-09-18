"""Stage 2/3 of the pipeline: deterministic baseline + pre-registered controlled regression.

For a finding we prove, on the pinned ``source_sha`` and in an isolated worktree:

1. ``clean_original``  - the original test file passes on clean code and the target test
                         node is present.
2. ``mutant_original`` - the exact mutant patch applies, and the original test file *still
                         passes* with the regression in place -> the test is false-green.

Both runs are recorded as evidence (exit codes, junit counts, per-case statuses, logs,
import provenance, environment fingerprint). Only a ``CONFIRMED`` finding may be turned into
an issue and remediated.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from drp.config import Settings
from drp.models import BaselineStatus, Finding
from drp.validator.gitops import GitRepo
from drp.validator.runner import environment_fingerprint, import_provenance, run_tests


def establish_baseline(
    finding: Finding, settings: Settings
) -> tuple[BaselineStatus, dict[str, Any]]:
    repo = GitRepo(settings.target_checkout)
    artifacts = settings.artifacts_dir / "baselines" / finding.id / time.strftime("%Y%m%dT%H%M%SZ")
    artifacts.mkdir(parents=True, exist_ok=True)
    mutant = Path(finding.mutant_path)
    evidence: dict[str, Any] = {
        "source_sha": finding.source_sha,
        "mutant_sha256": finding.mutant_sha256,
        "artifacts_dir": str(artifacts),
        "environment": environment_fingerprint(settings.target_python),
    }

    if not repo.has_commit(finding.source_sha):
        evidence["error"] = (
            f"source sha {finding.source_sha} not present in {settings.target_checkout}"
        )
        return BaselineStatus.ERROR, evidence

    with repo.worktree(finding.source_sha, settings.artifacts_dir / "worktrees") as wt:
        evidence["provenance"] = import_provenance(
            worktree=wt, python=settings.target_python, package=settings.target_package
        )
        if not evidence["provenance"]["resolved_from_worktree"]:
            evidence["error"] = "package not imported from isolated worktree"
            return BaselineStatus.ERROR, evidence

        clean = run_tests(
            worktree=wt,
            command_template=finding.test_command,
            python=settings.target_python,
            test_file=finding.test_file,
            artifacts_dir=artifacts,
            label="clean_original",
            timeout_s=settings.test_timeout_s,
        )
        evidence["clean_original"] = clean.to_dict()
        target_status = clean.status_of(finding.test_node_id)
        evidence["target_status_clean"] = target_status
        if not clean.all_passed or target_status != "passed":
            evidence["error"] = (
                "original test does not pass on clean code; baseline not deterministic"
            )
            return BaselineStatus.ERROR, evidence

        applies, apply_log = repo.patch_applies(wt, mutant)
        evidence["mutant_apply_check"] = apply_log
        if not applies:
            evidence["error"] = "mutant patch does not apply to source sha"
            return BaselineStatus.ERROR, evidence
        repo.apply_patch(wt, mutant)
        evidence["mutant_diff_stat"] = repo.run("diff", "--stat", cwd=wt)

        mutated = run_tests(
            worktree=wt,
            command_template=finding.test_command,
            python=settings.target_python,
            test_file=finding.test_file,
            artifacts_dir=artifacts,
            label="mutant_original",
            timeout_s=settings.test_timeout_s,
        )
        evidence["mutant_original"] = mutated.to_dict()
        evidence["target_status_mutant"] = mutated.status_of(finding.test_node_id)

    if mutated.all_passed:
        evidence["conclusion"] = (
            "original test file passes with the controlled regression applied: "
            "the regression escapes the test (false-green confirmed)"
        )
        return BaselineStatus.CONFIRMED, evidence
    evidence["conclusion"] = (
        "original test already detects the controlled regression; not a finding"
    )
    return BaselineStatus.REFUTED, evidence
