"""Minimal GitHub REST client (issues, comments, pull requests)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from drp.config import Settings

FINDING_MARKER = "<!-- drp:finding_id="


def finding_marker(finding_id: str) -> str:
    return f"{FINDING_MARKER}{finding_id} -->"


def parse_finding_marker(body: str | None) -> str | None:
    if not body or FINDING_MARKER not in body:
        return None
    start = body.index(FINDING_MARKER) + len(FINDING_MARKER)
    end = body.find(" -->", start)
    return body[start:end].strip() if end != -1 else None


@dataclass
class PullRequest:
    number: int
    url: str
    head_sha: str
    head_ref: str
    base_ref: str
    base_repo: str
    head_repo: str
    state: str
    draft: bool


class GitHubClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devin-remediation-platform",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._client = client or httpx.Client(
            base_url=settings.github_api_url, headers=headers, timeout=30.0
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    # Issues -----------------------------------------------------------------
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        data: dict[str, Any] = self._request(
            "POST", f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": labels}
        )
        return data

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        data: dict[str, Any] = self._request("GET", f"/repos/{repo}/issues/{number}")
        return data

    def comment(self, repo: str, number: int, body: str) -> None:
        self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._request("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        resp = self._client.get(f"/repos/{repo}/labels/{name}")
        if resp.status_code == 404:
            self._request(
                "POST",
                f"/repos/{repo}/labels",
                json={"name": name, "color": color, "description": description},
            )
        else:
            resp.raise_for_status()

    # Pull requests ----------------------------------------------------------
    def get_pull(self, repo: str, number: int) -> PullRequest:
        d: dict[str, Any] = self._request("GET", f"/repos/{repo}/pulls/{number}")
        return PullRequest(
            number=d["number"],
            url=d["html_url"],
            head_sha=d["head"]["sha"],
            head_ref=d["head"]["ref"],
            base_ref=d["base"]["ref"],
            base_repo=d["base"]["repo"]["full_name"],
            head_repo=(d["head"]["repo"] or {}).get("full_name", ""),
            state=d["state"],
            draft=bool(d.get("draft")),
        )

    def find_open_pulls_for_issue(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        """Fallback PR discovery: open PRs whose body references ``#<issue>``."""
        pulls: list[dict[str, Any]] = self._request(
            "GET", f"/repos/{repo}/pulls", params={"state": "open", "per_page": 50}
        )
        needle = f"#{issue_number}"
        return [p for p in pulls if needle in (p.get("body") or "") or needle in p.get("title", "")]


def parse_pr_url(url: str) -> tuple[str, int]:
    """``https://github.com/owner/repo/pull/123`` -> (``owner/repo``, 123)."""
    parts = url.rstrip("/").split("/")
    if len(parts) < 7 or parts[-2] != "pull":
        raise ValueError(f"not a pull request url: {url}")
    return f"{parts[-4]}/{parts[-3]}", int(parts[-1])
