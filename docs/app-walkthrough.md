# DevinTrace in plain English

DevinTrace helps a human ask Devin to repair one approved Superset issue and
then checks the result with a separate validator. Think of it as a small
mailroom:

1. A human labels one registered issue with `devin-remediate`.
2. The signed GitHub event creates a durable job in SQLite.
3. One worker gives Devin one bounded session.
4. Devin returns a candidate pull request.
5. GitHub and the validator inspect the exact commit.
6. Only the validator can mark the job `VERIFIED`.

The browser is a read-only window into this process. It cannot enqueue a job,
merge a pull request, or approve a repair.

For implementation details, payloads, retries, and state transitions, see the
[technical reference](technical-reference.md). Main code: [app/main.py](../app/main.py),
[app/orchestrator.py](../app/orchestrator.py), [app/devin.py](../app/devin.py),
[app/github.py](../app/github.py), and [app/validator.py](../app/validator.py).

## Three kinds of evidence

Keep these three things separate:

| Evidence | What it proves | What it does not prove |
| --- | --- | --- |
| `SIMULATION` | The workflow, retries, duplicates, recovery, and pages work with fake external services. | No real Devin run, GitHub run, customer result, or success rate. |
| Captured live evidence | One historical live candidate was observed and checked. | A current run, a merge, customer impact, or cost savings. |
| Current `/cases` readiness | Whether this checkout can safely admit a new live job now. | That an older archived job is still current. |

The current evaluator fingerprint differs from the archived baseline. Therefore
current live admission is blocked until baseline evidence is regenerated. The
historical `PASS` remains useful evidence about that recorded run, but it does
not make the current checkout live-ready.

## The simple flow

~~~mermaid
flowchart LR
    H[Approved issue] --> W[Signed webhook]
    W --> S[SQLite job]
    S --> D[One Devin session]
    D --> P[Candidate PR]
    P --> R[GitHub read-back]
    R --> V[Detached exact-SHA validator]
    V --> Q[VERIFIED, FAILED, or ESCALATED]
~~~

The registered case travels with the job. It supplies the issue binding, target
branch, baseline commit, allowed paths, and acceptance rule. Issue text cannot
change those rules. The registry is [evals/cases.yaml](../evals/cases.yaml),
and the allow-list logic is [app/cases.py](../app/cases.py).

## What the browser shows

`/dashboard` shows read-only jobs, filters, counters, and attention states;
`/jobs/{job_id}` shows a verdict, exact SHAs, links, and its timeline.
`/cases`, `/evidence`, `/report.json`, `/healthz`, and `/metrics` provide
contracts, dated evidence, and matching JSON views.

An archive card is not in current metrics. A job page is evidence, not merge permission.

## The small set of APIs

| Caller and operation | Purpose |
| --- | --- |
| `POST /webhooks/github` | Verify a signed `issues` event, bind the issue to a registered case, and durably return `202` with `job_id`, `status`, and `duplicate`. |
| Devin `POST /sessions` | Start one bounded session with the trusted case prompt and evidence attachment. |
| Devin `GET /sessions/{session_id}` | Poll the same session and resume it after a worker restart. |
| Devin `GET /sessions?first=100` | Recover a lost launch by searching for the exact `drp-{job_id}` tag. |
| Devin `POST /sessions/{session_id}/messages` | Send at most one correction to the same session after a genuine validation failure. |
| GitHub `GET /repos/{repo}/pulls/{number}` | Read back the PR and require the expected repository, base branch, open state, non-draft state, unmerged state, and 40-character head SHA. |
| Git ref `refs/pull/{number}/head` | Fetch the PR head before validation so the validator works on the exact commit read back from GitHub. |

The clients are in [app/devin.py](../app/devin.py) and
[app/github.py](../app/github.py); the webhook and HMAC checks are in
[app/main.py](../app/main.py). The webhook uses signed `issues` headers,
including `X-Hub-Signature-256` and `X-GitHub-Delivery`.

The HMAC covers the original bytes before JSON parsing. The repository, issue,
label, and case binding must all match. A duplicate delivery returns the
original job instead of launching another one.

## Worked example: issue 7 to PR 8

Issue 7 is registered as `import-unparseable-yaml`. It targets
`dheeraj5612/superset`, the `remediation-import-yaml` branch, and baseline
`dedfe23a805151decca6deaf15b032e040e42e82`. Devin may edit only:

~~~text
superset/commands/importers/v1/utils.py
~~~

The trusted application oracle is
[import_unparseable_yaml.py](../evals/application_cases/import_unparseable_yaml.py).

