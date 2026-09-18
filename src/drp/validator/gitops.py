"""Thin git wrapper: isolated worktrees, patch application, PR head fetching."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or self.checkout,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def rev_parse(self, rev: str) -> str:
        return self.run("rev-parse", "--verify", f"{rev}^{{commit}}").strip()

    def has_commit(self, sha: str) -> bool:
        try:
            self.run("cat-file", "-e", f"{sha}^{{commit}}")
        except GitError:
            return False
        return True

    def fetch_commit(self, remote: str, sha: str) -> None:
        if not self.has_commit(sha):
            self.run("fetch", "--quiet", remote, sha)

    def fetch_pr_head(self, remote: str, pr_number: int) -> str:
        """Fetch ``refs/pull/N/head`` and return its sha."""
        ref = f"refs/remotes/{remote}/pr/{pr_number}"
        self.run("fetch", "--quiet", "--force", remote, f"pull/{pr_number}/head:{ref}")
        return self.rev_parse(ref)

    @contextmanager
    def worktree(self, sha: str, root: Path) -> Iterator[Path]:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"wt-{sha[:12]}-{uuid.uuid4().hex[:8]}"
        self.run("worktree", "add", "--detach", "--quiet", str(path), sha)
        try:
            yield path
        finally:
            self.run("worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            self.run("worktree", "prune", check=False)

    def patch_applies(self, worktree: Path, patch: Path) -> tuple[bool, str]:
        proc = subprocess.run(
            ["git", "apply", "--check", "--verbose", str(patch)],
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()

    def apply_patch(self, worktree: Path, patch: Path) -> None:
        self.run("apply", str(patch), cwd=worktree)

    def changed_files(self, base: str, head: str) -> list[str]:
        out = self.run("diff", "--name-only", f"{base}...{head}")
        return [line for line in out.splitlines() if line.strip()]

    def merge_base(self, a: str, b: str) -> str:
        return self.run("merge-base", a, b).strip()

    def diff_stat(self, base: str, head: str) -> str:
        return self.run("diff", "--stat", f"{base}...{head}")
