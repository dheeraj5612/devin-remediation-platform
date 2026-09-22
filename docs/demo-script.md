# Five-minute customer walkthrough

This walkthrough uses the credential-free simulation. Every result is labelled synthetic. Do not describe local timings as production performance or imply that a candidate PR was merged.

## Acceptance criteria

- The dashboard and `/report.json` share one snapshot and show `SIMULATION` plus its evidence boundary.
- KPIs show their numerator and denominator where a denominator exists.
- Each case shows its workflow type, contract, allowed paths, baseline state, and evidence badge.
- A selected job shows exact candidate and validated SHAs, oracle status, timeline, and persisted links.
- The demo covers duplicate delivery, correction recovery, application acceptance and rejection, escalation, and restart recovery without a live API call.

## 0:00 to 0:45, what problem are we solving?

Open the dashboard and say:

> A green agent PR does not prove that a repair is safe. This demo covers two workflows: strengthening a weak test with normal and controlled-regression checks, and repairing a known application regression where malformed import YAML must return a validation error instead of an `UnboundLocalError`. In both workflows, the agent produces a candidate and an independent oracle checks the exact candidate SHA outside the Devin session. Synthetic evidence stays visible so this demo cannot be mistaken for a live customer result.

Point to the `SIMULATION` badge, evidence notice, and KPI denominators. Explain that the page and JSON download read the same persisted jobs and events.

## 0:45 to 1:35, how it works

Walk left to right:

1. GitHub sends a signed labelled issue event.
2. Admission checks the repository, approved case binding, and baseline proof before writing a job.
3. The worker records durable intent, then uses one Devin session for the edit and any bounded correction.
4. A fresh evaluator checks the exact candidate SHA, using normal/mutant phases for test-quality work or a trusted application contract for the YAML regression.
5. The dashboard exposes the timeline, exact SHAs, workflow-specific result, and pilot readiness.

Say: “Devin owns investigation and the edit. The control plane owns admission, recovery, and proof.”

## 1:35 to 3:35, show the simulation

From a clean checkout, run:

```bash
make demo
```

Open `http://127.0.0.1:8000`. The deterministic run settles six jobs: four `VERIFIED`, two `ESCALATED`, six sessions, and three correction messages. These are persisted simulation records, not live customer outcomes.

1. **Test-quality repair, issue 101.** The independent oracle records normal behavior and detection of the controlled regression without a correction.
2. **Recovery, issues 102 and 104.** Issue 102 receives one correction in the same session. Issue 104 resumes after a worker restart. Point to the correction count and `WORKER_RESUMED` in the timeline.
3. **Application regression, issues 105 and 106.** The trusted application oracle records simulated `PASS` and `REGRESSION` results. Point to the `APPLICATION REGRESSION` cards and the application label in the selected timeline.

If time permits, mention issue 103 as bounded escalation: repeated validation failure is not presented as verified. The duplicate delivery is counted as suppressed. Click **Export evidence** and show `truth.simulation`, workflow counts, case contracts, jobs, timelines, and validation fields. The export makes no ROI or ACU claim.

## 3:35 to 4:25, why Devin?

Say:

> A fixed script can check a known behavior, but it cannot investigate unfamiliar fixtures, trace the right code path, or choose the smallest scoped repair. Devin can inspect the repository and propose that change. The trusted application oracle and normal/mutant harness then check the candidate independently. The source-reference check proves what the known baseline does; it is validator evidence, never a Devin result. Provider completion remains a candidate artifact, never the verdict.

Point to allowed paths and the acceptance contract. Explain that scope checks and the external evaluator prevent a successful session from becoming an automatic merge.

## 4:25 to 5:00, when to pilot

Point to **Pilot readiness** and say:

> A customer pilot starts with one approved finding in a dedicated fork. Before live execution, the operator needs current baseline proof, a prepared disposable validator, a context bundle, and a unique issue binding. The readiness panel reports those gates; it does not call simulation production-ready.

Close on the dashboard. A live Devin run, merged change, ROI, and ACU savings require separate measured evidence.

## Evidence boundaries

- Available now: deterministic simulation states, SQLite jobs/events, validation fields, exact simulated SHAs, readiness checks, and the downloadable report.
- Available only after a configured live run: real session and PR links, live candidate SHA, independent validation record, and measured timestamps.
- New instrumentation is required for customer reporting: human review/merge decisions, provider usage or cost, and a defined business-impact measure.
- Not recorded here: merge status, customer impact, ROI, ACU savings, or a Loom link.
