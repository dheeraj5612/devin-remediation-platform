# DevinTrace: live Loom walkthrough

Target: 4 minutes. Record the **LIVE** app at
`http://127.0.0.1:8010/dashboard`. Keep `.env`, tokens, and terminal history off
screen. The worker can stay stopped while you present the saved evidence.

## Before recording

1. Open [issue #9](https://github.com/dheeraj5612/superset/issues/9),
   [PR #10](https://github.com/dheeraj5612/superset/pull/10), the
   [live dashboard](http://127.0.0.1:8010/dashboard), and the
   [job proof](http://127.0.0.1:8010/jobs/32ca494e299743fc9186bb8a9e4e5574).
2. Confirm the dashboard says `LIVE`, one job is `VERIFIED`, and the job page
   says `Exact SHA matched`. The [JSON export](http://127.0.0.1:8010/report.json)
   should show candidate and validated SHA
   `f2cf1e45117f8ac1859d1dfcfb4569cd1f9e2f35`.
3. Keep the [case readiness page](http://127.0.0.1:8010/cases#readiness)
   available. All four pinned baselines are `CONFIRMED`.

## What to show and say

**0:00 to 0:40 | The problem.** On the job page, jump to **Proof**.

> This test only checked that no exception was raised. Both the correct code and a known
> one-row datetime regression passed it. That is the before state: `PASS` and
> `PASS`. A green test can still miss the bug.

**0:40 to 1:25 | The real trigger and Devin APIs.** Show issue #9's
`devin-remediate` label, then return to the job's API activity and timeline.

> GitHub delivered the signed label event to this app, which saved one job in
> SQLite. The worker uploaded baseline evidence through Devin's Attachments
> API and created one session capped at 3 ACUs. That session reused a Playbook
> and Knowledge note prepared through their own APIs. The app then polled the
> session and recorded each operation. These are live records, not simulation.

Point out the Playbook and Knowledge status under API activity. The 26 poll
records are repeated reads of one session, not 26 distinct API capabilities.

**1:25 to 2:30 | Independent proof.** Open PR #10's **Files changed**, then
return to the job's **Proof** section.

> Devin changed only the approved test file. It now checks both the datetime
> column type and the actual values for empty, missing, one-row string, and
> already-datetime inputs. The separate validator fetched PR commit
> `f2cf1e45`, ran the original behavior and the registered regression in
> detached checkouts, and got `PASS` followed by `ASSERTION_FAILED`. The
> candidate and validated commit IDs match exactly. The app calls that
> `VERIFIED`; a human still decides whether to merge.

**2:30 to 3:15 | A failure the app refused to hide.** On the timeline, show
`FAILED` followed by `Verified after retry`.

> The first local check could not trust pytest's test ID because macOS gave a
> temporary folder two path spellings. The app reported `INFRA_ERROR` instead
> of a false success. After fixing the validator and rebuilding baselines, an
> operator rechecked the same PR commit. That used no second Devin session or
> correction message.

**3:15 to 4:00 | Operational evidence.** Show the dashboard counters and
`/report.json`, then return to the job verdict.

> GitHub redelivered the same event once. The app counted the duplicate and
> kept one job and one Devin session. The export contains the mode, timestamps,
> API operation outcomes, baseline and candidate verdicts, and exact SHA. It
> is one measured case, not a claim about broad coverage, savings, or a merged
> PR. The next step is human review of PR #10.

## Facts to keep straight

- This live run is issue #9, PR #10, one session, zero correction messages,
  one suppressed duplicate delivery, and one independently verified SHA.
- The test-quality oracle covers this registered one-row regression. It does
  not prove the full Superset suite or every datetime behavior.
- `/evidence` is a separate historical application case, issue #7 and PR #8.
  The simulation at port `8001` is synthetic. Neither belongs in this live
  job's counts.
- The provider reported `0.0` consumed ACUs for this session when checked.
  Do not infer a dollar cost or savings figure from that value.
