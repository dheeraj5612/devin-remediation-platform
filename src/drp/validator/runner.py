"""Deterministic test execution inside a git worktree, with junit-based evidence."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TestResult:
    command: list[str]
    exit_code: int
    duration_s: float
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    cases: dict[str, str] = field(default_factory=dict)  # nodeid -> passed|failed|error|skipped
    log_path: str = ""
    junit_path: str = ""
    timed_out: bool = False

    @property
    def all_passed(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.errors == 0 and self.total > 0

    def status_of(self, node_id: str) -> str | None:
        if node_id in self.cases:
            return self.cases[node_id]
        # parametrized ids: "file::test[param]" all belong to "file::test"
        statuses = {v for k, v in self.cases.items() if k.split("[", 1)[0] == node_id}
        if not statuses:
            return None
        for s in ("error", "failed", "passed", "skipped"):
            if s in statuses:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def render_command(
    template: list[str], *, python: Path, test_file: str, junit: Path, worktree: Path
) -> list[str]:
    values = {
        "python": str(python),
        "test_file": test_file,
        "junit": str(junit),
        "worktree": str(worktree),
    }
    return [part.format(**values) for part in template]


def _junit_nodeid(testcase: ET.Element, fallback_file: str) -> str:
    classname = testcase.get("classname", "")
    name = testcase.get("name", "")
    file_attr = testcase.get("file")
    if file_attr:
        path = file_attr
        # classname is "pkg.module[.Class]" -> keep the part after the module
        module = Path(path).with_suffix("").as_posix().replace("/", ".")
        cls = classname.removeprefix(module).lstrip(".")
    else:
        parts = classname.split(".")
        # heuristic: module path is every leading part that is not a class (CamelCase)
        module_parts = [p for p in parts if not p[:1].isupper()]
        class_parts = parts[len(module_parts) :]
        path = "/".join(module_parts) + ".py" if module_parts else fallback_file
        cls = ".".join(class_parts)
    return f"{path}::{cls}::{name}" if cls else f"{path}::{name}"


def parse_junit(junit_path: Path, fallback_file: str) -> tuple[dict[str, int], dict[str, str]]:
    counts = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    cases: dict[str, str] = {}
    if not junit_path.is_file():
        return counts, cases
    root = ET.parse(junit_path).getroot()
    for tc in root.iter("testcase"):
        node_id = _junit_nodeid(tc, fallback_file)
        if tc.find("failure") is not None:
            status = "failed"
        elif tc.find("error") is not None:
            status = "error"
        elif tc.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        cases[node_id] = status
        counts["total"] += 1
        counts[status if status != "error" else "errors"] += 1
    return counts, cases


def run_tests(
    *,
    worktree: Path,
    command_template: list[str],
    python: Path,
    test_file: str,
    artifacts_dir: Path,
    label: str,
    timeout_s: int,
) -> TestResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    junit = artifacts_dir / f"{label}.junit.xml"
    log = artifacts_dir / f"{label}.log"
    cmd = render_command(
        command_template, python=python, test_file=test_file, junit=junit, worktree=worktree
    )
    env = {
        "PYTHONPATH": str(worktree),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "SUPERSET_TESTENV": "true",
    }
    started = time.monotonic()
    timed_out = False
    with log.open("w", encoding="utf-8") as fh:
        fh.write(f"$ (cd {worktree} && {' '.join(cmd)})\n\n")
        fh.flush()
        try:
            proc = subprocess.run(
                cmd, cwd=worktree, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout_s
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code = -1
            timed_out = True
            fh.write(f"\n*** timed out after {timeout_s}s ***\n")
    duration = time.monotonic() - started
    counts, cases = parse_junit(junit, test_file)
    return TestResult(
        command=cmd,
        exit_code=exit_code,
        duration_s=round(duration, 3),
        total=counts["total"],
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["errors"],
        skipped=counts["skipped"],
        cases=cases,
        log_path=str(log),
        junit_path=str(junit),
        timed_out=timed_out,
    )


def import_provenance(*, worktree: Path, python: Path, package: str) -> dict[str, Any]:
    """Prove the interpreter resolves ``package`` from the worktree, not from another checkout."""
    proc = subprocess.run(
        [
            str(python),
            "-c",
            f"import {package}, sys; print({package}.__file__); print(sys.version)",
        ],
        cwd=worktree,
        env={"PYTHONPATH": str(worktree), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = proc.stdout.strip().splitlines()
    module_file = lines[0] if lines else ""
    return {
        "package": package,
        "module_file": module_file,
        "resolved_from_worktree": module_file.startswith(str(worktree)),
        "python_version": lines[1] if len(lines) > 1 else "",
        "stderr": proc.stderr.strip()[-2000:],
    }


def environment_fingerprint(python: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [str(python), "-m", "pip", "freeze", "--exclude-editable"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    freeze = proc.stdout if proc.returncode == 0 else ""
    return {
        "python": str(python),
        "platform": platform.platform(),
        "pip_freeze_sha256": hashlib.sha256(freeze.encode()).hexdigest() if freeze else None,
        "pip_freeze_lines": len(freeze.splitlines()),
    }
