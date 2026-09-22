# App walkthrough: from an approved issue to independent proof

DevinTrace is an evidence control plane for scoped Superset repairs. A human
approves one registered GitHub issue by adding `devin-remediate`. The service
verifies the signed event, writes durable work to `SQLite`, gives `Devin` one bounded
session, and checks the resulting pull request independently. Only the
validator can produce `VERIFIED`.

The browser is read-only. It shows mode, contracts, jobs, exact candidate and
validated commits, and event timelines. `SIMULATION` uses fake Devin, GitHub, and
validator adapters. `LIVE` uses configured services and still requires human
review and merge.

- [Orient in the workbench](#orient-in-the-workbench)
- [Follow the architecture](#follow-the-architecture)
- [Walk through issue 7 and PR 8](#walk-through-issue-7-and-pr-8)
- [Read the contracts and validation boundary](#read-the-contracts-and-validation-boundary)
- [Trace state, retry, and idempotency](#trace-state-retry-and-idempotency)
- [Check the HTTP and provider APIs](#check-the-http-and-provider-apis)
- [Run simulation or prepare live mode](#run-simulation-or-prepare-live-mode)
- [Interpret metrics and evidence](#interpret-metrics-and-evidence)
- [Know the limits](#know-the-limits)

## Orient in the workbench

| Page | Purpose |
| --- | --- |
| `/` | Product overview and link to the recorded application archive |
| `/dashboard` | Read-only workbench with filters, job ledger, counters, and attention states |
| `/jobs/{job_id}` | One job's verdict, exact-SHA comparison, links, and event timeline |
| `/cases` | Registered contracts, allowed paths, oracle type, and current readiness gates |
| `/evidence` | Separately dated live application evidence |
| `/report.json` | The same normalized report used by the dashboard |
| `/healthz` | Reads the selected SQLite store and reports mode |
| `/metrics` | Returns JSON counters computed from persisted jobs and events |

The archive does not add a historical run to current workspace metrics. A job
detail page is evidence for one stored job, not a merge approval. The browser
cannot enqueue work; only a signed GitHub event can do that. The routes and
report are implemented in [app/main.py](../app/main.py),
[app/views.py](../app/views.py), and [app/report.py](../app/report.py).

## Follow the architecture

~~~mermaid
flowchart LR
    H[Human adds devin-remediate] --> G[GitHub issues webhook]
    G --> A[FastAPI admission]
    A --> DB[(SQLite jobs, deliveries, events)]
    DB --> O[worker.py and Orchestrator]
    O --> D[Devin session]
    D --> P[Provider PR reference]
    P --> GH[GitHub PR read-back]
    GH --> V[Detached SHA validator]
    V --> DB
    DB --> UI[Dashboard and report.json]
    R[Registered cases and baseline proof] --> A
    R --> O
    V --> R
    V -->|One bounded correction| D
~~~

| Component | Responsibility | Boundary |
| --- | --- | --- |
| [app/main.py](../app/main.py) | HMAC admission, health, metrics, and report routes | Does not call Devin |
| [app/cases.py](../app/cases.py) and [evals/cases.yaml](../evals/cases.yaml) | Immutable case scope, contract, issue binding, and fingerprints | Issue text cannot choose scope |
| [app/db.py](../app/db.py) and [app/models.py](../app/models.py) | Jobs, deliveries, transitions, and append-only events | Intent survives restart |
| [worker.py](../worker.py) and [app/orchestrator.py](../app/orchestrator.py) | Launch, recover, poll, validate, correct once, and stop | One worker lock |
| [app/devin.py](../app/devin.py) | Devin v3 organization API | Provider output is only a candidate artifact |
| [app/github.py](../app/github.py) | PR discovery and exact head read-back | Pins a 40 character SHA |
| [app/validator.py](../app/validator.py) | Scope gates and detached test or application oracle | Only source of `VERIFIED` |

The web process and worker communicate through `SQLite`. There is no external
queue, auto-merge path, GitHub issue-creation client, webhook configuration
client, tunnel client, or normal-worker provider-termination method.

## Walk through issue 7 and PR 8

This is the recorded application run. The current `/cases` readiness panel is
still the source of truth for a future run.

### The case and admission

Issue 7 is bound to `import-unparseable-yaml`. It is an application case for
`dheeraj5612/superset` on `remediation-import-yaml`, with baseline
`dedfe23a805151decca6deaf15b032e040e42e82`. Devin may change only:

~~~
superset/commands/importers/v1/utils.py
~~~

The trusted oracle is
[evals/application_cases/import_unparseable_yaml.py](../evals/application_cases/import_unparseable_yaml.py).
The registry supplies the contract, scope, target branch, and baseline proof.
Arbitrary issue text does not authorize edits.

After `devin-remediate` is added, GitHub sends an `issues` event. FastAPI checks the
HMAC over the original raw bytes, exact repository ID and name, positive issue
number, exact label, and case binding. In `LIVE` it also checks live settings,
the disposable evaluator, provider context, and current baseline proof. A
transaction then creates the job and its first event. It returns HTTP `202`:

~~~json
{
  "job_id": "stored-job-identifier",
  "status": "QUEUED",
  "duplicate": false
}
~~~

The identifier is illustrative. Credentials are not in the response or event
log.

### The candidate and independent oracle

The worker records `launch_requested` before creating one `Devin` session. The
prompt includes trusted case data, baseline attachment, repo-scoped Playbook and
Knowledge note, and tag `drp-{job_id}`. Structured output requires `pr_url` and
`summary`, but neither is proof.

The recorded run used one session and produced PR [#8](https://github.com/dheeraj5612/superset/pull/8).
At capture it was open and unmerged. GitHub pinned head
`5533125a2056ca4d8cd344a69d4eec575b1db786`.

GitHub requires PR `8` to be open, non-draft, unmerged, on base branch
`remediation-import-yaml`, and in the configured fork for both base and head.
The detached validator then:

1. Imports `superset.commands.importers.v1.utils` from the candidate checkout.
2. Loads `datasets/valid.yaml` and requires one schema load of `{"key": "value"}`.
3. Loads malformed `databases/malformed.yaml` containing an unterminated string.
4. Requires one `marshmallow.ValidationError` with exactly
   `{"databases/malformed.yaml": "Not a valid YAML file"}`, an empty returned
   config, and no schema load.

A passing oracle result has this shape:

~~~json
{
  "outcome": "PASS",
  "module": "/candidate/checkout/superset/commands/importers/v1/utils.py",
  "provenance": true
}
~~~

The path is representative. Exit code `0`, true provenance, and no tracked-file
changes in the candidate worktree are also required. The recorded job is
`VERIFIED` with `application` `PASS`, zero corrections, zero provider API failures, zero stale-head
observations, and identical candidate and validated SHAs.

See [evidence/live-application.json](../evidence/live-application.json).
This is application-oracle evidence only. It does not claim the full Superset
suite, a merge, customer impact, or cost savings. The provider snapshot
reported 0.0 ACUs after a successful termination request, which is not an
independent cost result.

### Historical proof versus current readiness

[evidence/application-oracle.json](../evidence/application-oracle.json) is a
local source comparator. It records the pinned baseline as `REGRESSION` and the
known reference fix 22ec1f598808859c42dccff21665786224127122 as `PASS`. That
reference is not a new Devin run.

The dated live record says what issue 7 observed. `/cases` checks the current
checkout, case and harness fingerprints, baseline files, provider context, and
environment. The current evaluator fingerprint differs from the archived
baseline, so current live readiness is blocked until baseline evidence is
regenerated.

## Read the contracts and validation boundary

The registry has three cases. The two test-quality cases are source-inspected
candidates and need runtime baseline confirmation before live admission. The
application case has the recorded baseline and candidate evidence.

Both test-quality cases pin source commit
`5ecb19cf92ae8dedbf5b33ec92324cc77c4ee10e` and use the default
`remediation-demo` target branch. The application case pins its own source and
target branch as described in the issue 7 walkthrough.

| Case | Allowed path and contract | Acceptance |
| --- | --- | --- |
| `histogram-invalid-column` | `tests/unit_tests/pandas_postprocessing/test_histogram.py`. Genuine non-numeric values raise `ValueError` naming the selected column; numeric strings such as `10` remain valid. | Normal test passes; the registered invalid-input regression causes an assertion failure |
| `schema-missing-engine` | `tests/unit_tests/databases/schema_tests.py`. `DYNAMIC_FORM` with no engine raises the existing `ValidationError`; valid engine fixtures retain behavior. | Normal test passes; the registered missing-engine bypass causes an assertion failure |
| `import-unparseable-yaml` | `superset/commands/importers/v1/utils.py`. Malformed YAML yields one file-scoped validation error and never crashes; valid mapping still loads. | Trusted application oracle returns `PASS` with provenance |

For test-quality cases, normal means the designated test passes on clean
candidate behavior. The baseline weak test has normal `PASS` and mutant `PASS`,
because the active regression escapes it. A candidate repair must have normal
`PASS` and mutant `ASSERTION_FAILED`, because the same test now catches that
regression. Missing or extra test IDs, skips, xfails, setup errors, timeouts,
inactive controls, and non-assertion exceptions do not prove a repair.

Before either evaluator runs, the validator requires a descendant of the
pinned baseline, registered scope only, regular non-executable files, no
adds/deletes/renames, and at most 200 changed lines. A moving PR head invalidates
the earlier result.

## Trace state, retry, and idempotency

~~~mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> DEVIN_RUNNING
    DEVIN_RUNNING --> PR_OPENED
    PR_OPENED --> VALIDATING
    VALIDATING --> VERIFIED
    VALIDATING --> CORRECTING
    VALIDATING --> PR_OPENED
    CORRECTING --> PR_OPENED
    QUEUED --> FAILED
    VALIDATING --> FAILED
    VALIDATING --> ESCALATED
    DEVIN_RUNNING --> FAILED
    DEVIN_RUNNING --> ESCALATED
    PR_OPENED --> FAILED
    PR_OPENED --> ESCALATED
~~~

`FAILED` means control-plane or infrastructure failure. `ESCALATED` means a
human must resolve an ambiguous provider action, missing candidate, deadline,
unstable head, or unresolved repair. Terminal states are `VERIFIED`,
`FAILED`, and `ESCALATED`.

| Situation | Durable behavior |
| --- | --- |
| First tick | `QUEUED` becomes `DEVIN_RUNNING` and starts the job deadline |
| Lost launch response | Search sessions by exact `drp-{job_id}` tag. Do not blindly POST again. Zero matches becomes `ESCALATED`; multiple matches raise `RemoteError`, retry as `LAUNCH_UNCERTAIN` up to three times, then become `FAILED`. |
| Retryable provider error | Network, `429`, and `5xx` errors retry up to three consecutive failures with provider delay or exponential backoff. Other `4xx` errors become `FAILED`. |
| Lost correction acknowledgement | Record `correction_requested` first, send only to the same session, and escalate if acknowledgement is missing. |
| Repair failure | `NORMAL_FAILED`, `REGRESSION_SURVIVED`, or `APPLICATION_FAILED` gets one correction. A second failure escalates. |
| Changed PR head | Replace the SHA, clear validation evidence, and validate again. More than three changes escalates. |
| Deadline | Escalate and stop local polling. The remote session is not terminated automatically. |
| Duplicate delivery | Record `DUPLICATE_DELIVERY` and return the original job. |
| Same mode, repository, issue, and generation | Record `DUPLICATE_EXECUTION` and return the original job. |
| Worker restart | For unfinished jobs with a saved non-null `devin_session_id`, record `WORKER_RESUMED` and poll the saved session. |

Intent is committed before each expensive side effect. SQLite has a unique
mode, repository, issue, and generation key; one OS worker lock protects the
database. This avoids blind duplicate launches but is not an exactly-once
guarantee for an external provider.

## Check the HTTP and provider APIs

### Application endpoints

| Method and path | Input | Successful response |
| --- | --- | --- |
| `GET /healthz` | None | JSON fields `status`, `mode`, and `live_enabled`; `live_enabled` reflects the configured setting, and the route reads SQLite without proving readiness |
| `GET /metrics` | None | JSON object with `mode` plus counters computed from persisted jobs and events, not Prometheus exposition |
| `GET /report.json` | None | Attachment `devin-remediation-evidence-{mode}.json`, schema `devin-remediation-evidence/v1` |
| `POST /webhooks/github` | Signed JSON and GitHub headers | HTTP `202` with `job_id`, `status`, `duplicate` after durable enqueue |
| `GET /`, `/dashboard`, `/cases`, `/evidence`, `/jobs/{job_id}` | GET and dashboard filters | Server-rendered, read-only pages |
| `GET /robots.txt` | None | Plain text crawl policy, not access control |

All responses receive `nosniff`, `no-referrer`, and a local-only Content
Security Policy. Operational responses are uncached and `noindex`. The dashboard has no
authentication.

### Inbound GitHub webhook

~~~
POST /webhooks/github
Content-Type: application/json
X-GitHub-Event: issues
X-GitHub-Delivery: delivery-123
X-Hub-Signature-256: sha256=<HMAC_SHA256_HEX>
~~~

The accepted strict fields are illustrated below:

~~~json
{
  "action": "labeled",
  "repository": {"id": 123456, "full_name": "owner/superset-fork"},
  "issue": {"number": 7},
  "label": {"name": "devin-remediate"}
}
~~~

The SHA-256 HMAC covers exact UTF-8 bytes and is compared in constant time
before JSON parsing. Bodies are limited to 256 KiB. ping returns
`{"status":"pong"}` after signature verification. Other event families and
non-labeled actions return `{"status":"ignored"}`. Invalid signatures are
`401`, oversized bodies `413`, malformed events `400`, repository
mismatches `403`, unknown issue bindings `422`, and incomplete live setup
`503`. A wrong label is ignored.

### Devin v3

The base is `https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}/` with a
`Bearer` token, 30 second timeout, no ambient proxy, and no redirects.

| Operation | Request | Required response fields |
| --- | --- | --- |
| Bootstrap Playbook | `GET playbooks?first=100` and optional `after` cursor; absent resource uses `POST playbooks` with `title` and `body` | `items`, `has_next_page`, `end_cursor`; one `playbook_id` |
| Bootstrap Knowledge note | `GET knowledge/notes?first=100` and optional `after` cursor; absent resource uses `POST knowledge/notes` with `name`, `body`, `trigger`, `is_enabled` | Paginated envelope; one `note_id`; body drift stops reuse |
| Attachment | `POST attachments` multipart file named `case-id.json` with artifact JSON | `url` |
| Session create | `POST sessions` with `prompt`, `title`, `repos`, `playbook_id`, `knowledge_ids`, `attachment_urls`, `tags`, `max_acu_limit`, `structured_output_schema`, `structured_output_required` | `session_id`, `status`, optional `url`, `status_detail`, `pull_requests`, `tags` |
| Session poll | `GET sessions/{session_id}` | Same validated session fields and matching `session_id` |
| Recovery | Cursor-paginated `GET sessions`, exact `drp-{job_id}` tag | Zero escalates, one attaches, multiple are ambiguous |
| Correction | `POST sessions/{session_id}/messages` with `{"message": text}` | Any successful `2xx` acknowledgement |

The session structured output schema requires a string `pr_url` and string
`summary`, with no additional properties:

~~~json
{
  "prompt": "Trusted case prompt assembled from the registry",
  "title": "Registered case title",
  "repos": ["owner/superset-fork"],
  "playbook_id": "playbook-id",
  "knowledge_ids": ["note-id"],
  "attachment_urls": ["https://provider.example/attachment"],
  "tags": ["drp-job-id", "case-id"],
  "max_acu_limit": 3,
  "structured_output_schema": {
    "type": "object",
    "properties": {
      "pr_url": {"type": "string"},
      "summary": {"type": "string"}
    },
    "required": ["pr_url", "summary"],
    "additionalProperties": false
  },
  "structured_output_required": true
}
~~~

Provider session URLs are accepted only as `HTTPS` links on
`app.devin.ai` without embedded credentials. Provider output never
substitutes for validation.

Bootstrap reuses a same-named resource only when its `body` matches. The
knowledge note's `trigger` and `is_enabled` fields are sent when a
missing note is created; they are not separate reuse checks.

### GitHub PR read-back and oracle result

The adapter sends `GET https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{number}` with
`Accept: application/vnd.github+json` and API version `2022-11-28`.
It requires `state=open`, `merged=false`, `draft=false`, the
case's `base.ref`, matching `base.repo.id` and `head.repo.id`,
and lowercase 40 character `head.sha`. The candidate is `number`,
`sha`, and URL. The adapter discovers a PR only from an exact configured-repo URL
in the provider's pull_requests list. The validator also fetches
`refs/pull/{number}/head` before opening the detached worktree.

The application oracle is invoked as:

~~~text
{SUPERSET_PYTHON} evals/application_cases/import_unparseable_yaml.py
~~~

It receives `DRP_CANDIDATE_ROOT` and a scrubbed environment. The final
non-empty stdout line must be JSON with outcome `PASS`, `REGRESSION`,
`CONTRACT_FAILED`, or `INFRA_ERROR`. `INFRA_ERROR` must exit
`2`; the other statuses must exit `0`. The result must have
`provenance: true`, and the validator must observe no tracked-file changes
in the candidate worktree.

## Run simulation or prepare live mode

### Credential-free simulation

`SIMULATION` uses isolated storage and fake external adapters while exercising the
real webhook route, `SQLite`, orchestrator, report, and UI. It covers first-pass
verification, one correction, escalation, restart, application acceptance and
rejection, and duplicate delivery.

~~~bash
python3 -m venv .venv
. .venv/bin/activate
pip install -c constraints.txt -e '.[dev]'
make demo
~~~

`make demo` runs `python -m app.cli demo --reset --serve` at
`http://127.0.0.1:8000` and resets only `data/simulation`. It does not read
`.env` or call GitHub or Devin. Without a server:

~~~bash
python -m app.cli demo --reset
make test
make lint
~~~

The deterministic run produces six jobs, four `VERIFIED`, two `ESCALATED`, six fake
sessions, and three correction messages. These are simulation counts, not live
success rates.

### Live preparation

Use a dedicated fork and disposable environment. Candidate Python executes
locally; a detached worktree and scrubbed environment are not a hostile-code
sandbox.

The main settings are typed in `app/config.py`. The defaults and operator inputs
are documented in [.env.example](../.env.example).

| Setting | Meaning |
| --- | --- |
| `MODE` | `LIVE` for the worker; the demo constructs `SIMULATION` settings explicitly |
| `DATA_DIR` | Root for mode-isolated `SQLite` and evidence, default `data` |
| `ENABLE_LIVE` | Explicit second switch for paid execution, default `false` |
| `BASE_BRANCH` | `remediation-demo` by default; the application case overrides it with `remediation-import-yaml` |
| `CASE_ISSUES` | JSON map from each registered case ID to one positive issue number |
| `GITHUB_REPOSITORY` and `GITHUB_REPOSITORY_ID` | Dedicated fork identity checked by admission and PR read-back |
| `SUPERSET_REPO_PATH` and `SUPERSET_PYTHON` | Prepared checkout and interpreter used for local validation |
| `ALLOW_LOCAL_VALIDATION` | Must be `true` only in the disposable validation environment |

1. Copy [.env.example](../.env.example) to `.env`. Set fork identity, numeric
   repository ID, GitHub read token, webhook secret, Devin organization and API
   key, and `CASE_ISSUES`. Keep `ENABLE_LIVE=false` while preparing.
2. Prepare the pinned Superset checkout and real dependencies. Set
   `SUPERSET_REPO_PATH`, `SUPERSET_PYTHON`, optional `SUPERSET_CONFIG_PATH`, and
   `ALLOW_LOCAL_VALIDATION=true` only in the disposable environment.
3. Run `make baseline`. Every registered case needs current `CONFIRMED` evidence
   with matching case and harness fingerprints.
4. Run `make bootstrap-context` to create or reuse repo-scoped provider context.
5. Set `ENABLE_LIVE=true` and run `make doctor`. Resolve every gate.
6. Start the web process and worker separately, then configure a GitHub issues
   webhook ending in `/webhooks/github`:

~~~bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
# Separate terminal:
python worker.py
~~~

Worker startup checks `MODE=LIVE`, `live_errors()`, every registered
case's baseline evidence, and the worker lock before provider use. At webhook
admission and again before session creation, `launch_preflight` checks the
local evaluator and repo-scoped provider context. `make doctor` runs the
operator-facing checks separately. The worker exits when any gate fails or
another worker already owns the database.

7. Add `devin-remediate` to one approved issue. This can start paid work. Inspect
   the exact SHA, validator evidence, timeline, and complete diff before merge.

The optional live Docker profile is `docker compose --profile live up --build`.
Its mounted Superset interpreter must be Linux-compatible and its baseline,
context, and worker data must share `/data`. The default Docker compose starts
only the web service with live spending disabled.

## Interpret metrics and evidence

`/dashboard` and `/report.json` share report schema
`devin-remediation-evidence/v1`. The following abbreviated illustrative
shape omits descriptive fields from workflow steps and case records:

~~~json
{
  "schema": "devin-remediation-evidence/v1",
  "generated_at": "2026-09-22T00:00:00Z",
  "truth": {
    "mode": "SIMULATION",
    "simulation": true,
    "statement": "Synthetic adapters and local timings. No live remediation evidence."
  },
  "metrics": {
    "jobs_total": 0, "attempted": 0, "active": 0, "verified": 0,
    "unsuccessful": 0, "first_pass": 0, "first_pass_denominator": 0,
    "correction_recovery": 0, "correction_denominator": 0,
    "median_event_to_pr_seconds": null, "median_event_to_verified_seconds": null,
    "duplicate_deliveries": 0, "duplicate_executions": 0,
    "status_counts": {}, "validation_counts": {}
  },
  "readiness": {
    "state": "SIMULATION", "label": "Simulation only",
    "message": "The demo proves orchestration behavior, not live remediation.",
    "problems": [], "checks": [
      {"label": "Execution mode", "state": "SIMULATION", "detail": "Simulation is intentionally isolated from paid calls."},
      {"label": "Configuration gates", "state": "NOT_EVALUATED", "detail": "Run make doctor in LIVE mode before a pilot."}
    ]
  },
  "workflow": {
    "mode": "SIMULATION",
    "steps": [
      {"id": "admitted", "count": 0}, {"id": "sessions", "count": 0},
      {"id": "candidates", "count": 0}, {"id": "evaluated", "count": 0},
      {"id": "verified", "count": 0}
    ]
  },
  "cases": [
    {"id": "histogram-invalid-column"}, {"id": "schema-missing-engine"},
    {"id": "import-unparseable-yaml"}
  ],
  "jobs": []
}
~~~

This is a valid JSON illustration with descriptive fields omitted from workflow
steps and case records, rather than an exact report snapshot. Both
`SIMULATION` and `LIVE` reports include full case records and
workflow labels; a demo run also fills normalized jobs and event timelines.

Metrics are computed from one snapshot of persisted jobs and append-only events.
They are not imported from provider summaries or a separate counter store.
Fields include total, attempted, active, verified, unsuccessful, first-pass and
its evaluated denominator, correction recovery and its acknowledgement
denominator, median event-to-PR and event-to-verification time, duplicate
deliveries, duplicate executions, status counts, and validation counts.

`SIMULATION` truth is explicit. `LIVE` truth means control-plane records only and
still requires human review. `VERIFIED` requires validator evidence and an exact
candidate-to-validated SHA link. An open PR, provider summary, or archive card
cannot substitute.

| Evidence layer | Meaning |
| --- | --- |
| `SIMULATION` report | State transitions, duplicate handling, recovery, and UI behavior using fakes |
| `evidence/application-oracle.json` | Local pinned-source regression and known-reference comparison without a Devin call |
| `evidence/live-application.json` | One recorded live candidate, PR `8`, exact SHA, and application-oracle result for issue `7` |
| Current `/cases` readiness | Current configuration, context, fingerprints, baseline files, and environment gates |

## Know the limits

- The registry covers two selected test-quality nodes and one application oracle,
  not the full Superset suite.
- The live record covers the malformed-YAML application contract only. PR `8` was
  open and unmerged at capture. No customer impact, merge, ROI, or independently
  measured ACU cost is recorded.
- The dashboard is unauthenticated. Keep operational routes private and expose
  only the webhook path for a pilot.
- `SQLite` is intended for one worker. Terminal jobs have no automatic reset
  endpoint.
- A deadline stops local polling but does not terminate a remote session. Human
  reconciliation handles ambiguous launches, corrections, deadlines, and cleanup.
- Candidate code is executable Python without a hostile-code sandbox. Scope,
  provenance, diff, and environment checks reduce mistakes but do not isolate
  malicious code.
- There is no auto-merge, issue creation, webhook configuration, tunnel setup,
  or normal-worker provider-termination API.

For a short presentation, pair this reference with
[docs/demo-script.md](demo-script.md). For implementation review, start with
[app/main.py](../app/main.py), then follow
[app/orchestrator.py](../app/orchestrator.py) and
[app/validator.py](../app/validator.py).
