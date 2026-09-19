import pytest
from pydantic import ValidationError

from app.cases import detect
from app.config import Settings


def test_detector_reports_findings_not_proof(tmp_path):
    source = tmp_path / "test_sample.py"
    source.write_text('''def test_weak():
    try:
        call()
    except ValueError as error:
        assert str(error) == "expected"

def test_strong():
    try:
        call()
        assert False
    except ValueError:
        assert True
''')
    findings = detect(source)
    assert len(findings) == 1
    assert findings[0]["test"] == "test_weak"
    assert "runtime proof required" in findings[0]["finding"]


def test_registry_is_pinned_and_commands_are_trusted(cases):
    assert len(cases) == 2
    for case in cases.values():
        assert len(case.baseline_sha) == 40
        assert case.command[:3] == ["python", "-m", "pytest"]
        assert set(case.allowed_paths).issubset(case.affected_paths)
        assert len(case.fingerprint) == 64


@pytest.mark.parametrize("kwargs", [
    {"mode": "LIVE", "github_repository": "apache/superset"},
    {"mode": "LIVE", "github_repository": "my/fork", "github_repository_id": 1},
    {"mode": "LIVE", "github_repository": "my/fork", "github_repository_id": 50, "github_webhook_secret": "short"},
    {"devin_max_acu": 0}, {"devin_max_acu": 21}, {"devin_max_acu": 1.5},
    {"case_issues": {"one": 1, "two": 1}},
])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)
