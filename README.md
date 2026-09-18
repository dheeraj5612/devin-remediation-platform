# Devin Remediation Platform

An event-driven system that converts a **confirmed engineering-quality finding** into an
**independently validated candidate PR** using Devin.

Concrete finding type: **false-green / weak Apache Superset tests** — tests that pass without
enforcing the behavior they are meant to protect.

## Definition of success

A remediation is *verified* only when the platform itself has produced evidence that:

1. the repaired test **passes against correct (clean) code**, and
2. the repaired test **fails when the same pre-registered controlled regression** that escaped
   the original test is re-applied.

Both runs happen in isolated git worktrees driven by the platform, never by Devin. Devin's
PR description, structured output, or "I ran the tests" claims are orchestration metadata
only; they never influence the verdict.

## Pipeline

```
static finding (finding.yaml + mutant.patch)
   │  drp register        pin sha256(mutant.patch), store the finding
   ▼
deterministic baseline
   │  drp baseline        original test on clean source  -> must PASS
   │                      apply mutant in a fresh worktree, original test -> still PASSES
   │                      => CONFIRMED (regression escapes) | REFUTED | ERROR
   ▼
GitHub issue
   │  drp issue           issue body carries the finding marker + evidence + contracts
   ▼
`devin-remediate` label   (explicit human opt-in)
   ▼
GitHub webhook            HMAC-SHA256 verified, label/repo/marker/baseline gates,
   │  drp serve           idempotent on X-GitHub-Delivery
   ▼
durable RemediationJob    QUEUED -> SESSION_REQUESTED -> SESSION_RUNNING -> PR_READY -> VALIDATING
   │  drp worker          every transition is an audited JobEvent; leases reclaim crashed workers;
   │                      one active job per finding; exponential backoff then escalation
   ▼
Devin API (v3)            session tagged drp-job-<id>; prompt states the protected behavior and
   │                      the allowed paths but deliberately NOT the mutant
   ▼
candidate PR              must target the configured repo + default branch and be open
   ▼
independent validation    fetch PR head, enforce changed-file scope, require target test present
   │                      and not skipped, run clean (PASS) then with the SAME mutant (FAIL)
   ▼
VERIFIED | REJECTED | ESCALATED   -> issue + PR comments, dashboard, /api/metrics, /metrics
```

Verdict rules:

| situation | verdict |
| --- | --- |
| clean PASS and target test FAILS under mutant | **VERIFIED** |
| clean FAIL, or mutant still escapes | **REJECTED** (feedback is sent to the same Devin session; bounded by `max_remediation_attempts`) |
| PR touches files outside `allowed_paths` (e.g. production code), target test removed/renamed/skipped, mutant no longer applies, mutant hash mismatch | **ESCALATED** (a human decides) |
| Devin session errored / blocked / finished without a PR / timed out | **ESCALATED** |
| git or subprocess infrastructure failure | retried with backoff, then **ESCALATED** as infrastructure failure |

## First finding: `superset-normalize-dttm-edge-cases`

`tests/unit_tests/utils/test_date_parsing.py::test_edge_cases` calls
`superset.utils.core.normalize_dttm_col` on four edge-case frames and asserts nothing.

Controlled regression (`findings/superset-normalize-dttm-edge-cases/mutant.patch`):

```diff
 for _col in dttm_cols:
     if _col.col_label not in df.columns:
         continue
+    if len(df) < 2:
+        continue
```

Measured on Superset `2e1cf8a7`: original file passes 21/21 on clean code and 21/21 with the
regression applied. A strengthened probe that asserts the converted dtype fails under the
regression, which is exactly what a verified repair must do.

## Quick start

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

export DRP_TARGET_CHECKOUT=/path/to/superset          # clone of the target repo
export DRP_TARGET_PYTHON=/path/to/superset/.venv/bin/python
export DRP_TARGET_REPO=dheeraj5612/superset

