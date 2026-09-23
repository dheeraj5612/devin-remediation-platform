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
   - [Live dashboard](http://127.0.0.1:8010/dashboard) (leader strip at top)
   - [Issue #13](https://github.com/dheeraj5612/superset/issues/13) scrolled to the **DevinTrace status** comment
   - [PR #14](https://github.com/dheeraj5612/superset/pull/14) on **Files changed**
   - [Job #13 proof](http://127.0.0.1:8010/jobs/2891e18751e344c0b0ea444990cfc310)
   - [Case readiness](http://127.0.0.1:8010/cases#readiness)
   - Editor on `evals/application_cases/report_anchor_type.py` and `evals/cases.yaml`
3. Confirm the dashboard reads `LIVE`, **4/4 verified**, 0 needing attention.
   Confirm the job page says `Exact SHA matched`.
4. Browser zoom 110%, notifications off, 1080p.

## Script

**0:00 to 0:45 | What.** Dashboard, leader strip.

> This is DevinTrace. It turns an approved GitHub issue on our Superset fork
> into a Devin pull request, then independently proves the fix before a human
> looks at it. We filed five issues. Four were remediated live and all four
> are verified on the first pass: three weak tests that let real regressions
> through, and one production crash where a bad API payload returned a 500.
> The fifth was already fixed by an earlier Devin PR and closed. The strip at
> the top is the leader view: verified out of finished, median time from label
> to verified, first-pass rate, and what needs a human right now.

**0:45 to 2:15 | How, live.** Issue #13, then the PR, then the job page.

> An engineer adds one label, `devin-remediate`. GitHub sends a signed webhook;
> the app checks the signature, the repository, and that this issue is bound
> to a registered case, then saves one durable job. A single worker uploads
> the baseline evidence, starts a Devin session capped at 3 ACUs with a shared
> Playbook and Knowledge note, and polls it.

Point at the status comment on the issue.

> Status is written back where engineers already look. This one comment is
> edited in place as the job moves: running, PR opened, validating, verified.
> It links the Devin session, the PR at an exact commit, and the verdict.

Open the PR's **Files changed**.

> Devin touched one file, the only path this case allows. Report anchors from
> the API can be a list, a dict, or a number; the old code crashed on all of
> them. Now each becomes a normal validation error.

Open the job page **Proof** and timeline.

> The validator fetched that exact commit into a detached checkout and ran
> our oracle against it. Baseline: regression. Candidate: pass. The candidate
> and validated SHAs match, so the job is `VERIFIED`. Label to verified took
> about four minutes.

**2:15 to 3:30 | Why you can trust the green check.** Editor, oracle file.

> Devin's own "done" is not the signal. The oracle lives in this control-plane
> repo, outside the checkout Devin edits, and it proves it imported the
> candidate's code, not an installed copy. It runs controls first: valid
> anchors must still pass and an unknown tab must still be reported. Then it
> sends the three bad inputs. All three crash: that is the baseline
> regression. Some crash: `CONTRACT_FAILED`. A partial fix that only catches
> one type is rejected. I tested that against a real partial patch.

Switch to `evals/cases.yaml`, then case readiness.

> Each case pins a baseline commit, the files Devin may change, and its
> oracle. Before any paid run, `make baseline` must reproduce the bug. For
> weak-test cases we inject a known regression and require the old test to
> miss it and Devin's new test to catch it. If the evaluator itself breaks,
> that is `INFRA_ERROR`, never a pass. Issue #9 hit exactly that on macOS; we
> fixed the validator and re-checked the same commit without paying for a
> second session.

**3:30 to 4:20 | Why this matters to an engineering leader.** Dashboard.

> How would you know it is working? Verified over finished, not "PRs opened."
> Time from label to independent proof. First-pass rate, so you see how often
> Devin needs a correction; it gets at most one, in the same session. Human
> touches per fix: one label, one review. And ACUs per verified fix, which
> shows "Not reported" because the API reports 0.0 for these sessions; we do
> not invent a cost figure. The same numbers export as JSON at `/report.json`.

**4:20 to 5:00 | When to use it.**

> Use it where a defect can be stated as a contract with an oracle: weak
> tests, crash-on-bad-input bugs, dependency or scanner findings. Adding a
> case is one YAML entry plus one oracle. Next steps for production: isolated
> runners, auth on the dashboard, and a queue beyond one SQLite worker. It is
> not for vague feature work, and it never auto-merges.
> Devin writes the fix; the oracle proves it; a human merges.

## Facts to keep straight

- Live jobs: #9 -> PR #10, #2 -> PR #11, #5 -> PR #12, #13 -> PR #14. All
  `VERIFIED`, zero corrections. #9 first stopped at `INFRA_ERROR` and was
  re-validated at the same SHA without a new session.
- #7 was closed as completed by the earlier Devin PR #8 (historical
  `/evidence` archive); it is not in live counts. Duplicate issue #1 and
  superseded PRs #3, #4, #6 were closed with comments.
- Each oracle covers its registered contract only, not the full Superset suite.
- Provider-reported ACUs were `0.0`. Do not quote a dollar cost or savings.
- Port `8001` simulation is synthetic and never mixed into live metrics.
