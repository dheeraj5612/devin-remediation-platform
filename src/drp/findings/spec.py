"""Finding specification (``finding.yaml``) and its registration into the database.

A finding directory looks like::

    findings/<finding-id>/
        finding.yaml   # metadata, baseline command, protected behaviour
        mutant.patch   # the pre-registered controlled regression (git patch)

The mutant is the oracle for the whole pipeline: it is registered *before* Devin is
involved and the exact same bytes (pinned by sha256) are replayed by the validator.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drp.models import BaselineStatus, Finding


class MutantSpec(BaseModel):
    path: str = "mutant.patch"
    description: str


class FindingSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,100}$")
    title: str
    repo: str
    source_sha: str = Field(min_length=7, max_length=64)
    test_file: str
    test_node_id: str
    weakness_kind: str
    weakness_description: str
    protected_behavior: str
    test_command: list[str]
    allowed_paths: list[str]
    mutant: MutantSpec

    @classmethod
    def load(cls, directory: Path) -> FindingSpec:
        data = yaml.safe_load((directory / "finding.yaml").read_text(encoding="utf-8"))
        return cls.model_validate(data)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_all(findings_dir: Path) -> list[tuple[Path, FindingSpec]]:
    specs: list[tuple[Path, FindingSpec]] = []
    for directory in sorted(findings_dir.iterdir()):
        if (directory / "finding.yaml").is_file():
            specs.append((directory, FindingSpec.load(directory)))
    return specs


def register(session: Session, directory: Path, spec: FindingSpec) -> Finding:
    """Insert or refresh a finding row from its spec. Baseline status is preserved unless the
    mutant bytes changed, in which case the previous baseline evidence is invalidated."""
    mutant_path = (directory / spec.mutant.path).resolve()
    digest = sha256_file(mutant_path)
    finding = session.get(Finding, spec.id)
    if finding is None:
        finding = Finding(id=spec.id)
        session.add(finding)
    elif finding.mutant_sha256 != digest or finding.source_sha != spec.source_sha:
        finding.baseline_status = BaselineStatus.UNREGISTERED
        finding.baseline_evidence = {}
    finding.title = spec.title
    finding.repo = spec.repo
    finding.source_sha = spec.source_sha
    finding.test_file = spec.test_file
    finding.test_node_id = spec.test_node_id
    finding.weakness_kind = spec.weakness_kind
    finding.weakness_description = spec.weakness_description
    finding.protected_behavior = spec.protected_behavior
    finding.test_command = list(spec.test_command)
    finding.allowed_paths = list(spec.allowed_paths)
    finding.mutant_path = str(mutant_path)
    finding.mutant_sha256 = digest
    finding.mutant_description = spec.mutant.description
    session.flush()
    return finding
