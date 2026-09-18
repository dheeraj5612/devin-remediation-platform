"""Worker state machine end-to-end (fake Devin + fake GitHub, real git + pytest) and the
Devin v3 HTTP client against a mocked transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from drp import db
from drp.config import Settings
from drp.devin.client import DevinClient, FakeDevinClient
from drp.devin.prompt import build_prompt, job_tag
from drp.github.client import GitHubClient, PullRequest
from drp.jobs import create_job
from drp.models import BaselineStatus, Finding, JobState, RemediationJob, ValidationRun, Verdict
from drp.worker import Worker
from tests.conftest import STILL_WEAK_TEST, STRONG_TEST, FixtureRepo, git


class FakeGitHub(GitHubClient):
    """Serves PRs from a local bare remote and records comments; never touches the network."""

    def __init__(self, settings: Settings, repo: FixtureRepo) -> None:
        super().__init__(settings, client=httpx.Client(base_url="http://invalid.test"))
        self.repo = repo
        self.pulls: dict[int, PullRequest] = {}
        self.comments: list[tuple[int, str]] = []

    def open_pull(self, number: int, sha: str, *, base_ref: str = "master") -> str:
        git(self.repo.path, "push", "-qf", "origin", f"{sha}:refs/pull/{number}/head")
        url = f"https://github.com/{self.settings.target_repo}/pull/{number}"
        self.pulls[number] = PullRequest(
            number=number,
            url=url,
            head_sha=sha,
            head_ref=f"fix-{number}",
            base_ref=base_ref,
            base_repo=self.settings.target_repo,
            head_repo=self.settings.target_repo,
            state="open",
            draft=False,
        )
        return url

    def get_pull(self, repo: str, number: int) -> PullRequest:
        return self.pulls[number]

    def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))


@pytest.fixture
def remote_repo(fixture_repo: FixtureRepo, tmp_path: Path) -> FixtureRepo:
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(fixture_repo.path, "remote", "add", "origin", str(bare))
    git(fixture_repo.path, "push", "-q", "origin", "master")
    return fixture_repo


@pytest.fixture
def queued_job(finding: Finding, db_session: Session) -> str:
    finding.baseline_status = BaselineStatus.CONFIRMED
    finding.github_issue_number = 7
    job, _ = create_job(db_session, finding, issue_number=7, delivery_id="d", triggered_by="u")
    db_session.commit()
    return job.id


def _job(job_id: str) -> RemediationJob:
    with db.session_scope() as s:
        j = s.get(RemediationJob, job_id)
        assert j is not None
        s.expunge(j)
        return j


def test_worker_verifies_strong_repair_end_to_end(
    settings: Settings, remote_repo: FixtureRepo, queued_job: str
) -> None:
    devin = FakeDevinClient()
    gh = FakeGitHub(settings, remote_repo)
    w = Worker(settings, devin=devin, github=gh, owner="w1")

    assert w.run_once()  # QUEUED -> SESSION_RUNNING (session created)
    j = _job(queued_job)
    assert j.state == JobState.SESSION_RUNNING and j.devin_session_id == "devin-fake-1"
    created = devin.created[0]
    assert job_tag(queued_job) in created["tags"] and created["repos"] == [settings.target_repo]
    assert "len(values) < 2" not in created["prompt"]  # mutant is never disclosed to Devin

    assert w.run_once()  # still running, no PR -> stays SESSION_RUNNING
    assert _job(queued_job).state == JobState.SESSION_RUNNING

    sha = remote_repo.commit_test(STRONG_TEST, "devin-fix")
    devin.finish("devin-fake-1", gh.open_pull(42, sha))
    assert w.run_once()  # PR discovered -> PR_READY
    j = _job(queued_job)
    assert j.state == JobState.PR_READY and j.pr_number == 42 and j.pr_head_sha == sha

    assert w.run_once()  # VALIDATING -> VERIFIED
    j = _job(queued_job)
    assert j.state == JobState.VERIFIED, j.outcome_reason
    with db.session_scope() as s:
        run = s.scalars(select(ValidationRun)).one()
        assert run.verdict == Verdict.VERIFIED
        assert run.clean_passed is True and run.mutant_detected is True
        assert run.mutant_sha256 == s.get(Finding, j.finding_id).mutant_sha256  # type: ignore[union-attr]
    assert {n for n, _ in gh.comments} == {7, 42}
    assert (
        "**VERIFIED**" in gh.comments[-1][1] and "pre-registered regression" in gh.comments[-1][1]
    )
    assert not w.run_once()  # terminal: nothing left to claim


def test_worker_sends_feedback_then_rejects_after_max_attempts(
    settings: Settings, remote_repo: FixtureRepo, queued_job: str
) -> None:
    devin = FakeDevinClient()
    gh = FakeGitHub(settings, remote_repo)
    w = Worker(settings, devin=devin, github=gh, owner="w1")
    w.run_once()
    sha1 = remote_repo.commit_test(STILL_WEAK_TEST, "weak-1")
    devin.finish("devin-fake-1", gh.open_pull(42, sha1))
    w.run_once()  # PR_READY
    w.run_once()  # rejected -> feedback -> SESSION_RUNNING, attempt 2
    j = _job(queued_job)
    assert j.state == JobState.SESSION_RUNNING and j.attempt == 2
    assert len(devin.messages) == 1 and "controlled regression" in devin.messages[0][1]

    # Devin pushes a new commit that is still weak
    sha2 = remote_repo.commit_test(STILL_WEAK_TEST + "\n# retry\n", "weak-2")
    devin.finish("devin-fake-1", gh.open_pull(42, sha2))
    w.run_once()  # PR_READY with new head
    assert _job(queued_job).pr_head_sha == sha2
    w.run_once()
    j = _job(queued_job)
    assert j.state == JobState.REJECTED
    with db.session_scope() as s:
        assert len(s.scalars(select(ValidationRun)).all()) == 2


def test_worker_escalates_when_session_finishes_without_pr(
    settings: Settings, remote_repo: FixtureRepo, queued_job: str
) -> None:
    devin = FakeDevinClient()
    w = Worker(settings, devin=devin, github=FakeGitHub(settings, remote_repo), owner="w1")
    w.run_once()
    devin.finish("devin-fake-1")
    devin.sessions["devin-fake-1"].structured_output = {"status": "blocked", "blocked_reason": "x"}
    w.run_once()
    j = _job(queued_job)
    assert j.state == JobState.ESCALATED and "without a candidate PR" in (j.outcome_reason or "")


def test_worker_ignores_prs_against_other_repos_or_branches(
    settings: Settings, remote_repo: FixtureRepo, queued_job: str
) -> None:
    devin = FakeDevinClient()
    gh = FakeGitHub(settings, remote_repo)
    w = Worker(settings, devin=devin, github=gh, owner="w1")
    w.run_once()
    sha = remote_repo.commit_test(STRONG_TEST, "wrong-base")
    gh.open_pull(5, sha, base_ref="release-1.0")
    devin.sessions["devin-fake-1"].pull_requests = [
        {"pr_url": "https://github.com/other/repo/pull/1"},
        {"pr_url": f"https://github.com/{settings.target_repo}/pull/5"},
    ]
    w.run_once()
    assert _job(queued_job).state == JobState.SESSION_RUNNING


def test_worker_reuses_existing_session_by_tag(
    settings: Settings, remote_repo: FixtureRepo, queued_job: str
) -> None:
    devin = FakeDevinClient()
    w = Worker(settings, devin=devin, github=FakeGitHub(settings, remote_repo), owner="w1")
    with db.session_scope() as s:
        job = s.get(RemediationJob, queued_job)
        assert job is not None
        finding = job.finding
        devin.create_session(
            prompt=build_prompt(finding, job),
            title="t",
            tags=[job_tag(queued_job)],
            repos=[finding.repo],
            structured_output_schema={},
            max_acu_limit=1,
        )
    w.run_once()
    assert len(devin.created) == 1
    assert _job(queued_job).devin_session_id == "devin-fake-1"


def test_worker_records_infrastructure_errors_with_backoff(
    settings: Settings, remote_repo: FixtureRepo, queued_job: str
) -> None:
    class Exploding(FakeDevinClient):
        def create_session(self, **kwargs: Any) -> Any:
            raise httpx.ConnectError("devin down")

    w = Worker(settings, devin=Exploding(), github=FakeGitHub(settings, remote_repo), owner="w1")
    assert w.run_once()
    j = _job(queued_job)
    assert j.state == JobState.SESSION_REQUESTED and j.error_count == 1
    assert "devin down" in (j.last_error or "")
    assert j.lease_owner is None  # released so another worker may retry after backoff


# ---------------------------------------------------------------- Devin HTTP client


def _client(handler: Any, settings: Settings) -> DevinClient:
    live = settings.model_copy(update={"devin_api_key": "k-test", "devin_org_id": "org-test"})
    return DevinClient(
        live,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"),
    )


def test_devin_client_create_get_find_and_message(settings: Settings) -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append(
            (
                req.method,
                req.url.path + ("?" + req.url.query.decode() if req.url.query else ""),
                body,
            )
        )
        assert req.headers["authorization"] == "Bearer k-test"
        if req.method == "POST" and req.url.path.endswith("/sessions"):
            return httpx.Response(
                200,
                json={
                    "session_id": "s1",
                    "url": "https://app.devin.ai/sessions/s1",
                    "status": "new",
                },
            )
        if req.method == "GET" and req.url.path.endswith("/sessions/s1"):
            return httpx.Response(
                200,
                json={
                    "session_id": "s1",
                    "status": "exit",
                    "status_detail": "finished",
                    "pull_requests": [{"pr_url": "https://github.com/a/b/pull/3"}],
                    "structured_output": {
                        "status": "pr_opened",
                        "pr_url": "https://github.com/a/b/pull/3",
                    },
                    "tags": ["x"],
                },
            )
        if req.method == "GET" and req.url.path.endswith("/sessions"):
            return httpx.Response(
                200, json={"items": [{"session_id": "s9", "tags": ["drp-job-1"]}]}
            )
        if req.method == "POST" and req.url.path.endswith("/messages"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    c = _client(handler, settings)
    s = c.create_session(
        prompt="p",
        title="t",
        tags=["a"],
        repos=["a/b"],
        structured_output_schema={},
        max_acu_limit=3,
    )
    assert s.session_id == "s1" and s.is_active
    base = "/v3/organizations/org-test/sessions"
    assert calls[0][0:2] == ("POST", base)
    assert calls[0][2]["max_acu_limit"] == 3 and calls[0][2]["structured_output_required"] is True

    got = c.get_session("s1")
    assert got.is_finished and not got.is_active
    assert got.pr_urls == ["https://github.com/a/b/pull/3"]  # deduplicated across sources

    assert c.find_session_by_tag("drp-job-1") is not None
    assert c.find_session_by_tag("nope") is None
    c.send_message("s1", "hello")
    assert calls[-1] == ("POST", f"{base}/s1/messages", {"message": "hello"})


def test_devin_client_raises_on_http_error(settings: Settings) -> None:
    c = _client(lambda req: httpx.Response(500, json={"detail": "boom"}), settings)
    with pytest.raises(httpx.HTTPStatusError):
        c.get_session("s1")
    assert c.find_session_by_tag("t") is None  # lookup failures degrade to "not found"


def test_devin_session_status_semantics() -> None:
    from drp.devin.client import DevinSession

    running = DevinSession("s", "u", "running", "working")
    blocked = DevinSession("s", "u", "running", "waiting_for_user")
    suspended = DevinSession("s", "u", "suspended", None)
    errored = DevinSession("s", "u", "error", None)
    assert running.is_active and not running.is_blocked
    assert blocked.is_blocked and not blocked.is_active
    assert suspended.is_blocked
    assert errored.is_error and not errored.is_active
