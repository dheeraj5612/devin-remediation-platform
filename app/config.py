"""Settings: every knob the platform reads, loaded from the environment / `.env`.

ELI5: this is the control panel. Nothing else in the app reads `os.environ`
directly; everything asks a `Settings` object instead, so a test can hand in
fake values and the live worker can refuse to start when something is missing.
"""

from pathlib import Path  # Build stable paths from the repository and data directory.
from typing import Literal  # Restrict the two supported operating modes.

from pydantic import Field, SecretStr  # Validate settings and keep credentials masked.
from pydantic_settings import BaseSettings, SettingsConfigDict  # Read typed values from `.env`.

# Repository root (the folder containing `app/`, `evals/`, `README.md`).
ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Typed control panel for simulation and live remediation runs.

    Inputs come from defaults, environment variables, or `.env`; properties derive paths,
    while :meth:`live_errors` explains which safety gates still block paid execution.
    """

    # Read `.env` if present; ignore unrelated variables instead of failing.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LIVE talks to GitHub/Devin/Superset for real; SIMULATION uses fakes and its own storage folder.
    mode: Literal["LIVE", "SIMULATION"] = "LIVE"
    data_dir: Path = Path("data")
    cases_file: Path = ROOT / "evals/cases.yaml"  # the trusted list of approved weak tests
    enable_live: bool = False  # extra "yes, really spend money" switch

    # GitHub: which fork we accept webhooks from, and the credentials to read its PRs.
    github_repository: str = ""  # "owner/name"
    github_repository_id: int = 0  # numeric ID, checked too so a renamed repo can't impersonate
    github_webhook_secret: SecretStr = SecretStr("")  # HMAC key GitHub signs deliveries with
    github_token: SecretStr = SecretStr("")
    case_issues: dict[str, int] = Field(default_factory=dict)  # case id -> approved issue number
    base_branch: str = "remediation-demo"  # branch Devin's PR must target

    # Devin: API credentials and a hard spend cap per session.
    devin_api_key: SecretStr = SecretStr("")
    devin_org_id: str = ""
    devin_max_acu: int = Field(default=3, ge=1, le=20)
    poll_seconds: float = Field(default=5, gt=0, le=60)  # how often the worker checks on a job
    job_timeout_seconds: int = Field(default=3600, ge=30, le=14400)  # give up (escalate) after this

    # Local Superset checkout used by the validator. SecretStr not needed; paths are not secrets.
    superset_repo_path: Path = Path(".superset")
    superset_python: Path = Path(".superset/.venv/bin/python")  # interpreter with Superset deps installed
    superset_config_path: Path | None = None
    allow_local_validation: bool = False  # candidate test code runs here; must be a disposable box
    validation_timeout_seconds: int = Field(default=180, ge=1, le=600)

    @property
    def storage(self) -> Path:
        """Return the mode-specific directory where durable evidence is stored."""

        # LIVE and SIMULATION never share a folder, so a demo can't pollute real evidence.
        return self.data_dir.resolve() / self.mode.lower()

    @property
    def database_url(self) -> str:
        """Return the SQLite URL used by this mode's job store."""

        return f"sqlite:///{self.storage / 'jobs.sqlite'}"

    def live_errors(self) -> list[str]:
        """Return the safety gates that still block live, paid execution.

        An empty list means configuration is complete enough for the caller to continue.
        The method reports names and policy requirements only; it never exposes secret values.
        """

        missing = []  # Collect every problem so the operator can fix them together.
        for name in ("github_repository", "github_repository_id", "github_token", "github_webhook_secret",
                     "devin_api_key", "devin_org_id"):
            value = getattr(self, name)  # Read one setting without duplicating the credential checks.
            if not (value.get_secret_value() if isinstance(value, SecretStr) else value):
                missing.append(name.upper())  # Report the setting name, never its value.
        if not self.enable_live:
            missing.append("ENABLE_LIVE=true")  # Require an explicit second confirmation before spending.
        if self.github_repository.lower() == "apache/superset":
            missing.append("a dedicated fork, not Apache upstream")  # Never point the bot at upstream.
        if not self.allow_local_validation:
            missing.append("ALLOW_LOCAL_VALIDATION=true (dedicated disposable environment)")  # Candidate code runs locally.
        return missing  # The caller decides whether to stop, while preserving all diagnostics.
