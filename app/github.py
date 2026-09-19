import base64
import re
from dataclasses import dataclass

import httpx

from app.cases import Case
from app.config import Settings
from app.devin import ProviderError, Session
from app.models import Job


class ScopeError(Exception):
    pass


@dataclass
class Candidate:
    number: int
    url: str | None
    sha: str
    files: dict[str, str]


class GitHub:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=f"https://api.github.com/repos/{settings.github_repository}/",
            headers={"Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, timeout=30,
        )

    def get(self, path: str, **params):
        try:
            response = self.client.get(path, params=params)
        except httpx.TransportError as error:
            raise ProviderError("GitHub transport failure", retryable=True) from error
        if response.is_error:
            raise ProviderError(f"GitHub HTTP {response.status_code}", response.status_code == 429 or response.status_code >= 500)
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError("Malformed GitHub response") from error

    def discover(self, job: Job, session: Session, case: Case) -> Candidate | None:
        urls = [item.get("pr_url", "") for item in session.pull_requests]
        if session.structured_output and session.structured_output.get("pr_url"):
            urls.append(session.structured_output["pr_url"])
        if job.candidate_pr_number:
            urls = [f"https://github.com/{job.repository}/pull/{job.candidate_pr_number}"]
        if not urls:
            return None
        numbers = set()
        for url in urls:
            match = re.fullmatch(rf"https://github\.com/{re.escape(job.repository)}/pull/([1-9][0-9]*)", str(url))
            if not match:
                raise ScopeError("Session proposed a PR outside the configured fork")
            numbers.add(int(match[1]))
        if len(numbers) != 1:
            raise ScopeError("Expected exactly one candidate PR")
        number = numbers.pop()
        pr = self.get(f"pulls/{number}")
        try:
            valid = (
                pr["state"] == "open" and not pr["draft"] and
                pr["base"]["repo"]["id"] == self.settings.github_repository_id and
                pr["head"]["repo"]["id"] == self.settings.github_repository_id and
                pr["base"]["ref"] == self.settings.github_base_branch and
                pr["base"]["sha"] == case.baseline_sha and
                pr["head"]["ref"] == f"remediation/{job.id}" and
                f"Remediation job: {job.id}" in (pr.get("body") or "")
            )
            sha = pr["head"]["sha"]
        except (KeyError, TypeError) as error:
            raise ScopeError("Incomplete candidate PR metadata") from error
        if not valid or not re.fullmatch("[0-9a-f]{40}", sha):
            raise ScopeError("Candidate PR identity, base, branch, or job marker does not match")
        comparison = self.get(f"compare/{case.baseline_sha}...{sha}")
        if comparison.get("merge_base_commit", {}).get("sha") != case.baseline_sha:
            raise ScopeError("Candidate does not descend from the pinned baseline")
        changed = comparison.get("files")
        if not isinstance(changed, list) or not 0 < len(changed) == pr.get("changed_files", 0) <= len(case.allowed_paths):
            raise ScopeError("Empty, incomplete, or overbroad changed-file listing")
        files = {}
        for item in changed:
            path = item.get("filename")
            if (path not in case.allowed_paths or item.get("status") != "modified" or
                    item.get("additions", 10000) + item.get("deletions", 10000) > 200):
                raise ScopeError("Change exceeds the allowed test-only scope or 200-line edit budget")
            tree_sha = self.get(f"git/commits/{sha}")["tree"]["sha"]
            parts = path.split("/")
            for index, part in enumerate(parts):
                tree = self.get(f"git/trees/{tree_sha}")
                entry = next((entry for entry in tree.get("tree", []) if entry["path"] == part), None)
                if tree.get("truncated") or not entry:
                    raise ScopeError("Incomplete candidate Git tree")
                expected_mode = "100644" if index == len(parts) - 1 else "040000"
                if entry.get("mode") != expected_mode:
                    raise ScopeError("Candidate must preserve regular test files and directories")
                tree_sha = entry["sha"]
            blob = self.get(f"git/blobs/{tree_sha}")
            if blob.get("encoding") != "base64" or blob.get("size", 100001) > 100000:
                raise ScopeError("Expected a small regular UTF-8 source file")
            try:
                files[path] = base64.b64decode(blob["content"].replace("\n", ""), validate=True).decode("utf-8")
            except (ValueError, KeyError, UnicodeError) as error:
                raise ScopeError("Invalid candidate file encoding") from error
        return Candidate(number, f"https://github.com/{job.repository}/pull/{number}", sha, files)

    def unchanged(self, candidate: Candidate) -> bool:
        pr = self.get(f"pulls/{candidate.number}")
        return pr.get("state") == "open" and pr.get("head", {}).get("sha") == candidate.sha
