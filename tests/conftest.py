"""Shared fixtures: simulation settings, a fresh SQLite Store, the web client, a fake-backed Orchestrator (`rig`),
and `live`: LIVE-mode settings with fake credentials plus on-disk baseline proof and context for API-client tests."""

import json

import pytest
from fastapi.testclient import TestClient

from app.cases import Registry, harness_fingerprint
from app.db import Store
from app.main import create_app
from app.orchestrator import Orchestrator
from app.simulation import FakeDevin, FakeGitHub, FakeValidator, simulation_settings


@pytest.fixture
def settings(tmp_path):
    return simulation_settings(tmp_path)


@pytest.fixture
def store(settings):
    database = Store(settings)
    yield database
    database.engine.dispose()


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def rig(settings, store):
    devin = FakeDevin()
    github = FakeGitHub(devin)
    validator = FakeValidator()
    return Orchestrator(settings, store, devin, github, validator)


@pytest.fixture
def live(settings):
    config = settings.model_copy(update={
        "mode": "LIVE", "enable_live": True, "allow_local_validation": True,
        "devin_org_id": "org-test", "case_issues": {"histogram-invalid-column": 101, "schema-missing-engine": 102},
    })
    # Pydantic's model_copy does not validate updates; use normal assignment for secret fields.
    config.devin_api_key = config.github_webhook_secret
    config.github_token = config.github_webhook_secret
    registry = Registry(config)
    for case in registry.cases.values():
        proof = {"case_id": case.id, "mode": "LIVE", "sha": case.baseline_sha, "outcome": "CONFIRMED",
                 "normal": "PASS", "mutant": "PASS", "case_fingerprint": case.fingerprint,
                 "harness_fingerprint": harness_fingerprint()}
        directory = config.storage / "baselines"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{case.id}.json").write_text(json.dumps(proof))
    context = {"org_id": config.devin_org_id, "repository": config.github_repository,
               "base_branch": config.base_branch, "playbook_id": "playbook-test", "note_id": "note-test"}
    (config.storage / "context.json").write_text(json.dumps(context))
    return config
