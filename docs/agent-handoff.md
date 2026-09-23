# DevinTrace agent handoff

Updated 2026-09-23. Read this before changing or presenting the project. Do not
print, commit, or paste credentials from `.env`.

## Assignment and goal

The available summary of Cognition's Applied AI Engineer take-home asks for an
event-driven Devin API automation around Apache Superset issues, observable
outputs, Docker, public repositories, and a short Loom presentation. The
request was time-boxed to four days. The exact email body is not stored in this
repository, so verify it in Gmail before claiming an item is verbatim.

The user's additional goal is to show meaningful breadth across the Devin API,
measure real outcomes, and present an impressive but honest live demo. They
prefer concise, ELI5 explanations, a modern dashboard, and no extra PRs for
this platform's own changes. The app is called **DevinTrace**.

## Where to work

```bash
cd /Users/dheerajnamburu/Documents/Codex/devin-remediation-platform
```

This is the primary repository on `main`, synced with GitHub `main` at the
handoff. The temporary `/private/tmp/devintrace-review-fixes` worktree was
removed after its commits, `.env`, and live data were migrated. Do not use links
to that removed path. The original checkout had five unfinished `src/drp`
edits on `codex/multi-api-evaluation`; they were preserved locally as
`stash@{0}` with message `Preserve pre-DevinTrace local src/drp edits before main
sync`. Do not apply or drop that stash without reviewing it; `src/drp` was
replaced by `app/` on current `main`.

The root `.env` and `data/live/` are ignored by Git. The `.env` has mode `0600`.
They hold the live configuration, baseline proofs, SQLite job history, and
context IDs. Never commit them. The local live app runs at
<http://127.0.0.1:8010/dashboard>; its worker is stopped so the recorded demo
does not launch further paid work. Port 8001 is a separate synthetic demo.
The webhook-only proxy and temporary Cloudflare tunnel may stop or change URL;
verify them before triggering another GitHub event. The dashboard is not
authenticated, so keep it local/private.

## What was built

DevinTrace is a FastAPI, SQLite, single-worker remediation control plane. A
human approves one registered Superset issue with `devin-remediate`. GitHub
sends a signed `issues` webhook to `POST /webhooks/github`. The app verifies
HMAC, repository identity, label, and exact issue-to-case binding, then saves
one durable job. The worker uploads baseline evidence, launches one bounded
Cloud Devin session using a reusable Playbook and Knowledge note, polls that
session, reads the proposed PR back from GitHub, and independently tests the
exact PR commit in detached Superset checkouts. It can send one correction in
the same session for a genuine failed repair. Infrastructure errors fail
closed. `VERIFIED` means the registered contract passed, not that the PR was
merged or the full Superset suite passed. Human review is the final gate.

Key code: `app/main.py` and `app/views.py` serve webhook and pages;
`app/cases.py` plus `evals/cases.yaml` define trusted case scope;
`app/db.py` stores jobs, deliveries, and events; `worker.py` and
`app/orchestrator.py` manage durable state; `app/devin.py` and `app/github.py`
wrap external APIs; `app/validator.py` runs the independent oracle;
`app/report.py` and `app/metrics.py` feed `/dashboard`, `/jobs/{id}`, and
`/report.json`. `docs/technical-reference.md` explains the state machine and
HTTP/API contracts. `docs/app-walkthrough.md` is the short ELI5 explanation.

Meaningful Devin v3 use includes paginated Playbook and Knowledge lookup or
creation, attachment upload, session creation and polling, exact-tag session
reconciliation after an uncertain launch, and same-session messages when a
repair fails. Session creation uses repository, playbook, knowledge, evidence
URL, structured output, job tags, and a 3 ACU limit. The GitHub webhook and
PR read-back are separate GitHub/app operations, not Devin endpoints. The app
records provider operation outcomes; it does not expose every Devin endpoint.
Exploratory read-only checks of session messages, attachments, insights, and
consumption were performed outside the app. Do not present them as dashboard
integrations. See `README.md` for local simulation and live setup.

## Live proof to show

- Start at <http://127.0.0.1:8010/dashboard> and then open the
  [verified job](http://127.0.0.1:8010/jobs/32ca494e299743fc9186bb8a9e4e5574).
- The trigger was [Superset issue #9](https://github.com/dheeraj5612/superset/issues/9)
  with the `devin-remediate` label. Devin authored
  [Superset PR #10](https://github.com/dheeraj5612/superset/pull/10), which
  remains open and unmerged. The only changed file is
  `tests/unit_tests/utils/test_date_parsing.py`.
- One real Devin session was created. The app recorded one evidence attachment,
  session creation, 26 polls, and no correction message. The 26 polls are
  repeated reads, not 26 API capabilities. Four older project-tagged Devin
  sessions were archived; unrelated organization sessions were left alone.
- The original weak test passed on both clean behavior and a known one-row
  datetime regression. The PR adds dtype and value assertions for empty,
  missing, one-row string, and already-datetime inputs. The independent
  validator returned normal `PASS` and mutant `ASSERTION_FAILED` at exact SHA
  `f2cf1e45117f8ac1859d1dfcfb4569cd1f9e2f35`.
- The first validation failed closed with `INFRA_ERROR` because macOS gave a
  temp checkout two equivalent path spellings. The validator was fixed and all
  baselines rebuilt. An operator revalidated the unchanged PR commit without
  a second Devin session; the timeline now shows `Verified after retry`.
  GitHub redelivered the signed event once; the app suppressed the duplicate
  execution. The dashboard shows one verified job and one suppressed delivery.
- The provider later reported 0.0 consumed ACUs. That does not establish a
  dollar cost or savings. This run proves only the registered one-row
  test-quality contract, not broad Superset correctness or production impact.

The four-minute recording plan is `docs/demo-script.md`. The `/evidence` page
contains a different historical live application case, issue #7 and PR #8.
Port 8001 and its six-job dashboard are explicitly `SIMULATION`; keep those
counts separate from this live job.

## Safe next steps and checks

Review the [Loom script](demo-script.md) and confirm that the live dashboard,
job, issue, and PR still load before recording. For source checks, run
`.venv/bin/python -m pytest -q` and `.venv/bin/python -m ruff check .` from the
primary repo. For live readiness, use `.venv/bin/python -m app.cli doctor`;
this does not authorize another session. To run another paid case, restore the
worker intentionally and first confirm the signed webhook tunnel, registered
issue binding, baseline, and ACU cap. Do not relabel an issue casually.

Known limitations: one-worker SQLite deployment; unauthenticated dashboard;
no hostile-code sandbox for candidate tests; limited registered oracle scope;
no automatic merge; no verified paid ACU consumption; no claim that every
Devin endpoint was used. The app's `VERIFIED` verdict is contract evidence for
human review, not a production release decision.
