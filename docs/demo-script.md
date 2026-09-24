# DevinTrace: five-minute Loom

Audience: a VP of Engineering and senior ICs. Order: **What, How, Why, When.**
Hard stop at 5:00. Record the **LIVE** app at `http://127.0.0.1:8010`. Keep
`.env`, tokens, and terminal history off screen.

One-line thesis to open and close on:

> Devin writes the fix. An independent oracle, which Devin cannot edit, proves
> the fix catches the bug. A human only labels the issue and reviews the PR.

## Pre-recording checklist

1. Web on 8010 is running (`curl -s http://127.0.0.1:8010/healthz` says `LIVE`).
   The worker can stay stopped; everything shown is persisted evidence.
2. Tabs, left to right:
   1. [Dashboard](http://127.0.0.1:8010/dashboard) (What; back here for the leader view)
   2. [Issue #13](https://github.com/dheeraj5612/superset/issues/13#issuecomment-5789275304) at the **DevinTrace status** comment
   3. [PR #14 Files changed](https://github.com/dheeraj5612/superset/pull/14/files)
   4. [PR #14](https://github.com/dheeraj5612/superset/pull/14#issuecomment-5818401237) at the **DevinTrace verification** comment
   5. [Job #13](http://127.0.0.1:8010/jobs/2891e18751e344c0b0ea444990cfc310): How this run worked, Devin API activity, Proof, timeline
   6. [Oracle](https://github.com/dheeraj5612/devin-remediation-platform/blob/main/evals/application_cases/report_anchor_type.py) `report_anchor_type.py`
   7. [Registry](https://github.com/dheeraj5612/devin-remediation-platform/blob/main/evals/cases.yaml) `evals/cases.yaml`
   8. [Contracts](http://127.0.0.1:8010/cases#readiness): readiness, why trust, when to use
   9. [`/report.json`](http://127.0.0.1:8010/report.json)
3. The Devin API card's **Once per repository** group should show Playbooks
   `reused` and Knowledge `created` (refreshed 23 September to list all five cases).
4. Confirm the dashboard reads `LIVE`, **4/4 verified**, 0 needing attention.
   Confirm the job page says `Exact SHA matched`.
5. Browser zoom 110%, notifications off, 1080p.

## Script

**0:00 to 0:45 | What.** Dashboard, leader strip.

> This is DevinTrace. It turns an approved GitHub issue on our Superset fork
> into a Devin pull request, then independently proves the fix before a human
> looks at it. Four live runs, four verified on the first pass: three weak
> tests that let real regressions through, and one API crash that returned a
> 500. The strip at the top is the leader view: verified out of finished,
> time from label to proof, first-pass rate, and what needs a human now.

**0:45 to 2:15 | How, live.** Start on tab 2 (issue #13).

> An engineer adds one label, `devin-remediate`. GitHub sends a signed webhook;
> the app checks the signature, the repository, and that this issue is bound
> to a registered case, then saves one durable job.

Tab 5, job page: walk **How this run worked** (steps 01 to 06), then the
**Devin API activity** card: **Per run** rows, then **Once per repository**.

> Four Devin APIs, not just sessions. Once per repo, a **Playbook** says how
> Devin works: reproduce first, smallest fix, allowed files only, never touch
> the checker, never merge. A **Knowledge** note, generated from our case
> registry, gives the branch, baseline, and allowed files. Both are named by a
> hash of their content, so an edited copy is refused. Per job, **Attachments**
> uploads the baseline proof, and **Sessions** starts one session capped at 3
> ACUs that references all three, then polls it: 41 API calls, zero failures.
> Sessions also handle crash recovery by job tag and one in-session
> correction message.

Back to tab 2: point at the status comment.

> Status is written back where engineers already look. This one comment is
> edited in place as the job moves: running, PR opened, validating, verified.
> It links the Devin session, the PR at an exact commit, which APIs ran, and
> the verdict. The same card sits on the PR for the reviewer.

Tab 3: PR **Files changed**. Optionally flash tab 4, the same card on the PR.

> Devin touched one file, the only path this case allows. Report anchors from
> the API can be a list, a dict, or a number; the old code crashed on all of
> them. Now each becomes a normal validation error.

Tab 5: job page **Proof** and timeline.

> The validator fetched that exact commit into a detached checkout and ran
> our oracle against it. Baseline: regression. Candidate: pass. The candidate
> and validated SHAs match, so the job is `VERIFIED`. Label to verified took
> about four minutes.

**2:15 to 3:30 | Why you can trust the green check.** Tab 6, oracle file.

> Devin's "done" is not the signal. The oracle lives in this repo, outside
> the checkout Devin edits, and proves it loaded the candidate's code. Controls
> first: valid anchors must still pass. Then three bad inputs. All crash is
> the baseline; some crash is `CONTRACT_FAILED`, so a partial fix is rejected.

Tab 7 (`evals/cases.yaml`), then tab 8 (Contracts, readiness).

> The whole system is driven by this registry. Each case pins a baseline
> commit, the files Devin may change, and its oracle. Before any paid run,
> `make baseline` must reproduce the bug. For weak tests we inject a known
> regression: the old test misses it, Devin's new test must catch it. If the
> evaluator breaks, that is `INFRA_ERROR`, never a pass. Issue #9 hit that;
> we fixed the validator and re-checked the same commit with no new session.

**3:30 to 4:20 | Why this matters to an engineering leader.** Tab 1, leader strip.

> How would you know it is working? Verified over finished, not "PRs opened."
> Time from label to independent proof. First-pass rate, so you see how often
> Devin needs a correction; it gets at most one, in the same session. Human
> touches per fix: one label, one review. And ACUs per verified fix, which
> reads "Not reported": we show only provider-reported usage and never
> estimate a cost. The same numbers export as JSON at `/report.json`.

**4:20 to 5:00 | When to use it.** Tab 8, When to use it.

> Use it where a defect can be stated as a contract with an oracle: weak
> tests, crash-on-bad-input bugs, scanner findings. A new case is one YAML
> entry plus one oracle. For production: isolated runners, dashboard auth, a
> real queue. Not for vague feature work, and it never auto-merges.
> Devin writes the fix; the oracle proves it; a human merges.

## Facts to keep straight

- Live jobs: #9 -> PR #10, #2 -> PR #11, #5 -> PR #12, #13 -> PR #14. All
  `VERIFIED`, zero corrections. #9 first stopped at `INFRA_ERROR` and was
  re-validated at the same SHA without a new session.
- #7 was closed as completed by the earlier Devin PR #8 (historical
  `/evidence` archive); it is not in live counts. Duplicate issue #1 and
  superseded PRs #3, #4, #6 were closed with comments.
- Devin APIs: Playbooks and Knowledge notes (setup, once per repo; playbook
  reused, note created). The #13 run used the earlier four-case note; the
  new case's rules reached Devin through its attachment. The note was
  refreshed afterwards to list all five. Attachments: 1 per job. Sessions:
  create 1 per job, 39 polls on #13; list-by-tag recovery and correction
  messages are built and tested but were not needed live.
- Each oracle covers its registered contract only, not the full Superset suite.
- No ACU figure is recorded by the app, so the leader strip reads "Not reported".
  Do not quote a dollar cost or savings.
- Port `8001` simulation is synthetic and never mixed into live metrics.
