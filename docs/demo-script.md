# Cognition take-home Loom script

Target length: 4:35. The audience is a VP of Engineering or senior IC. Keep
the browser read-only and keep the two evidence boundaries visible:

- `/evidence` is the captured live application result for issue `7` and PR `8`.
- `/dashboard` after `make demo` is a credential-free simulation with six jobs,
  four `VERIFIED`, two `ESCALATED`, six fake sessions, and three corrections.

The captured PR was open and unmerged. Do not describe the archive as a current
run, a merge, a cost result, or a customer impact result. Do not describe the
simulation's provider timings as live timings.

## Prep checklist

1. From `/private/tmp/devintrace-review-fixes`, run `make demo`. This starts the
   simulation at `http://127.0.0.1:8000` and makes no Devin or GitHub calls.
2. Open these routes in separate tabs: `/evidence`, `/dashboard`,
   `/dashboard?view=attention`, `/cases#readiness`, and `/report.json`.
3. On `/evidence`, confirm the archive shows issue `7`, PR `8`, application
   `PASS`, and candidate equals validated SHA
   `5533125a2056ca4d8cd344a69d4eec575b1db786`.
4. On `/dashboard`, confirm the `SIMULATION` banner, six jobs, the `Devin API
   activity` card, and the `Export all evidence` link. Click issue `105` once
   during the recording to open `/jobs/{job_id}`.
5. Keep `/cases#readiness` ready for the final shot. After `make demo`, this
   route shows the `SIMULATION` and `NOT_EVALUATED` gates; it does not evaluate
   LIVE readiness or display the evaluator fingerprint mismatch. Explain the
   archived evidence mismatch separately: its fingerprint differs from the
   current checkout, so a LIVE pilot needs fresh baseline evidence.

## Shot-by-shot script

### 0:00 to 0:40, What: start with the real proof

**Route:** `/evidence`

**Show:** The archive banner, the baseline to candidate result, the full SHA
pair, and the scope panel.

**Say:**

> The problem is that an agent PR is not the same thing as a safe repair. This
> captured run starts from issue 7, where malformed import YAML crashed instead
> of returning a validation error. Devin produced PR 8, and a separate
> application oracle checked this exact candidate SHA and returned PASS. This
> record is deliberately narrow: the PR was open and unmerged at capture, and
> it makes no claim about a merge, cost, or customer impact.

### 0:40 to 1:15, How: show the control plane

**Route:** `/dashboard`

**Show:** The `SIMULATION` banner, handoff line, denominator-aware counters,
and job ledger.

**Say:**

> The workflow is simple to audit. A signed GitHub label creates one durable
> SQLite job. The worker gives Devin one bounded session and records the
> candidate. GitHub reads back the pull request, then a detached validator
> checks the exact commit. Only that validator can produce VERIFIED. The human
> review gate stays visible after every run.

Point to the six synthetic rows and say: “This run is four VERIFIED and two
ESCALATED. It demonstrates orchestration behavior, not a live success rate.”

### 1:15 to 1:55, How: show Devin API activity without overstating it

**Route:** `/dashboard`, then select any job with `/jobs/{job_id}`

**Show:** The dashboard `Devin API activity` card, then the same card on the
job detail page. Point to operation counts and the bootstrap resource panel,
which is `NOT_RECORDED` in this simulation.

**Say:**

> The card is built from persisted, redacted `DEVIN_API_OPERATION` events. The
> choreography has one shared setup phase and a bounded per-job phase:
> Playbook and Knowledge are live bootstrap API capabilities. This simulation
> has no `context.json`, so that shared setup is `NOT_RECORDED` here.

| Phase | Devin v3 activity | Trace rule |
| --- | --- | --- |
| Shared setup | Bootstrap the repo-scoped Playbook and Knowledge note | Live bootstrap capability; `NOT_RECORDED` in this simulation |
| Every launch | Upload the case attachment, then create one session | `attachment_upload`, `session_create` |
| Existing session | Poll the same session | `session_poll` |
| Uncertain launch or restart only | List sessions and match the exact `drp-{job_id}` tag | `list_reconcile`; never blindly create again |
| Genuine validation failure only | Send one message to the same session | `correction_message`; bounded to one |

