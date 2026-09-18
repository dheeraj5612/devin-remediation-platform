from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from sqlalchemy.orm import Session

from drp.findings.scanner import scan_source
from drp.findings.spec import FindingSpec, register
from drp.models import BaselineStatus, Finding
from tests.conftest import FixtureRepo


def test_scanner_flags_no_assertion_but_not_raises_or_mock_asserts() -> None:
    src = textwrap.dedent(
        """
        import pytest

        def test_weak():
            do_thing()

        def test_raises():
            with pytest.raises(ValueError):
                do_thing()

        def test_mock(m):
            m.assert_called_once()

        def test_true():
            assert True

        def test_swallow():
            try:
                do_thing()
            except Exception:
                pass

        def helper():
            do_thing()
        """
    )
    hits = scan_source(src, "t.py")
    by_test = {(h.test_name, h.kind) for h in hits}
    assert ("test_weak", "no_assertion") in by_test
    assert ("test_true", "assert_true") in by_test
    assert ("test_swallow", "swallowed_exception") in by_test
    assert not any(h.test_name in {"test_raises", "test_mock", "helper"} for h in hits)


def test_register_pins_mutant_sha_and_invalidates_baseline_on_change(
    db_session: Session, fixture_repo: FixtureRepo
) -> None:
    spec = FindingSpec.load(fixture_repo.finding_dir)
    f = register(db_session, fixture_repo.finding_dir, spec)
    patch = fixture_repo.finding_dir / "mutant.patch"
    assert f.mutant_sha256 == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert f.baseline_status == BaselineStatus.UNREGISTERED
    assert f.allowed_paths == ["tests/test_normalize.py"]

    f.baseline_status = BaselineStatus.CONFIRMED
    f.baseline_evidence = {"x": 1}
    db_session.flush()
    register(db_session, fixture_repo.finding_dir, spec)  # unchanged -> baseline preserved
    assert f.baseline_status == BaselineStatus.CONFIRMED

    patch.write_text(patch.read_text() + "\n")
    register(db_session, fixture_repo.finding_dir, spec)
    assert f.baseline_status == BaselineStatus.UNREGISTERED
    assert f.baseline_evidence == {}
    assert f.mutant_sha256 == hashlib.sha256(patch.read_bytes()).hexdigest()


def test_shipped_superset_finding_spec_is_valid() -> None:
    root = Path(__file__).resolve().parents[1] / "findings" / "superset-normalize-dttm-edge-cases"
    spec = FindingSpec.load(root)
    assert spec.test_node_id == "tests/unit_tests/utils/test_date_parsing.py::test_edge_cases"
    assert spec.allowed_paths == ["tests/unit_tests/utils/test_date_parsing.py"]
    assert (root / spec.mutant.path).is_file()
    assert "superset/utils/core.py" in (root / spec.mutant.path).read_text()


def test_finding_model_roundtrip(finding: Finding) -> None:
    assert finding.id == "mathlib-normalize-small-frames"
    assert finding.test_command[0] == "{python}"
