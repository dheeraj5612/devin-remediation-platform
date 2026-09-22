"""The trusted registry of approved remediation cases and their baseline proof.

ELI5: `evals/cases.yaml` is the allow-list. A GitHub issue can only start work
if its number is bound to one of these cases in `CASE_ISSUES`, and everything
Devin is told (test IDs, allowed files, contract) comes from here, never from
the issue text. `Registry.evidence` is the gate that says "we have already
proven, on this machine, that the pinned case exposes its approved defect".
"""

import ast
import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import ROOT, Settings


# ELI5: this model is the one source of truth for case scope and acceptance fields.
class Case(BaseModel):
    """One approved repair case with immutable scope and acceptance rules."""

    # ELI5: one frozen record describes exactly what Devin may attempt to repair.
    model_config = ConfigDict(frozen=True, extra="forbid")  # typos in the YAML fail loudly
    # ELI5: the slug identifies this approved case in webhooks and evidence files.
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    # ELI5: the validator chooses test-quality or production-application behavior from this kind.
    kind: Literal["test_quality", "application"] = "test_quality"
    # ELI5: the title is the human-readable name shown in prompts and reports.
    title: str
    # ELI5: record the public source repository that supplied the approved defect.
    source_repository: str
    # ELI5: application fixes may need a branch different from the global demo branch.
    target_branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._/-]+$")  # per-case PR base
    # ELI5: pin one immutable source commit so baseline and candidate comparisons agree.
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")  # the exact Superset commit everything is measured against
    # ELI5: list production files related to the defect for review and provenance.
    affected_paths: list[str]  # production code the test is supposed to protect
    # ELI5: restrict Devin's changes to this exact allow-list.
    allowed_paths: list[str]  # the only files Devin may change
    # ELI5: identify the tests that must run for a test-quality case.
    test_ids: list[str]  # pytest node IDs that must keep existing
    # ELI5: name the registered mutant that proves the test catches the weakness.
    challenge: str  # name of the registered regression in evals/challenges.py
    # ELI5: state the behavior in words so humans can review the acceptance contract.
    contract: str  # plain-English behaviour the test must enforce
    # ELI5: explain what a successful fix must demonstrate.
    acceptance: str
    # ELI5: preserve the human review reason for admitting this case.
    inspection: str  # why a human approved this case for remediation
    # ELI5: application cases use this control-plane oracle instead of candidate tests.
    acceptance_test: str | None = None  # trusted application oracle, outside the candidate checkout

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "Case":
        """Require an application case to name its control-plane acceptance oracle."""
        # ELI5: only the control plane may name the checker, and its path must stay in our tree.
        if self.kind == "application" and not self.acceptance_test:
            # ELI5: refuse an application case that has no trusted acceptance contract.
            raise ValueError("application cases require acceptance_test")
        # ELI5: a candidate cannot move the trusted script by sneaking in an absolute or parent path.
        if self.acceptance_test and (Path(self.acceptance_test).is_absolute()
                                     or not self.acceptance_test.startswith("evals/application_cases/")
                                     or not self.acceptance_test.endswith(".py")):
            # ELI5: refuse absolute, parent-traversal, or non-Python oracle paths.
            raise ValueError("acceptance_test must be a relative Python file under evals/application_cases")
        # ELI5: return the unchanged frozen record after all cross-field checks pass.
        return self

    @property
    def fingerprint(self) -> str:
        """Return the stable identity of every case field used by baseline evidence."""
        # ELI5: changing any case field makes old baseline proof unsafe to reuse.
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def harness_fingerprint() -> str:
    """Hash of the evaluation code itself. If the oracle changes, every old proof is stale."""
    # ELI5: include the registry because it owns case admission and proof validation gates.
    paths = [ROOT / "app/cases.py", ROOT / "evals/challenges.py", ROOT / "app/validator.py"]
    # Application oracles are trusted code too, so changing one invalidates its old proof.
    # ELI5: include every application oracle because its behavior is trusted evidence too.
    # ELI5: include nested oracle modules too, so changing any trusted evaluator invalidates proof.
    paths.extend(sorted((ROOT / "evals/application_cases").rglob("*.py")))
    # ELI5: hash names and bytes together so replacing or renaming an oracle cannot reuse proof.
    source = b"".join(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() for path in paths)
    # ELI5: one digest lets baseline readers reject any changed evaluator code.
    return hashlib.sha256(source).hexdigest()