> In this Loom, those per-job rows are synthetic adapter records with zero
> duration. The captured LIVE archive predates this trace, so a LIVE activity
> card or report can correctly say `NOT_RECORDED`. I am not presenting the
> synthetic rows as historical live API activity.

### 1:55 to 2:55, Why Devin: prove the independent boundary

**Route:** `/dashboard?kind=application`, then click issue `105` to open
`/jobs/{job_id}`

**Show:** The application workflow label, contract and allowed path, exact
candidate and validated SHAs, `Application: PASS`, and the persisted timeline.

**Say:**

> Devin is useful here because it can inspect unfamiliar repository code,
> fixtures, and instructions, reproduce the failure, and make a focused repair
> inside the approved path. A fixed script cannot investigate that context. The
> control plane still owns the boundaries: trusted case data becomes the
> attachment and prompt, the PR is read back from the expected repository, and
> the validator runs against a fresh detached checkout at the exact SHA. A
> provider summary or an open PR is only a candidate artifact.

Point to `EVALUATED` and `VERIFIED`, then say: “The green result means this
contract passed in the simulation. It is not permission to merge.”

### 2:55 to 3:35, Failure handling: show recovery and escalation

**Route:** `/dashboard?view=attention`, then open the issue `103` or `106`
detail at `/jobs/{job_id}`

**Show:** An `ESCALATED` verdict, correction count, `CORRECTION_SENT` event,
and the `Show all` events control. Point out issue `104` in the ledger as the
restart case and its reconciliation row in the API card.

**Say:**

> A lost launch response does not trigger a second paid session. The worker
> reconciles by the exact job tag. A genuine validation failure can send one
> correction to the existing session, and the new candidate must still pass
> independent validation. If reconciliation finds multiple sessions, the
> provider operation is recorded as `AMBIGUOUS`, the job becomes
> `RECONCILIATION_REQUIRED` with status `ESCALATED`, and a human must review
> it. No second session is created. If the failure persists, the job becomes
> ESCALATED for a human. No automatic merge is hidden behind this page.

### 3:35 to 4:05, When: show the customer pilot gate

**Route:** `/cases#readiness`

**Show:** The `Simulation only` label, the `SIMULATION` execution-mode check,
and the `NOT_EVALUATED` configuration-gates check. The simulation page does not
display the LIVE evaluator fingerprint mismatch.

**Say:**

> This page is intentionally simulation-only. It proves the demo cannot enable
> paid calls, while its LIVE configuration gates remain unevaluated. The
> archived application evidence has a fingerprint that differs from the
> current checkout, so it is historical proof rather than current LIVE
> baseline proof. Before a customer pilot, regenerate fresh baseline evidence
> with the current case and harness fingerprints, prepare the disposable
> validator, bootstrap the repo context, and run `make doctor` in LIVE mode.
> Proceed only after every gate passes and a human approves the pilot.

### 4:05 to 4:35, close on portable evidence

**Route:** `/report.json`, then return to `/dashboard`

**Show:** The JSON export's mode, workflow counts, jobs, timelines, and
provider activity summary. Finish on the dashboard's human review gate.

**Say:**

> The customer gets a portable evidence record: what was admitted, what Devin
> did, what the independent oracle checked, and where a human must decide. The
> next measured step is the approved pilot itself. Merge decisions, customer
> impact, ROI, and provider cost require their own evidence.

## Evidence boundaries to keep on screen

- The live archive proves one application contract result for issue `7` and PR
  `8`, with matching candidate and validated SHA. It does not include a
  historical API trace.
- The dashboard trace is synthetic and persisted in the simulation store. Its
  zero-duration timing is a deterministic fake-provider value.
- `SIMULATION` counts are six jobs, four `VERIFIED`, two `ESCALATED`, six fake
  sessions, and three correction messages. They are not live customer metrics.
- The source comparator is validator evidence for the known baseline, not a
  Devin result. `/evidence` is separate from current dashboard metrics.