drp register findings/superset-normalize-dttm-edge-cases
drp baseline superset-normalize-dttm-edge-cases        # -> confirmed
drp issue    superset-normalize-dttm-edge-cases        # needs GITHUB_DHEERAJ_PAT

# in two terminals
drp serve --port 8000                                   # webhook receiver + dashboard
drp worker                                              # needs DEVIN_API_KEY + DEVIN_ORG_ID

# offline: validate any ref against a finding without Devin
drp validate superset-normalize-dttm-edge-cases some-branch-or-sha
drp metrics
```

Point a GitHub webhook (event: `issues`, content type `application/json`, secret =
`DRP_GITHUB_WEBHOOK_SECRET`) at `https://<host>/webhooks/github`, then add the
`devin-remediate` label to the issue that `drp issue` opened.

### Configuration

All settings are environment variables prefixed with `DRP_` (see `src/drp/config.py`),
except the credentials `GITHUB_DHEERAJ_PAT`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`.

| variable | default | purpose |
| --- | --- | --- |
| `DRP_DATABASE_URL` | `sqlite:///data/drp.sqlite3` | durable job store (any SQLAlchemy URL) |
| `DRP_ARTIFACTS_DIR` | `data/artifacts` | JUnit XML, logs, worktrees |
| `DRP_TARGET_REPO` / `DRP_TARGET_DEFAULT_BRANCH` | `dheeraj5612/superset` / `master` | candidate PRs must target this |
| `DRP_TARGET_CHECKOUT` / `DRP_TARGET_PYTHON` | | local clone + interpreter used for validation |
| `DRP_TRIGGER_LABEL` | `devin-remediate` | opt-in label |
| `DRP_GITHUB_WEBHOOK_SECRET` | empty | HMAC secret (empty = not enforced; set it in production) |
| `DRP_DEVIN_MODE` | `live` | `fake` uses an in-memory Devin for demos/tests |
| `DRP_DEVIN_MAX_ACU` | `10` | ACU cap per session |
| `DRP_MAX_REMEDIATION_ATTEMPTS` | `2` | rejected candidates get feedback until this is reached |
| `DRP_LEASE_SECONDS` | `900` | worker lease; expired leases are reclaimed |

## Layout

```
findings/<id>/finding.yaml, mutant.patch   pre-registered findings + controlled regressions
src/drp/
  findings/scanner.py   AST scan: no_assertion, assert_true, swallowed_exception
  findings/spec.py      finding.yaml schema, registration, mutant sha256 pinning
  baseline.py           clean + mutant baseline for the ORIGINAL test
  validator/gitops.py   git worktrees, PR head fetch, patch check/apply, changed files
  validator/runner.py   deterministic pytest runs, JUnit parsing, env + import provenance
  validator/validate.py candidate validation (scope, presence, clean pass, mutant fail)
  github/               REST client, issue rendering, webhook (HMAC, gates, idempotency)
  devin/                v3 API client + FakeDevinClient, prompt / feedback / structured output
  jobs.py               state machine, JobEvent audit, leases, retry/backoff
  worker.py             drives a job: session -> poll -> PR -> validate -> verdict -> comments
  metrics.py            rates, medians, queue age; JSON + Prometheus
  web/                  FastAPI: /webhooks/github, /api/*, /metrics, dashboard pages
  cli.py                drp scan|register|baseline|issue|serve|worker|validate|metrics
tests/                  runs the whole pipeline against a miniature git repo (no Superset needed)
```

## Development

```bash
pytest -q                 # ~30 tests: real git + pytest on a fixture repo, fake Devin/GitHub
ruff check src tests && ruff format --check src tests
mypy                      # strict
```

The test fixture (`tests/conftest.py`) builds a tiny `mathlib` package with the same shape as
the Superset finding: a weak test, a mutant that escapes it, and a strong repair that catches
it. Worker tests push PR heads to a local bare remote as `refs/pull/N/head`, so the fetch and
validation path is the real one.
