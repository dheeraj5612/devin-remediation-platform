from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: Literal["LIVE", "SIMULATION"] = "LIVE"
    data_dir: Path = Path("data")
    cases_file: Path = ROOT / "evals/cases.yaml"
    enable_live: bool = False
    github_repository: str = ""
    github_repository_id: int = 0
    github_webhook_secret: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    case_issues: dict[str, int] = Field(default_factory=dict)
    base_branch: str = "remediation-demo"
    devin_api_key: SecretStr = SecretStr("")
    devin_org_id: str = ""
    devin_max_acu: int = Field(default=3, ge=1, le=20)
    poll_seconds: float = Field(default=5, gt=0, le=60)
    job_timeout_seconds: int = Field(default=3600, ge=30, le=14400)
    superset_repo_path: Path = Path(".superset")
    superset_python: Path = Path(".superset/.venv/bin/python")
    superset_config_path: Path | None = None
    allow_local_validation: bool = False
    validation_timeout_seconds: int = Field(default=180, ge=1, le=600)

    @property
    def storage(self) -> Path:
        return self.data_dir.resolve() / self.mode.lower()

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.storage / 'jobs.sqlite'}"

    def live_errors(self) -> list[str]:
        missing = []
        for name in ("github_repository", "github_repository_id", "github_token", "github_webhook_secret",
                     "devin_api_key", "devin_org_id"):
            value = getattr(self, name)
            if not (value.get_secret_value() if isinstance(value, SecretStr) else value):
                missing.append(name.upper())
        if not self.enable_live:
            missing.append("ENABLE_LIVE=true")
        if self.github_repository.lower() == "apache/superset":
            missing.append("a dedicated fork, not Apache upstream")
        if not self.allow_local_validation:
            missing.append("ALLOW_LOCAL_VALIDATION=true (dedicated disposable environment)")
        return missing