# ELI5: this registry turns trusted case configuration into safe runtime lookups.
class Registry:
    """Load approved cases and bind only explicitly configured issue numbers."""

    def __init__(self, settings: Settings) -> None:
        """Load the allow-list and bind only approved issue numbers to immutable cases."""
        # ELI5: retain settings so evidence is read from the same mode-specific store.
        self.settings = settings
        # ELI5: parse every YAML row through the strict Pydantic schema before binding issues.
        entries = [Case.model_validate(item) for item in yaml.safe_load(settings.cases_file.read_text())]
        # ELI5: index cases by their stable IDs for prompts and webhook bindings.
        self.cases = {case.id: case for case in entries}
        # ELI5: reject duplicate IDs because they would make an issue ambiguous.
        if len(self.cases) != len(entries):
            # ELI5: stop before an ambiguous case can be selected by a webhook.
            raise ValueError("Duplicate case ID")
        # ELI5: take only explicit issue-to-case bindings from trusted settings.
        bindings = settings.case_issues
        # ELI5: convert the configured mapping into the lookup used by webhook admission.
        self.by_issue = {number: self.cases[name] for name, number in bindings.items()}
        # ELI5: reject duplicate or non-positive issue numbers before accepting webhooks.
        if len(self.by_issue) != len(bindings) or any(number < 1 for number in self.by_issue):
            # ELI5: stop before malformed settings can admit an unknown issue.
            raise ValueError("Issue bindings must be unique positive issue numbers")
        # ELI5: simulation mode intentionally uses fixed demo issue numbers instead of live bindings.
        if settings.mode == "SIMULATION":
            # ELI5: bind six demo issues to the first three cases, including both app outcomes.
            self.by_issue = {101: entries[0], 102: entries[1], 103: entries[0], 104: entries[1],
                             105: entries[2], 106: entries[2]}

    def evidence(self, case: Case) -> dict:
        """Load the baseline proof written by `make baseline`; raise if missing, failed, or stale.

        CONFIRMED means: the pinned case's trusted test or application oracle produced its
        expected baseline evidence and is worth paying Devin to fix.
        """
        # ELI5: derive the proof path from the case ID inside the selected storage mode.
        path = self.settings.storage / "baselines" / f"{case.id}.json"
        # ELI5: no proof means the worker must not spend a Devin session on this case.
        if not path.exists():
            # ELI5: tell the operator which case needs a local baseline proof.
            raise ValueError(f"Baseline unconfirmed: run make baseline for {case.id}")
        # ELI5: load the recorded result so every identity and verdict field can be checked.
        data = json.loads(path.read_text())
        # ELI5: shared identity checks catch stale files before case-specific status checks.
        common = (data.get("outcome") == "CONFIRMED" and data.get("mode") == "LIVE"
                  and data.get("case_id") == case.id and data.get("sha") == case.baseline_sha
                  and data.get("case_fingerprint") == case.fingerprint
                  and data.get("harness_fingerprint") == harness_fingerprint())
        # ELI5: choose the proof fields required by the selected case kind.
        if case.kind == "application":
            # ELI5: an app proof needs the exact known crash plus a true candidate provenance marker.
            valid = common and data.get("application") == "REGRESSION" and data.get("provenance") is True
        else:
            # ELI5: a test-quality proof needs both the normal and mutant phases to pass their gates.
            valid = common and data.get("normal") == "PASS" and data.get("mutant") == "PASS"
        # ELI5: reject missing, stale, or unsuccessful proof before any paid work starts.
        if not valid:
            # ELI5: fail closed instead of treating a partial JSON file as evidence.
            raise ValueError(f"Baseline proof is missing, unsuccessful, or stale: {case.id}")
        # ELI5: return only after all identity, fingerprint, and verdict gates pass.
        return data


def detect(source: str, path: str) -> list[dict]:
    """Static hint: flag test functions whose *only* asserts live inside `except` blocks.

    Such a test silently passes when no exception is raised at all. This is a
    pointer for humans; runtime baseline proof (above) is what actually admits a case.
    """
    # ELI5: collect suspicious test shapes as review hints, not as acceptance evidence.
    findings = []
    # ELI5: walk nested functions so methods and ordinary tests are inspected uniformly.
    for function in ast.walk(ast.parse(source)):
        # ELI5: only test_* functions can be affected by the exception-only assertion weakness.
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith("test_"):
            # ELI5: skip helpers and non-test functions because the rule concerns test bodies only.
            continue
        # ELI5: record every assertion line in the candidate test function.
        assertions = {node.lineno for node in ast.walk(function) if isinstance(node, ast.Assert)}
        # ELI5: find exception handlers that might hide the test's only assertions.
        handlers = [node for node in ast.walk(function) if isinstance(node, ast.ExceptHandler)]
        # ELI5: collect assertions located inside those handlers for comparison.
        guarded = {node.lineno for handler in handlers for node in ast.walk(handler) if isinstance(node, ast.Assert)}
        # ELI5: flag a test when every assertion is unreachable unless an exception occurs.
        if assertions and assertions == guarded:
            # ELI5: keep the finding tied to the exact test line for human review.
            findings.append({"path": path, "test": function.name, "line": function.lineno,
                             "rule": "assertions-only-in-except", "status": "SUSPICIOUS_NOT_CONFIRMED"})
    # ELI5: return hints without deciding whether a baseline is confirmed.
    return findings
