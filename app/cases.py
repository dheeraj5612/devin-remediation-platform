"""The trusted registry of approved weak tests ("cases") and their baseline proof.

ELI5: `evals/cases.yaml` is the allow-list. A GitHub issue can only start work
if its number is bound to one of these cases in `CASE_ISSUES`, and everything
Devin is told (test IDs, allowed files, contract) comes from here, never from
the issue text. `Registry.evidence` is the gate that says "we have already
proven, on this machine, that the original test is weak".
"""

import ast
import hashlib
import json

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import ROOT, Settings


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")  # typos in the YAML fail loudly
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    source_repository: str
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")  # the exact Superset commit everything is measured against
    affected_paths: list[str]  # production code the test is supposed to protect
    allowed_paths: list[str]  # the only files Devin may change (test files)
    test_ids: list[str]  # pytest node IDs that must keep existing
    challenge: str  # name of the registered regression in evals/challenges.py
    contract: str  # plain-English behaviour the test must enforce
    acceptance: str
    inspection: str  # why a human believes the test is weak

    @property
    def fingerprint(self) -> str:
        # Changing any field invalidates old baseline proofs.
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def harness_fingerprint() -> str:
    """Hash of the evaluation code itself. If the oracle changes, every old proof is stale."""
    source = (ROOT / "evals/challenges.py").read_bytes() + (ROOT / "app/validator.py").read_bytes()
    return hashlib.sha256(source).hexdigest()


class Registry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        entries = [Case.model_validate(item) for item in yaml.safe_load(settings.cases_file.read_text())]
        self.cases = {case.id: case for case in entries}
        if len(self.cases) != len(entries):
            raise ValueError("Duplicate case ID")
        # issue number -> case. Only these issue numbers can ever create a job.
        bindings = settings.case_issues
        self.by_issue = {number: self.cases[name] for name, number in bindings.items()}
        if len(self.by_issue) != len(bindings) or any(number < 1 for number in self.by_issue):
            raise ValueError("Issue bindings must be unique positive issue numbers")
        if settings.mode == "SIMULATION":
            self.by_issue = {101: entries[0], 102: entries[1], 103: entries[0], 104: entries[1]}  # demo scenarios

    def evidence(self, case: Case) -> dict:
        """Load the baseline proof written by `make baseline`; raise if missing, failed, or stale.

        CONFIRMED means: original test PASSES on clean code *and* PASSES with the registered
        regression active -> the test is provably weak and worth paying Devin to fix.
        """
        path = self.settings.storage / "baselines" / f"{case.id}.json"
        if not path.exists():
            raise ValueError(f"Baseline unconfirmed: run make baseline for {case.id}")
        data = json.loads(path.read_text())
        if (data.get("outcome") != "CONFIRMED" or data.get("normal") != "PASS" or data.get("mutant") != "PASS" or data.get("mode") != "LIVE"
                or data.get("case_id") != case.id or data.get("sha") != case.baseline_sha or data.get("case_fingerprint") != case.fingerprint
                or data.get("harness_fingerprint") != harness_fingerprint()):
            raise ValueError(f"Baseline proof is missing, unsuccessful, or stale: {case.id}")
        return data


def detect(source: str, path: str) -> list[dict]:
    """Static hint: flag test functions whose *only* asserts live inside `except` blocks.

    Such a test silently passes when no exception is raised at all. This is a
    pointer for humans; runtime baseline proof (above) is what actually admits a case.
    """
    findings = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith("test_"):
            continue
        assertions = {node.lineno for node in ast.walk(function) if isinstance(node, ast.Assert)}
        handlers = [node for node in ast.walk(function) if isinstance(node, ast.ExceptHandler)]
        guarded = {node.lineno for handler in handlers for node in ast.walk(handler) if isinstance(node, ast.Assert)}
        if assertions and assertions == guarded:
            findings.append({"path": path, "test": function.name, "line": function.lineno,
                             "rule": "assertions-only-in-except", "status": "SUSPICIOUS_NOT_CONFIRMED"})
    return findings
