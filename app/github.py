import re
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.devin import RemoteError, SessionState, request


@dataclass(frozen=True)
class Candidate:
    number: int
    sha: str
    url: str | None = None


class GitHub:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", settings.github_repository):
            raise ValueError("GITHUB_REPOSITORY must be owner/repository")
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=f"https://api.github.com/repos/{settings.github_repository}/",
            headers={"Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            timeout=30, follow_redirects=False, trust_env=False,
        )

        self.client.headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"

    def candidate(self, number: int) -> Candidate:
        data = request(self.client, "GET", f"pulls/{number}")
        try:
            if (data["state"] != "open" or data.get("merged") or data.get("draft")
                    or data["base"]["ref"] != self.settings.base_branch
                    or data["base"]["repo"]["id"] != self.settings.github_repository_id
                    or data["head"]["repo"]["id"] != self.settings.github_repository_id):
                raise RemoteError("PR is not an open, non-draft candidate within the configured fork and target branch")
            sha = data["head"]["sha"]
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("Invalid SHA")
            return Candidate(number, sha, f"https://github.com/{self.settings.github_repository}/pull/{number}")
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteError("Invalid PR response") from exc

    def discover(self, state: SessionState) -> Candidate | None:
        pattern = rf"https://github\.com/{re.escape(self.settings.github_repository)}/pull/([1-9][0-9]*)/?"
        numbers = set()
        for pull in state.pull_requests:
            match = re.fullmatch(pattern, str(pull.get("pr_url", "")))
            if match:
                numbers.add(int(match[1]))
        if len(numbers) > 1:
            raise RemoteError("Multiple candidate PRs; manual selection required")
        return self.candidate(numbers.pop()) if numbers else None