The recorded sequence was label, signed admission, one Devin session, and
[PR #8](https://github.com/dheeraj5612/superset/pull/8). GitHub read-back
recorded head SHA `5533125a2056ca4d8cd344a69d4eec575b1db786`; the detached
validator checked that exact SHA, returned `PASS`, and the job became `VERIFIED`.

[evidence/live-application.json](../evidence/live-application.json) records
that candidate and its matching validated SHA. At capture, PR 8 was open and
unmerged. It is historical application-oracle evidence, not a merge claim.

The local comparator in
[evidence/application-oracle.json](../evidence/application-oracle.json) shows
the pinned baseline as `REGRESSION` and a known reference fix as `PASS`. That
comparison used no Devin call and is separate from PR 8.

## What validation means

Before running candidate code, the validator requires a descendant of the
pinned baseline, registered paths only, regular tracked files, no adds/deletes/
renames, and no more than 200 changed lines.

Then it creates a fresh detached worktree at the exact candidate SHA. For the
issue 7 oracle, it loads a valid YAML mapping, then loads malformed YAML. A
passing candidate keeps valid input working and returns one file-scoped
`marshmallow.ValidationError` for the malformed file, with an empty config and
no schema load. The oracle must report `PASS`, prove its import came from the
candidate worktree, exit successfully, and leave no tracked-file changes.

The application oracle command is `{SUPERSET_PYTHON}
evals/application_cases/import_unparseable_yaml.py`.

For test-quality cases, a repaired candidate must pass its normal test and make
the registered regression fail with an assertion. Skips, xfails, setup errors,
timeouts, and unrelated exceptions are not proof. The short contracts are:

| Case | Allowed path | Acceptance |
| --- | --- | --- |
| `histogram-invalid-column` | `tests/unit_tests/pandas_postprocessing/test_histogram.py` | Real non-numeric values raise the named-column `ValueError`; numeric strings remain valid. |
| `schema-missing-engine` | `tests/unit_tests/databases/schema_tests.py` | Missing engine raises the existing `ValidationError`; valid fixtures keep working. |
| `import-unparseable-yaml` | `superset/commands/importers/v1/utils.py` | Malformed YAML returns the scoped validation error without crashing. |

`VERIFIED` requires both validator evidence and an exact match between the
candidate SHA and validated SHA. A provider summary or open PR alone cannot
produce it. The validation boundary is implemented in
[app/validator.py](../app/validator.py).

## Run the safe simulation

Simulation uses fake Devin, GitHub, and validator adapters. It still exercises
the real webhook route, SQLite store, worker orchestration, report, and pages.
It does not read `.env`, call GitHub, or call Devin.

~~~bash
python3 -m venv .venv
. .venv/bin/activate
pip install -c constraints.txt -e '.[dev]'
make demo
~~~

`make demo` serves `http://127.0.0.1:8000` and resets only
`data/simulation`. Without the server:

~~~bash
python -m app.cli demo --reset
make test
make lint
~~~

The deterministic demo currently creates six jobs, four `VERIFIED`, two
`ESCALATED`, six fake sessions, and three correction messages. These are
simulation counts from local records. They are not live success rates.

## Prepare a live run

Use a dedicated fork and disposable environment. Candidate Python runs locally,
so the detached worktree and scrubbed environment are repeatability controls,
not a hostile-code sandbox.

1. Copy [.env.example](../.env.example) to `.env`; set the fork, repository ID,
   GitHub and Devin credentials, and issue mapping. Keep `ENABLE_LIVE=false`.
2. Prepare the pinned checkout and interpreter; set `SUPERSET_REPO_PATH`,
   `SUPERSET_PYTHON`, and disposable `ALLOW_LOCAL_VALIDATION=true`.
3. Run `make baseline`; every case needs current evidence with matching
   fingerprints. Run `make bootstrap-context` for the Devin context.
4. Set `ENABLE_LIVE=true`, run `make doctor`, and resolve every `/cases` gate.
5. Start web and worker separately, then configure an issues webhook ending in
   `/webhooks/github`:

~~~bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
# Separate terminal:
python worker.py
~~~

For this checkout, stop at the readiness gate: the evaluator fingerprint does not
match the archived baseline. Regenerate evidence and recheck `/cases` first.

## Read metrics without overclaiming

`/metrics` and `/report.json` use one snapshot of persisted jobs and append-only
events, not Devin summaries or a second counter database.

| Field or state | How to read it |
| --- | --- |
| `jobs_total`, `attempted`, `active` | Workflow volume and jobs still in progress |
| `verified`, `unsuccessful`, `status_counts` | Stored outcomes, separated by mode |
| `first_pass` and its denominator | First validator pass among jobs that reached that check |
| `correction_recovery` and its denominator | Repairs that recovered after the allowed correction |
| Median event-to-PR or event-to-verified time | Stored timing for the selected job set; `null` means no usable sample |
| `duplicate_deliveries`, `duplicate_executions` | Work suppressed by idempotency checks |
| `validation_counts` | Validator outcomes such as `PASS`, `REGRESSION`, or infrastructure failure |

Read `mode` and `truth` first. Simulation is synthetic. A live report describes
control-plane records and still needs human review. The linked
[`application-oracle.json`](../evidence/application-oracle.json),
[`live-application.json`](../evidence/live-application.json), and current
`/cases` page provide the separate comparator, historical run, and readiness evidence.

## Hard limits

- The registry covers two selected test-quality cases and one application
  oracle, not the full Superset suite.
- The historical live record covers malformed YAML only. It records no merge,
  customer impact, ROI, or independently measured provider cost.
- The dashboard has no authentication. Keep operational pages private and
  expose only the webhook path for a pilot.
- SQLite is intended for one worker. A deadline stops local polling but does not
  terminate a remote session automatically.
- Candidate code is executable Python without a hostile-code sandbox. Scope,
  provenance, diff, and environment checks reduce mistakes but do not isolate
  malicious code.
- There is no auto-merge, issue creation, webhook configuration, tunnel setup,
  or normal-worker provider-termination API.

For a short presentation, see [docs/demo-script.md](demo-script.md). For the full exact API and state reference, see [technical-reference.md](technical-reference.md).
