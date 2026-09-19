from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    mode: Literal["LIVE", "SIMULATION"] = "SIMULATION"
    data_dir: Path = Path("data")
    run_live: bool = False
    github_token: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("local-simulation-only")
    github_repository: str = Field(default="example/superset", pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    github_repository_id: int = 1
    github_base_branch: str = "remediation-demo"
    case_issues: dict[str, int] = Field(default_factory=dict)
    devin_api_key: SecretStr = SecretStr("")
    devin_org_id: str = ""
    devin_max_acu: int = Field(default=5, gt=0, le=20)
    poll_seconds: float = Field(default=10, ge=0.1)
    job_timeout_seconds: int = Field(default=3600, ge=30)
    validation_timeout_seconds: int = Field(default=300, ge=10, le=900)
    evaluator_image: str = Field(default="remediation-superset-eval:baseline", pattern=r"^[A-Za-z0-9./:_@-]+$")

    @model_validator(mode="after")
    def live_boundaries(self) -> "Settings":
        if self.mode == "LIVE":
            if self.github_repository in {"apache/superset", "example/superset"}:
                raise ValueError("LIVE requires your Superset fork, never apache/superset")
            if self.github_repository_id <= 1:
                raise ValueError("LIVE requires the numeric repository ID of your fork")
            if len(self.github_webhook_secret.get_secret_value()) < 24:
                raise ValueError("Use a random GitHub webhook secret of at least 24 characters")
        if len(set(self.case_issues.values())) != len(self.case_issues):
            raise ValueError("Each approved case must map to a different issue")
        if any(number <= 0 for number in self.case_issues.values()):
            raise ValueError("Issue numbers must be positive")
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / f"{self.mode.lower()}.db"

    def require_live(self) -> None:
        if self.mode != "LIVE" or not self.run_live:
            raise ValueError("Live execution is disabled; explicitly set MODE=LIVE and RUN_LIVE=true")
        if not self.devin_api_key.get_secret_value() or not self.devin_org_id:
            raise ValueError("DEVIN_API_KEY and DEVIN_ORG_ID are required")
        if not self.github_token.get_secret_value():
            raise ValueError("GITHUB_TOKEN is required")
