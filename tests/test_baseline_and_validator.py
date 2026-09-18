"""Baseline + independent validation on the miniature fixture repo (real git, real pytest)."""

from __future__ import annotations

from pathlib import Path

from drp.baseline import establish_baseline
from drp.config import Settings
from drp.models import BaselineStatus, Finding, Verdict
from drp.validator.validate import validate_candidate
from tests.conftest import MODULE, STILL_WEAK_TEST, STRONG_TEST, FixtureRepo


def test_baseline_confirms_original_test_is_false_green(
    finding: Finding, settings: Settings
) -> None:
    status, ev = establish_baseline(finding, settings)
    assert status == BaselineStatus.CONFIRMED, ev
    assert ev["clean_original"]["passed"] == 1
    assert ev["mutant_original"]["passed"] == 1  # regression escapes the original test
    assert ev["target_status_clean"] == "passed"
    assert ev["target_status_mutant"] == "passed"
    assert ev["mutant_sha256"] == finding.mutant_sha256
    assert Path(ev["clean_original"]["junit_path"]).is_file()
    assert Path(ev["mutant_original"]["log_path"]).is_file()
    assert ev["provenance"]["resolved_from_worktree"]


def test_baseline_refutes_when_original_test_already_catches_regression(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo
) -> None:
    sha = fixture_repo.commit_test(STRONG_TEST, "already-strong")
    finding.source_sha = sha
    status, ev = establish_baseline(finding, settings)
    assert status == BaselineStatus.REFUTED
    assert ev["mutant_original"]["failed"] == 1


def test_baseline_errors_when_mutant_does_not_apply(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo
) -> None:
    sha = fixture_repo.commit_files(
        {"mathlib/__init__.py": MODULE.replace("if not values:", "if len(values) == 0:")},
        "drift",
    )
    finding.source_sha = sha
    status, ev = establish_baseline(finding, settings)
    assert status == BaselineStatus.ERROR
    assert "does not apply" in ev["error"]


def test_validator_verifies_repaired_test(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo, tmp_path: Path
) -> None:
    head = fixture_repo.commit_test(STRONG_TEST, "fix")
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.VERIFIED, out.reason
    assert out.scope_ok and out.target_test_present
    assert out.clean_passed is True and out.mutant_detected is True
    assert out.evidence["clean_repaired"]["passed"] == 1
    assert out.evidence["mutant_repaired"]["failed"] == 1
    assert out.evidence["target_status_mutant"] == "failed"
    assert out.mutant_sha256 == finding.mutant_sha256
    assert out.evidence["changed_files"] == ["tests/test_normalize.py"]


def test_validator_rejects_when_regression_still_escapes(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo, tmp_path: Path
) -> None:
    head = fixture_repo.commit_test(STILL_WEAK_TEST, "meh")
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.REJECTED
    assert out.clean_passed is True and out.mutant_detected is False


def test_validator_rejects_test_that_fails_on_clean_code(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo, tmp_path: Path
) -> None:
    head = fixture_repo.commit_test(STRONG_TEST.replace("[1.0]", "[42.0]"), "wrong")
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.REJECTED
    assert out.clean_passed is False
    assert out.mutant_detected is None  # never run: clean failure is conclusive


def test_validator_escalates_when_production_code_is_touched(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo, tmp_path: Path
) -> None:
    head = fixture_repo.commit_files(
        {
            "tests/test_normalize.py": STRONG_TEST,
            "mathlib/__init__.py": MODULE + "\n# touched\n",
        },
        "scope",
    )
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.ESCALATED  # never auto-reject a PR that touches prod code
    assert not out.scope_ok
    assert "mathlib/__init__.py" in out.reason


def test_validator_rejects_when_target_test_removed_or_skipped(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo, tmp_path: Path
) -> None:
    renamed = STRONG_TEST.replace("test_normalize_small_frames", "test_other_name")
    head = fixture_repo.commit_test(renamed, "renamed")
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.REJECTED
    assert not out.target_test_present

    skipped = "import pytest\n" + STRONG_TEST.replace(
        "def test_normalize_small_frames", "@pytest.mark.skip\ndef test_normalize_small_frames"
    )
    head = fixture_repo.commit_test(skipped, "skipped")
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.REJECTED
    assert "skipped" in out.reason


def test_validator_escalates_on_mutant_hash_mismatch(
    finding: Finding, settings: Settings, fixture_repo: FixtureRepo, tmp_path: Path
) -> None:
    head = fixture_repo.commit_test(STRONG_TEST, "fix2")
    Path(finding.mutant_path).write_text(Path(finding.mutant_path).read_text() + "\n")
    out = validate_candidate(
        finding=finding, head_sha=head, settings=settings, artifacts_root=tmp_path / "v"
    )
    assert out.verdict == Verdict.ESCALATED
    assert "sha256" in out.reason
