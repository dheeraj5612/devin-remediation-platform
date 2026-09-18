"""Devin API v3 client (``/v3/organizations/{org_id}/sessions``) plus an in-memory fake used by
tests and offline demos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from drp.config import Settings

# v3 ``status`` values: new, claimed, running, exit, error, suspended, resuming
# v3 ``status_detail`` (while running): working, waiting_for_user, waiting_for_approval, finished
ACTIVE_STATUSES = frozenset({"new", "claimed", "running", "resuming"})
BLOCKED_DETAILS = frozenset({"waiting_for_user", "waiting_for_approval"})


@dataclass
class DevinSession:
    session_id: str
    url: str
    status: str
    status_detail: str | None = None
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES and self.status_detail not in BLOCKED_DETAILS

    @property
    def is_blocked(self) -> bool:
        return self.status_detail in BLOCKED_DETAILS or self.status == "suspended"

    @property
    def is_finished(self) -> bool:
        return self.status == "exit" or self.status_detail == "finished"

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @property
    def pr_urls(self) -> list[str]:
        urls = [str(p["pr_url"]) for p in self.pull_requests if p.get("pr_url")]
        so = self.structured_output or {}
        if isinstance(so.get("pr_url"), str) and so["pr_url"] not in urls:
            urls.append(so["pr_url"])
        return urls


def _parse(data: dict[str, Any]) -> DevinSession:
    return DevinSession(
        session_id=str(data["session_id"]),
        url=str(data.get("url", "")),
        status=str(data.get("status", "")),
        status_detail=data.get("status_detail"),
        pull_requests=list(data.get("pull_requests") or []),
        structured_output=data.get("structured_output"),
        tags=list(data.get("tags") or []),
        raw=data,
    )


class DevinClientProtocol(Protocol):
    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        tags: list[str],
        repos: list[str],
        structured_output_schema: dict[str, Any],
        max_acu_limit: int,
    ) -> DevinSession: ...

    def get_session(self, session_id: str) -> DevinSession: ...

    def find_session_by_tag(self, tag: str) -> DevinSession | None: ...

    def send_message(self, session_id: str, message: str) -> None: ...


class DevinClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.devin_api_key or not settings.devin_org_id:
            raise RuntimeError("DEVIN_API_KEY and DEVIN_ORG_ID are required for live Devin mode")
        self.org = settings.devin_org_id
        self._client = client or httpx.Client(base_url=settings.devin_api_url, timeout=60.0)
        self._client.headers["Authorization"] = f"Bearer {settings.devin_api_key}"
        self._client.headers["Content-Type"] = "application/json"

    def _base(self) -> str:
        return f"/v3/organizations/{self.org}/sessions"

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        tags: list[str],
        repos: list[str],
        structured_output_schema: dict[str, Any],
        max_acu_limit: int,
    ) -> DevinSession:
        body: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "repos": repos,
            "structured_output_schema": structured_output_schema,
            "structured_output_required": True,
            "max_acu_limit": max_acu_limit,
            "resumable": False,
        }
        resp = self._client.post(self._base(), json=body)
        resp.raise_for_status()
        return _parse(resp.json())

    def get_session(self, session_id: str) -> DevinSession:
        resp = self._client.get(f"{self._base()}/{session_id}")
        resp.raise_for_status()
        return _parse(resp.json())

    def find_session_by_tag(self, tag: str) -> DevinSession | None:
        resp = self._client.get(self._base(), params={"tags": [tag], "limit": 20})
        if resp.status_code >= 400:
            return None
        for item in resp.json().get("items", []):
            if tag in (item.get("tags") or []):
                return _parse(item)
        return None

    def send_message(self, session_id: str, message: str) -> None:
        resp = self._client.post(f"{self._base()}/{session_id}/messages", json={"message": message})
        resp.raise_for_status()


class FakeDevinClient:
    """Deterministic stand-in: sessions finish immediately with a configurable PR url."""

    def __init__(self, pr_url: str | None = None) -> None:
        self.pr_url = pr_url
        self.sessions: dict[str, DevinSession] = {}
        self.messages: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        tags: list[str],
        repos: list[str],
        structured_output_schema: dict[str, Any],
        max_acu_limit: int,
    ) -> DevinSession:
        sid = f"devin-fake-{len(self.sessions) + 1}"
        self.created.append({"prompt": prompt, "title": title, "tags": tags, "repos": repos})
        s = DevinSession(
            session_id=sid,
            url=f"https://app.devin.ai/sessions/{sid}",
            status="running",
            status_detail="working",
            tags=tags,
        )
        self.sessions[sid] = s
        return s

    def finish(self, session_id: str, pr_url: str | None = None) -> None:
        s = self.sessions[session_id]
        s.status, s.status_detail = "exit", "finished"
        url = pr_url or self.pr_url
        if url:
            s.pull_requests = [{"pr_url": url, "pr_state": "open"}]

    def get_session(self, session_id: str) -> DevinSession:
        return self.sessions[session_id]

    def find_session_by_tag(self, tag: str) -> DevinSession | None:
        return next((s for s in self.sessions.values() if tag in s.tags), None)

    def send_message(self, session_id: str, message: str) -> None:
        self.messages.append((session_id, message))
        self.sessions[session_id].status, self.sessions[session_id].status_detail = (
            "running",
            "working",
        )
