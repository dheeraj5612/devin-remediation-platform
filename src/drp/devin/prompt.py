"""Prompt and structured-output schema for the remediation session.

The prompt deliberately describes the *protected behaviour* and the remediation contract but
never discloses the mutant patch: a repaired test that merely targets one known patch would
be far weaker than one that asserts the behaviour the test exists to protect.
"""

from __future__ import annotations

from typing import Any

from drp.models import Finding, RemediationJob

STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "pr_url", "summary", "changed_files"],
    "properties": {
        "status": {"type": "string", "enum": ["pr_opened", "blocked", "not_a_weak_test"]},
        "pr_url": {"type": ["string", "null"], "description": "Candidate PR URL, if opened."},
        "summary": {"type": "string", "description": "What was asserted and why (<= 600 chars)."},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "blocked_reason": {"type": ["string", "null"]},
    },
}


def job_tag(job_id: str) -> str:
    return f"drp-job-{job_id}"


def build_prompt(finding: Finding, job: RemediationJob, *, feedback: str | None = None) -> str:
    test_name = finding.test_node_id.split("::", 1)[1]
    cmd = (
        " ".join(finding.test_command)
        .replace("{python}", "python")
        .replace("{test_file}", finding.test_file)
        .replace("{junit}", "/tmp/junit.xml")
    )
    allowed = "\n".join(f"  - `{p}`" for p in finding.allowed_paths)
    feedback_block = (
        f"""
## Feedback from the previous validation attempt (attempt {job.attempt - 1} was rejected)
{feedback.strip()}

Address this feedback in the SAME pull request branch (push additional commits).
"""
        if feedback
        else ""
    )
    return f"""You are repairing a false-green (weak) test in Apache Superset.

Repository: `{finding.repo}` (open the PR against branch `master` of this repository).
Pinned revision the finding was measured on: `{finding.source_sha}`.
Tracking issue: #{job.issue_number}. Remediation job id: `{job.id}`.

## The finding
Test: `{finding.test_node_id}`
Weakness kind: `{finding.weakness_kind}`

{finding.weakness_description.strip()}

## Behaviour this test is supposed to protect
{finding.protected_behavior.strip()}

## What you must do
1. Read the code under test and the current test to confirm the weakness.
2. Strengthen `{test_name}` so that it asserts the protected behaviour described above with
   concrete, deterministic assertions (values, dtypes, shapes, mutation-in-place, etc.).
   A regression that silently skips or garbles the work of the code under test must make
   the test FAIL. Do not merely assert that no exception is raised.
3. Keep the test's name `{test_name}` exactly. Do not skip, mark xfail, or delete it. You may
   add helper assertions or additional test functions in the same file.
4. Do not modify production code. Allowed paths (anything else will be rejected by the
   validator):
{allowed}
5. Run the file locally and make sure it passes on clean code:
   `{cmd}`
   Also run `pre-commit run --files {finding.test_file}` and fix formatting/lint findings.
6. Open ONE pull request against `{finding.repo}` `master` titled
   `test(utils): strengthen {test_name} to assert protected behaviour` whose description
   references `#{job.issue_number}` and explains which behaviours are now asserted.
7. Report via structured output: status `pr_opened` with the PR URL; if you conclude the test is
   not actually weak, report `not_a_weak_test` with your reasoning; if blocked, `blocked`.

## Non-negotiables
- The platform will independently validate your PR: it runs the repaired file on clean code
  (must pass) and against a pre-registered controlled regression of the code under test
  (must fail). Your own claims are not used as evidence, so be rigorous.
- Keep changes minimal and focused; follow AGENTS.md (type hints, Apache header already
  present, pre-commit).
{feedback_block}"""


def build_feedback(reason: str, evidence: dict[str, Any]) -> str:
    clean = evidence.get("clean_repaired") or {}
    mutant = evidence.get("mutant_repaired") or {}
    return (
        f"Validator verdict: REJECTED - {reason}\n"
        f"- repaired file on clean code: exit={clean.get('exit_code')} "
        f"passed={clean.get('passed')} failed={clean.get('failed')} errors={clean.get('errors')}\n"
        f"- repaired file with the controlled regression applied: exit={mutant.get('exit_code')} "
        f"passed={mutant.get('passed')} failed={mutant.get('failed')} errors={mutant.get('errors')}\n"  # noqa: E501
        "The regression breaks the protected behaviour described in the task; the test must "
        "assert that behaviour strongly enough to fail when it is broken."
    )
