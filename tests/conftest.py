"""Shared fixtures.

``fixture_repo`` builds a miniature "target repository" in a temp dir: a package ``mathlib`` with
``normalize()``, a weak test that calls it without asserting anything, and a controlled
regression patch that breaks ``normalize()`` for short inputs. It exercises the exact same
baseline/validator code paths as the Superset finding, in ~1s instead of ~60s.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from drp import db
from drp.config import Settings, get_settings
from drp.findings.spec import FindingSpec, register
from drp.models import Finding

WEAK_TEST = textwrap.dedent(
    '''
    from mathlib import normalize


    def test_normalize_small_frames():
        """Edge cases should not raise."""
        normalize([])
        normalize([5])
        normalize([1, 2, 3])
    '''
)

STRONG_TEST = textwrap.dedent(
    """
    from mathlib import normalize


    def test_normalize_small_frames():
        assert normalize([]) == []
        assert normalize([5]) == [1.0]
        assert normalize([1, 2, 3]) == [0.0, 0.5, 1.0]
    """
)

STILL_WEAK_TEST = textwrap.dedent(
    """
    from mathlib import normalize


    def test_normalize_small_frames():
        assert normalize([]) == []
        assert normalize([1, 2, 3]) == [0.0, 0.5, 1.0]
    """
)

MODULE = textwrap.dedent(
    """
    def normalize(values):
        if not values:
            return []
        lo, hi = min(values), max(values)
        if hi == lo:
            return [1.0 for _ in values]
        return [(v - lo) / (hi - lo) for v in values]
    """
)

MUTANT = textwrap.dedent(
    """\
    --- a/mathlib/__init__.py
    +++ b/mathlib/__init__.py
    @@ -1,5 +1,7 @@

     def normalize(values):
    +    if len(values) < 2:
    +        return list(values)
         if not values:
             return []
         lo, hi = min(values), max(values)
    """
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
        },
    ).stdout.strip()


@dataclass
class FixtureRepo:
    path: Path
    finding_dir: Path
    source_sha: str

    def commit_test(self, content: str, branch: str) -> str:
        git(self.path, "checkout", "-q", "-b", branch, self.source_sha)
        (self.path / "tests" / "test_normalize.py").write_text(content)
        git(self.path, "commit", "-qam", f"repair on {branch}")
        sha = git(self.path, "rev-parse", "HEAD")
        git(self.path, "checkout", "-q", "master")
        return sha

    def commit_files(self, files: dict[str, str], branch: str) -> str:
        git(self.path, "checkout", "-q", "-b", branch, self.source_sha)
        for rel, content in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            git(self.path, "add", rel)
        git(self.path, "commit", "-qm", f"change on {branch}")
        sha = git(self.path, "rev-parse", "HEAD")
        git(self.path, "checkout", "-q", "master")
        return sha


@pytest.fixture
def fixture_repo(tmp_path: Path) -> FixtureRepo:
    repo = tmp_path / "target"
    (repo / "mathlib").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "mathlib" / "__init__.py").write_text(MODULE)
    (repo / "tests" / "test_normalize.py").write_text(WEAK_TEST)
    (repo / "tests" / "__init__.py").write_text("")
    git(repo, "init", "-q", "-b", "master")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    sha = git(repo, "rev-parse", "HEAD")

    finding_dir = tmp_path / "findings" / "mathlib-normalize-small-frames"
    finding_dir.mkdir(parents=True)
    (finding_dir / "mutant.patch").write_text(MUTANT)
    (finding_dir / "finding.yaml").write_text(
        textwrap.dedent(
            f"""
            id: mathlib-normalize-small-frames
            title: test_normalize_small_frames asserts nothing
            repo: example/mathlib
            source_sha: {sha}
            test_file: tests/test_normalize.py
            test_node_id: tests/test_normalize.py::test_normalize_small_frames
            weakness_kind: no_assertion
            weakness_description: calls normalize() three times without asserting.
            protected_behavior: normalize scales values to [0, 1]; a single value maps to 1.0.
            test_command:
              - "{{python}}"
              - "-m"
              - "pytest"
              - "{{test_file}}"
              - "-q"
              - "-p"
              - "no:cacheprovider"
              - "--junitxml={{junit}}"
            allowed_paths:
              - tests/test_normalize.py
            mutant:
              path: mutant.patch
              description: short inputs are returned unnormalized.
            """
        )
    )
    return FixtureRepo(path=repo, finding_dir=finding_dir, source_sha=sha)


@pytest.fixture
def settings(
    tmp_path: Path, fixture_repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("DRP_DATABASE_URL", f"sqlite:///{tmp_path}/drp.sqlite3")
    monkeypatch.setenv("DRP_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DRP_TARGET_CHECKOUT", str(fixture_repo.path))
    monkeypatch.setenv("DRP_TARGET_PYTHON", sys.executable)
    monkeypatch.setenv("DRP_TARGET_PACKAGE", "mathlib")
    monkeypatch.setenv("DRP_TARGET_REPO", "example/mathlib")
    monkeypatch.setenv("DRP_DEVIN_MODE", "fake")
    monkeypatch.setenv("DRP_DEVIN_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("DRP_WORKER_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("DRP_GITHUB_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("GITHUB_DHEERAJ_PAT", "test-token")
    monkeypatch.setenv("DEVIN_API_KEY", "")
    monkeypatch.setenv("DEVIN_ORG_ID", "")
    get_settings.cache_clear()
    db.reset_engine()
    s = get_settings()
    yield s  # type: ignore[misc]
    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def db_session(settings: Settings) -> Iterator[Session]:
    db.init_db()
    with db.session_scope() as s:
        yield s


@pytest.fixture
def finding(db_session: Session, fixture_repo: FixtureRepo) -> Finding:
    spec = FindingSpec.load(fixture_repo.finding_dir)
    f = register(db_session, fixture_repo.finding_dir, spec)
    db_session.commit()
    return f
