# Final integration review

The original concept comparison and two visual critique cycles are in `DESIGN.md` and `CRITIQUE.md`. This additional review closes issues surfaced while preparing PR #7; it does not replace those cycles.

## Corrections

- **Cross-surface evidence integrity:** `linked_verdict` now governs Verified filtering, attention prioritization and the final proof-line count, not just the row and detail badge. Missing, malformed or mismatched commit IDs are not proof. Unlinked stored VERIFIED records get a visible reconciliation warning. Canonical report data and outcome metrics remain unchanged.
- **Installed application:** the historical record is copied exactly into `app/archive/live-application.json`, included in package data and checked for byte parity against the historical source. `scripts/check_wheel.py` exercises the built wheel in a separate interpreter away from the checkout, verifying the landing, archive, contracts and UI/identity assets. Docker's existing `COPY app ./app` includes this record; a Docker runtime test was not performed.
- **Archive validation:** both baseline and candidate require full lowercase hexadecimal commit IDs. A matching placeholder cannot receive a positive verdict.
- **Discoverability and readiness:** a Proof link makes the dated archive directly reachable from the workspace navigation. Blocked readiness checks use the attention color as well as explicit text. The unconfigured LIVE browser fixture now clears its webhook secret instead of retaining the simulation-only value.
- **Repeatability:** the browser workflow is PR/manual-only, cancels superseded runs, and records both the actual checked-out revision and the PR head. The complete review includes a real unlinked-record fixture. The escaping regression test now verifies that encoded input is retained, not merely that raw script text is absent.

## Validation at this stage

The expanded Python suite passed **175 tests** locally. The isolated wheel smoke test passed. Python compilation, JavaScript syntax checking and `git diff --check` passed. The final PR's read-only browser workflow reruns the complete Chromium, Firefox and WebKit journeys; its exact run links, head revision, audit counts and screenshots are recorded in the PR rather than guessed here.

## Findings checked but not adopted

The baseline commit `fb0b150` serves its original workbench at `/`, not `/dashboard`; changing the baseline capture to `/dashboard` would photograph a 404. The captured before images visibly contain the original dashboard. The existing HTML recovery handler also works for FastAPI's HTTPException through Starlette's exception dispatch: both the route regression and all three real browser engines already exercised the 404 recovery page. No redundant handler was added. All production assets currently occupy a flat static directory and the built-wheel check confirms they are present; hypothetical nested assets are not a current packaging failure. Two harmless unused `.mobile-only` rules are not involved in the actual mobile-accounting layout and were left unchanged. The temporary exact-main snapshot workflow was removed when the concurrent documentation update was reconciled; it is not shipped.
