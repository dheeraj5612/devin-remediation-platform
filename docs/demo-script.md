# DevinTrace: five-minute Loom

Audience: a VP of Engineering and senior ICs. Order: **What, How, Why, When.**
Hard stop at 5:00. Record the **LIVE** app at `http://127.0.0.1:8010`. Keep
`.env`, tokens, and terminal history off screen.

One-line thesis to open and close on:

> Devin writes the fix. A separate checker Devin can't touch proves it works.
> A human only adds a label and reviews the PR.

## Pre-recording checklist

1. Stack runs in containers: `docker compose -f docker-compose.yml -f docker-compose.live.yml --profile live up -d`.
   `curl -s http://127.0.0.1:8010/healthz` says `LIVE`. Everything shown is persisted evidence.
2. Tabs, left to right:
   0. [Landing](http://127.0.0.1:8010/) (What; later Architecture, Why Devin, When)
   1. [Dashboard](http://127.0.0.1:8010/dashboard) (leader strip)
   2. [Issue #13](https://github.com/dheeraj5612/superset/issues/13#issuecomment-5789275304) at the **DevinTrace status** comment
   3. [Job #13](http://127.0.0.1:8010/jobs/2891e18751e344c0b0ea444990cfc310): How this run worked, Watch Devin work, Devin API activity, Proof
   4. [Devin session](https://app.devin.ai/sessions/80ee022794704af7aee70293bba9790d) for #13 (keep sidebar and secrets out of frame)
   5. [PR #14 Files changed](https://github.com/dheeraj5612/superset/pull/14/files)
   6. [Oracle](https://github.com/dheeraj5612/devin-remediation-platform/blob/main/evals/application_cases/report_anchor_type.py) `report_anchor_type.py`
   7. [Registry](https://github.com/dheeraj5612/devin-remediation-platform/blob/main/evals/cases.yaml) `evals/cases.yaml`
   8. [Contracts](http://127.0.0.1:8010/cases#readiness): readiness, why trust
   9. [`/report.json`](http://127.0.0.1:8010/report.json) (optional)
3. The Devin API card's **Once per repository** group should show Playbooks
   `reused` and Knowledge `created` (refreshed 23 September to list all five cases).
4. Confirm the dashboard reads `LIVE`, **4/4 verified**, 0 needing attention.
   Confirm the job page says `Exact SHA matched`.
5. Browser zoom 110%, notifications off, 1080p.

## Script

**0:00 to 0:35 | What.** Tab 0, landing hero. Then tab 1, leader strip.

> Every engineering team has a backlog of small, well-defined bugs: tests
> too weak to catch regressions, APIs that crash on bad input, scanner
> findings. Nobody gets to them, so real risk sits unfixed. And when an AI
> writes a fix, a senior reviewer still has to work out whether it actually
> works, so the time saved goes straight back into checking. DevinTrace
> solves both.
> Devin writes the fix. A separate checker Devin can't touch proves it works.
> A human only adds a label and reviews the PR. We ran it four times for real
> on our Superset fork: four fixes, all proven on the first try.

Tab 1:

> This strip is the manager's view: proven fixes, time to proof, and what
> still needs a person.

**0:35 to 2:15 | How, live.** Tab 2, issue #13.

> It starts with one label. An engineer tags the issue `devin-remediate`.
> GitHub sends us a signed message. We check it came from our repo and that
> this issue is one we've approved, then save one job that survives a restart.

Tab 3, job page: **How this run worked**, then the **Devin API activity** card.

> We use four Devin APIs. The **Playbook** is the team handbook: reproduce
> first, smallest fix, allowed files only, never touch the checker, never
> merge. **Knowledge** is the project brief: branch, files, what "broken"
> looks like. Both are set up once per repo. **Attachments** hand Devin the
> evidence for this bug, and **Sessions** starts the work with a budget cap
> and checks in until the PR is ready. 41 calls, zero failures.

Click **Open the Devin session** (tab 4). Scroll through the work.

> Here's Devin actually doing it. It got our brief and evidence through the
> API, read the code, reproduced the crash, made a one-file fix, ran checks,
> and opened the PR. Nobody was driving; our app just checked in.

Tab 5, PR **Files changed**.

> One file, the only one it was allowed to touch. The API expected text, got
> a list or a number, and crashed. Now it returns a clean validation error.

Tab 2, status comment.

> Engineers follow along right here: one comment that updates itself.
> Working, PR opened, checking, verified. The reviewer sees the same card on
> the PR.

Tab 3, **Proof**.

> Then the checker grabs that exact commit and tests it on its own. Before:
> broken. After: passes. Same commit Devin pushed, same one we tested. Label
> to proof: about four minutes.

**2:15 to 3:00 | How: architecture.** Tab 0, **Key architectural decisions**.

> A few decisions make this safe to run unattended. Only approved issues
> start work. The job is saved before we pay for anything, so GitHub retries
> never double-bill. Every Devin session is tagged, so if we crash we find it
> again instead of starting over. And the checker is a separate trust plane:
> it checks out the exact commit, rejects edits outside the allowed files,
> strips secrets, and only it can say verified.

**3:00 to 3:40 | Why trust the green check.** Tab 6 oracle, tab 7
`cases.yaml`, tab 8 Contracts.

> The checker lives outside Devin's reach. Good inputs must still work, and
> all three bad inputs must be rejected; a partial fix fails. Everything comes
> from this one list of approved bugs: where it lives, which files Devin may
> touch, which checker proves it. We confirm every bug reproduces before
> spending a cent, and a broken checker never counts as a pass.

**3:40 to 4:20 | Why Devin.** Tab 0, **Why Devin is the right tool here**.

> Why Devin? Without an autonomous agent this isn't practical: every bug
> needs either an engineer at the keyboard or custom code written for it.
> A codemod can't read intent or write a
> new test. A copilot needs an engineer driving every step. A raw model call
> has no repo, no shell, no tests; we'd have to build the whole agent
> ourselves. Devin has its own machine, and its API already gives us budget
> caps, tags, follow-up messages, and team rules. Our part is the trigger,
> the scope, and the proof.

**4:20 to 5:00 | When.** Tab 1 leader strip, then tab 0 **When**.

> How would a leader know it's working? Count proven fixes, not PRs. Time to
> proof. First-try rate. One label and one review per fix, all exportable as
> JSON. Next, in a real customer engagement: week one, plug in their scanner or Jira feed. Pilot
> ten to twenty cases with agreed targets. Then grow the case library and run
> sessions in parallel. Devin writes the fix. The checker proves it. A human
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
