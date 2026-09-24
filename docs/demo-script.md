# DevinTrace: five-minute Loom

Audience: a VP of Engineering and senior ICs. Order: **What, How, Why, When.**
Hard stop at 5:00. Record the **LIVE** app at `http://127.0.0.1:8010`. Keep
`.env`, tokens, and terminal history off screen.

One-line thesis to open and close on:

> Devin writes the fix. A separate checker Devin can't touch proves it works.
> A human only adds a label and reviews the PR.

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

> This is DevinTrace. You give it a bug report. Devin writes the fix. Then a
> separate checker, one Devin can't touch, proves the fix really works before
> any human spends time on it. We ran it four times for real on our Superset
> fork. Four fixes, all proven on the first try: three tests that were too
> weak to catch real bugs, and one API that crashed on bad input. The strip
> at the top is the manager's view: how many fixes were proven, how fast,
> and what still needs a person.

**0:45 to 2:15 | How, live.** Start on tab 2 (issue #13).

> It starts with one label. An engineer tags the issue `devin-remediate`.
> GitHub sends us a signed message. We check it really came from GitHub, from
> our repo, and that this issue is one we've approved. Then we save one job
> that survives a restart.

Tab 5, job page: walk **How this run worked** (steps 01 to 06), then the
**Devin API activity** card: **Per run** rows, then **Once per repository**.

> We use four Devin APIs, not just one. Think of it like onboarding a new
> engineer. The **Playbook** is the team handbook: reproduce the bug first,
> make the smallest fix, only touch allowed files, never touch the checker,
> never merge. The **Knowledge** note is the project brief: which branch,
> which files, what "broken" looks like. We set both up once, and fingerprint
> them, so a changed copy gets rejected. Then for each bug, **Attachments**
> hands Devin the evidence, and **Sessions** starts the work with a budget
> cap and checks in until the PR is ready. Here: 41 API calls, zero failures.
> If our app crashes, it finds the session again. If the fix is wrong, it
> sends Devin one correction.

Back to tab 2: point at the status comment.

> Engineers don't need a new dashboard to follow along. This one comment on
> the issue updates itself: working, PR opened, checking, verified. It links
> Devin's session, the exact PR commit, and the result. The reviewer sees the
> same card on the PR.

Tab 3: PR **Files changed**. Optionally flash tab 4, the same card on the PR.

> Devin changed one file, the only one it was allowed to. The bug: this API
> expected text, but got a list or a number and crashed. Now it politely says
> "that's not valid" instead.

Tab 5: job page **Proof** and timeline.

> Now the checker. It grabs that exact commit and tests it on its own. Before
> the fix: broken. After: passes. Same commit Devin pushed, same commit we
> tested, so it's marked verified. Label to proof took about four minutes.

**2:15 to 3:30 | Why you can trust the green check.** Tab 6, oracle file.

> Devin saying "done" isn't proof. Our checker lives outside Devin's reach, so
> Devin can't grade its own homework. First it checks good inputs still work.
> Then it throws three bad inputs at it. If only some are fixed, that's a
> partial fix, and it's rejected.

Tab 7 (`evals/cases.yaml`), then tab 8 (Contracts, readiness).

> Everything is driven by this one list of approved bugs. Each entry says
> where the bug lives, which files Devin may touch, and which checker proves
> it. Before we spend a cent, we confirm the bug actually reproduces. For
> weak tests, we plant a known bug: the old test misses it, Devin's new test
> has to catch it. And if the checker itself breaks, that's never counted as
> a pass. That happened once, on issue #9. We fixed the checker and re-tested
> the same commit, without paying Devin again.

**3:30 to 4:20 | Why this matters to an engineering leader.** Tab 1, leader strip.

> So how would you know it's working? Count proven fixes, not PRs opened.
> Watch time from label to proof. Watch how often Devin gets it right the
> first time; it gets at most one retry. Human effort per fix: one label, one
> review. Cost per fix only shows when Devin reports it; we never guess. And
> it's all available as JSON for your own dashboards.

**4:20 to 5:00 | When to use it.** Tab 8, When to use it.

> Use it for bugs you can check automatically: weak tests, crashes on bad
> input, security scanner findings. Adding a new bug type is one config entry
> and one checker. Don't use it for fuzzy feature work, and it never merges
> on its own. To run it in production, we'd add isolated machines, a login,
> and a real job queue. Devin writes the fix. The checker proves it. A human
> merges.

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
