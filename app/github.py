"""GitHub REST adapter: find Devin's PR and pin down the exact commit we will judge.

ELI5: Devin says "I opened PR #4". We don't trust that; we ask GitHub for PR #4
ourselves, check it targets our fork + branch, is open and not a draft, and
record its head commit SHA. The validator then tests *that SHA*, so a later
push can't swap the code out from under a green verdict.
"""

import re
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.devin import RemoteError, SessionState, http_client, request


# ELI5: this immutable record pins the PR number, URL, and exact commit judged by validation.
@dataclass(frozen=True)
class Candidate:
    """Immutable GitHub identity for the pull request commit under evaluation."""

    # ELI5: retain the GitHub pull-request number for later refreshes.
    number: int
    # ELI5: retain the 40-character commit identity that validation must test.
    sha: str
    # ELI5: keep a display link when GitHub provided one.
    url: str | None = None


# ELI5: this adapter is the only module that accepts GitHub PR metadata.
class GitHub:
    """Read and validate candidate pull requests from the configured fork."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        """Build a GitHub client locked to the configured repository identity."""
        # ELI5: reject malformed repository names before constructing an authenticated client.
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", settings.github_repository):
            # ELI5: fail before a token can be used against an unintended repository URL.
            raise ValueError("GITHUB_REPOSITORY must be owner/repository")
        # ELI5: retain repository and branch policy for every future PR check.
        self.settings = settings
        # ELI5: unwrap the token only where the HTTP authorization header is made.
        token = settings.github_token.get_secret_value()
        # ELI5: reuse a test client or create a locked-down client scoped to this repository.
        self.client = client or http_client(
            f"https://api.github.com/repos/{settings.github_repository}/", token,
            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        # ELI5: ensure injected clients receive the same auth header as production clients.
        self.client.headers["Authorization"] = f"Bearer {token}"  # also covers an injected (test) client

    def candidate(self, number: int, target_branch: str | None = None) -> Candidate:
        """Fetch PR `number` and return its head SHA after checking its case-specific base branch."""
        # ELI5: validate the number before placing it in the API path, so malformed input cannot change that path.
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("PR number must be a positive integer")
        # ELI5: read the PR from GitHub instead of trusting Devin's reported URL or head.
        data = request(self.client, "GET", f"pulls/{number}")
        # A case can pin a different source branch when its baseline is not the global demo branch.
        expected_branch = target_branch if target_branch is not None else self.settings.base_branch
        # ELI5: all later checks use this one expected branch, so a valid PR cannot drift across cases.
        try:
            # ELI5: accept only an open, non-draft PR whose base and head repositories are our fork.
            if (data["state"] != "open" or data.get("merged") or data.get("draft")
                    or data["base"]["ref"] != expected_branch
                    or data["base"]["repo"]["id"] != self.settings.github_repository_id
                    or data["head"]["repo"]["id"] != self.settings.github_repository_id):  # no cross-fork heads
                # ELI5: reject a PR that is closed, draft, merged, cross-fork, or on another base branch.
                raise RemoteError("PR is not an open, non-draft candidate within the configured fork and target branch")
            # ELI5: extract the exact head commit instead of accepting a mutable branch name.
            sha = data["head"]["sha"]
            # ELI5: reject malformed commit identities before they reach the validator.
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                # ELI5: do not let a branch name or provider placeholder stand in for a commit.
                raise ValueError("Invalid SHA")
            # ELI5: return the immutable candidate identity used by every later state transition.
            return Candidate(number, sha, f"https://github.com/{self.settings.github_repository}/pull/{number}")
        except (KeyError, TypeError, ValueError) as exc:
            # ELI5: hide provider response details and fail closed on a shape mismatch.
            raise RemoteError("Invalid PR response") from exc

    def discover(self, state: SessionState, target_branch: str | None = None) -> Candidate | None:
        """Pick one reported PR in our repo and enforce the optional case-specific base branch."""
        # ELI5: build a strict URL pattern for this configured repository only.
        pattern = rf"https://github\.com/{re.escape(self.settings.github_repository)}/pull/([1-9][0-9]*)/?"
        # ELI5: a set deduplicates repeated provider references to the same PR.
        numbers = set()
        # ELI5: inspect every provider-reported PR link before choosing a candidate.
        for pull in state.pull_requests:
            # ELI5: parse only exact GitHub PR URLs and ignore unrelated links.
            match = re.fullmatch(pattern, str(pull.get("pr_url", "")))
            # ELI5: keep only URLs that matched the configured repository pattern.
            if match:
                # ELI5: keep only the numeric PR number from a trusted repository URL.
                numbers.add(int(match[1]))  # ELI5: convert it once for the later API lookup.
        # ELI5: multiple PRs need human selection instead of arbitrary automation.
        if len(numbers) > 1:
            # ELI5: refuse to select among competing provider claims automatically.
            raise RemoteError("Multiple candidate PRs; manual selection required")
        # ELI5: fetch and validate the single PR, or report that no candidate exists yet.
        return self.candidate(numbers.pop(), target_branch) if numbers else None
