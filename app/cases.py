import ast
import hashlib
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]


class Case(BaseModel):
    id: str
    title: str
    repository: str
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    issue_number: int | None = None
    allowed_paths: list[str]
    affected_paths: list[str]
    test_ids: list[str]
    contract: str
    challenge: str
    acceptance: str

    @property
    def command(self) -> list[str]:
        return ["python", "-m", "pytest", "-q", "-o", "addopts=", "-p", "pytest_mock", "-p", "probe", *self.test_ids]

    @property
    def fingerprint(self) -> str:
        content = json.dumps(self.model_dump(), sort_keys=True).encode()
        content += (ROOT / "evals" / "probe.py").read_bytes()
        return hashlib.sha256(content).hexdigest()


def load_cases() -> dict[str, Case]:
    cases = [Case.model_validate(item) for item in yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Duplicate case ID")
    return {case.id: case for case in cases}


def baseline_evidence(case: Case, directory: Path) -> dict:
    path = directory / "baselines" / f"{case.id}.json"
    if not path.exists():
        raise ValueError(f"Baseline not confirmed: run make baseline CASE={case.id}")
    evidence = json.loads(path.read_text())
    if evidence.get("fingerprint") != case.fingerprint or evidence.get("baseline_sha") != case.baseline_sha:
        raise ValueError("Baseline proof does not match this case and evaluator")
    from app.validator import baseline_confirmed

    if not baseline_confirmed(case, evidence.get("normal", {}), evidence.get("mutant", {})):
        raise ValueError("Baseline lacks passing original tests and active positive controls")
    return evidence


def detect(path: Path) -> list[dict]:
    tree = ast.parse(path.read_text())
    findings = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith("test_"):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Try) and not node.orelse:
                assertions = [child for handler in node.handlers for child in ast.walk(handler) if isinstance(child, ast.Assert)]
                guards = any(isinstance(child, (ast.Assert, ast.Raise)) for statement in node.body for child in ast.walk(statement))
                if assertions and not guards:
                    findings.append({"test": function.name, "line": node.lineno, "finding": "Exception-only assertion; runtime proof required"})
    return findings
