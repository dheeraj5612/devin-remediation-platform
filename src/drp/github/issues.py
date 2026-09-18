"""Stage 4: render and open the GitHub issue for a confirmed finding."""

from __future__ import annotations

from typing import Any

from drp.config import Settings
from drp.github.client import GitHubClient, finding_marker
from drp.models import BaselineStatus, Finding

FINDING_LABEL = "weak-test"


def _run_summary(run: dict[str, Any] | None) -> str:
    if not run:
        return "n/a"
    return (
        f"exit={run['exit_code']} total={run['total']} passed={run['passed']} "
        f"failed={run['failed']} errors={run['errors']} skipped={run['skipped']}"
    )


def render_issue(finding: Finding, settings: Settings) -> tuple[str, str]:
    ev = finding.baseline_evidence or {}
    title = f"[weak test] {finding.test_node_id.split('::', 1)[1]} is false-green"
    body = f"""{finding_marker(finding.id)}
## Finding: `{finding.id}`

**Test:** `{finding.test_node_id}`
**Kind:** `{finding.weakness_kind}`
**Pinned revision:** `{finding.source_sha}`

### Why the test is weak
{finding.weakness_description.strip()}

### Behaviour the test is supposed to protect
{finding.protected_behavior.strip()}

### Deterministic baseline (independently measured)
| run | result |
| --- | --- |
| original test file on clean `{finding.source_sha[:12]}` | {_run_summary(ev.get("clean_original"))} |
| original test file with controlled regression applied | {_run_summary(ev.get("mutant_original"))} |

The controlled regression (sha256 `{finding.mutant_sha256}`) is pre-registered by the
platform and is **not** disclosed here on purpose: the repaired test must assert the
protected behaviour, not target one specific patch. Regression summary:
{finding.mutant_description.strip()}

Reproduce the clean baseline:
```bash
git checkout {finding.source_sha}
{" ".join(finding.test_command).replace("{python}", "python").replace("{test_file}", finding.test_file).replace("{junit}", "junit.xml")}
```

### Remediation contract
* Only `{finding.test_file}` may change (allowed paths: {", ".join(f"`{p}`" for p in finding.allowed_paths)}).
* `{finding.test_node_id.split("::", 1)[1]}` must keep its name and must not be skipped.
* The repaired test must pass on clean code and must **fail** when the protected
  behaviour is broken.

### How this issue is processed
Add the label **`{settings.trigger_label}`** to opt in. The platform will start a Devin
session, wait for the candidate PR, then independently validate it: the repaired test is
run against clean code (expect pass) and against the exact pre-registered regression
(expect fail). Results are posted back here.
"""  # noqa: E501
    return title, body


def open_issue(finding: Finding, settings: Settings, gh: GitHubClient) -> Finding:
    if finding.baseline_status != BaselineStatus.CONFIRMED:
        raise ValueError(
            f"finding {finding.id} baseline is {finding.baseline_status.value}, not confirmed"
        )
    if finding.github_issue_number:
        return finding
    gh.ensure_label(finding.repo, FINDING_LABEL, "d93f0b", "Weak / false-green test finding")
    gh.ensure_label(
        finding.repo,
        settings.trigger_label,
        "0e8a16",
        "Opt-in: let the Devin Remediation Platform repair this finding",
    )
    title, body = render_issue(finding, settings)
    issue = gh.create_issue(finding.repo, title, body, [FINDING_LABEL])
    finding.github_issue_number = int(issue["number"])
    finding.github_issue_url = str(issue["html_url"])
    return finding
