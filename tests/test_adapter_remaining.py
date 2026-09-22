"""Focused regressions for adapter, archive, and validator input boundaries."""

import copy
import json

import httpx
import pytest

from app import presentation
from app.github import GitHub
from app.validator import classify


def test_classify_rejects_a_non_object_report() -> None:
    """A JSON scalar cannot be treated as trusted pytest evidence."""

    assert classify([], ["tests/example.py::test_contract"]) == "INFRA_ERROR"


def test_github_rejects_an_invalid_pr_number_before_network(settings) -> None:
    """An invalid PR number is rejected before it reaches the GitHub transport."""

    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Record unexpected calls so the assertion proves validation happened first."""

        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url=f"https://api.github.com/repos/{settings.github_repository}/",
        transport=httpx.MockTransport(transport),
    ) as client:
        github = GitHub(settings, client)
        with pytest.raises(ValueError, match="positive integer"):
            github.candidate(0)

    assert requests == []


def test_recorded_evidence_rejects_boolean_pr_and_provenance(tmp_path, monkeypatch) -> None:
    """Archive records need typed identifiers and explicit boolean provenance."""

    source = json.loads(presentation.ARCHIVE_PATH.read_text())
    archive = tmp_path / "live-application.json"
    monkeypatch.setattr(presentation, "ARCHIVE_PATH", archive)

    boolean_pr = copy.deepcopy(source)
    boolean_pr["candidate"]["pr_number"] = True
    archive.write_text(json.dumps(boolean_pr))
    assert presentation.recorded_evidence() is None

    non_boolean_provenance = copy.deepcopy(source)
    non_boolean_provenance["job"]["provenance"] = "true"
    archive.write_text(json.dumps(non_boolean_provenance))
    assert presentation.recorded_evidence() is None
