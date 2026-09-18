"""Runtime configuration, loaded from environment variables (prefix ``DRP_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRP_", env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite:///data/drp.sqlite3"
    artifacts_dir: Path = Path("data/artifacts")
    findings_dir: Path = Path("findings")

    # Target repository (the Superset fork Devin will open PRs against)
    target_repo: str = "dheeraj5612/superset"
    target_default_branch: str = "master"
    target_checkout: Path = Path("/home/ubuntu/repos/superset")
    target_python: Path = Path("/home/ubuntu/repos/superset/.venv/bin/python")
    target_package: str = "superset"  # import provenance is checked for this package

    # GitHub
    github_token: str = Field(default="", validation_alias="GITHUB_DHEERAJ_PAT")
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str = ""
    trigger_label: str = "devin-remediate"

    # Devin API (v3)
    devin_api_url: str = "https://api.devin.ai"
    devin_api_key: str = Field(default="", validation_alias="DEVIN_API_KEY")
    devin_org_id: str = Field(default="", validation_alias="DEVIN_ORG_ID")
    devin_mode: str = "live"  # live | fake
    devin_max_acu: int = 10
    devin_poll_interval_s: float = 30.0
    devin_session_timeout_s: float = 3 * 60 * 60

    # Worker
    worker_poll_interval_s: float = 5.0
    lease_seconds: int = 900
    max_remediation_attempts: int = 2
    test_timeout_s: int = 1800

    # Web
    public_base_url: str = "http://localhost:8000"

    def ensure_dirs(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            db_path = Path(self.database_url.removeprefix("sqlite:///"))
            db_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
