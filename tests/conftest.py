import pytest

from app.cases import load_cases
from app.config import Settings
from app.db import Store
from app.demo import SimulatedDevin, SimulatedGitHub, SimulatedValidator
from app.orchestrator import Orchestrator


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path, mode="SIMULATION", run_live=False,
                    github_repository="example/superset", github_repository_id=1,
                    github_webhook_secret="local-simulation-only",
                    case_issues={"histogram-invalid": 101, "schema-no-engine": 102}, poll_seconds=0.1)


@pytest.fixture
def store(settings):
    result = Store(settings.database_path)
    yield result
    result.engine.dispose()


@pytest.fixture
def cases():
    return load_cases()


@pytest.fixture
def harness(settings, store, cases):
    devin = SimulatedDevin()
    github = SimulatedGitHub(devin)
    validator = SimulatedValidator()
    return Orchestrator(settings, store, cases, devin, github, validator)


def enqueue(store, issue=101, case_id="histogram-invalid"):
    return store.enqueue(f"delivery-{issue}", "SIMULATION", "example/superset", issue, case_id)[0]


def tick(worker, job_id):
    worker.store.change(job_id, "TEST_CLOCK", next_poll_at=0)
    worker.tick(job_id)
