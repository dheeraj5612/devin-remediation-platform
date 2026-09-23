# Agent notes

## Current documentation refresh

- The walkthrough is checked against the current DevinTrace multi-page layout: `/` overview, `/dashboard` workbench, `/cases` contracts, `/evidence` archive, `/jobs/{job_id}` detail, and the JSON, health, metrics, and webhook endpoints. It uses file links without fragile source-line anchors.
- `docs/app-walkthrough.md` separates simulation, the local source comparator, and the recorded live application result. It omits provider session and durable job identifiers and links the public candidate evidence instead.
- The review fixes also change launch preflight, repository-scoped context naming, and recursive oracle fingerprinting. Any future live proof must be regenerated after those changes are integrated.
- The review-fixes worktree's full suite passed 226 tests, Ruff passed, and `git diff --check` passed. The documentation refresh is intentionally separate from runtime fixes pending integration review.

## Scope and base

- Integration base: `cb0ddd1`.
- Final integrated branch: breadth, readiness, simulation, and presentation changes are complete on `codex/breadth-presentation` at head `93776c773c0d784ef47d8a8d5d9782a1a1f8a0b9`.
- Draft PR [#4](https://github.com/dheeraj5612/devin-remediation-platform/pull/4) is mergeable. GitHub Verify passed in both [run 35677656472](https://github.com/dheeraj5612/devin-remediation-platform/actions/runs/35677656472) and [run 35677659445](https://github.com/dheeraj5612/devin-remediation-platform/actions/runs/35677659445).

## Decisions

- Preserve the durable SQLite job and event timeline, signed webhook admission, repository and issue binding, allowed path checks, one session correction bound, restart recovery, and independent validation.
- Add a third application workflow for the Superset malformed YAML defect. Its trusted oracle remains outside the candidate checkout and requires candidate provenance.
- Keep dashboard and report evidence boundaries explicit. Simulation is labelled synthetic, live proof requires current baselines and operator gates, and the product does not claim merge, ROI, ACU savings, or customer impact without measured records.
- Keep the test quality cases tied to Superset test baseline `5ecb19cf92ae8dedbf5b33ec92324cc77c4ee10e`.
- Keep the application case tied to baseline `dedfe23a805151decca6deaf15b032e040e42e82`; its known fixed reference is `22ec1f598808859c42dccff21665786224127122`.

## Verification

- Maintained code annotation checks, Ruff `0.16.8`, Python compilation, diff whitespace checks, and docstring-stripped AST equivalence passed for the maintained source.
- The full suite passed **133 tests** under Python `3.14.7`; GitHub Verify also passed the suite under Python `3.12.14`.
- Commit `93776c7` makes the live-shaped test fixture point at the checkout and running interpreter, so CI does not require a developer-only `.superset` installation.
- The local source oracle confirmed baseline `dedfe23a805151decca6deaf15b032e040e42e82` as `REGRESSION` and known reference `22ec1f598808859c42dccff21665786224127122` as `PASS`, both with trusted provenance. The sanitized record is [`evidence/application-oracle.json`](../evidence/application-oracle.json), with ignored baseline proof at `data/live/baselines/import-unparseable-yaml.json`.

## Follow up

- Fresh live application proof is recorded in [`evidence/live-application.json`](../evidence/live-application.json); it covers only the malformed-YAML case and does not claim a merged change, full Superset suite, customer impact, or cost savings.
- Keep the ignored `.env` protected and keep `ENABLE_LIVE=false` after the completed pilot unless a new run is explicitly approved with current gates.
- The source comparator remains local validator evidence. It is separate from the live Devin candidate result.

## Comment coverage integration

- The annotation pass is comment/docstring-only from source baseline `fb0b150df3fa09376a24c2642102aab1ee2e5c7b`; historical live-proof files and fingerprints are unchanged.
- All 18 maintained Python files match their source AST after stripping docstrings. The semantic audit covers 1,059/1,059 substantive statements with an inline or adjacent explanation, excluding imports, punctuation-only structure, docstring expressions, and `pass`; all 146 classes/functions/methods have summaries.
- Jinja comment stripping and the same fixture render are unchanged. Python compilation, `ruff check .`, and whitespace checks pass. The existing 133-test result remains applicable because this branch changes documentation only.

## Non-Python and asset audit

Reviewed 22 September 2026 against `945aa10`. The audit covered deployment files,
workflow configuration, package metadata, templates, static identity assets,
case and evidence records, findings, and design documents.

Changes made:

- Added plain-language ELI5 comments immediately above all three macros in
  `app/templates/macros.html`.
- Renamed stale Proofline temporary paths and artifact names in the browser
  review workflow to DevinTrace names.
- Updated the package description to cover both code and test remediation.
- Ignored the review checkout and UI review output in Docker build context.
- Replaced the manifest's em dash with a colon in the installed product name.

No-change rationale by category:

- `.gitignore`, Makefile, Dockerfile, dependency constraints, and UI
  requirements already match the supported Python, wheel, and runtime layout.
  The package description was corrected to include application code remediation;
  no command or package behavior was expanded.
- The verify workflow and the browser matrix's pinned baseline, permissions,
  timeouts, and dependency installation are internally consistent. Only the
  stale product names and their matching paths were corrected.
- `DESIGN.md` and `docs/design/*` are dated design and review records. They
  were preserved as historical evidence, including their original product
  terminology and measured claims.
- `evals/cases.yaml`, `findings/*`, `evidence/*.json`, and
  `app/archive/live-application.json` are source-backed or immutable evidence.
  Their SHA, fingerprint, provenance, and scope fields were cross-checked and
  left unchanged.
- SVG, PNG, and ICO identity assets are valid packaged files with matching
  manifest references. No binary or visual asset change was justified.
- `app/__init__.py`, `evals/__init__.py`, and `tests/__init__.py` are empty
  package markers and need no content.

Validation for this audit included YAML and JSON parsing, evidence-to-case
baseline matching, Jinja template rendering through the existing test suite,
and `git diff --check`.

## Datetime edge-case case

- Added `superset-normalize-dttm-edge-cases` for `tests/unit_tests/utils/test_date_parsing.py::test_edge_cases`, pinned to fork baseline `5ecb19cf92ae8dedbf5b33ec92324cc77c4ee10` on `remediation-demo`. Devin scope allows only the test file.
- Added the trusted `normalize-dttm-skip-single-row` challenge. Its mutant skips `_process_datetime_column` only for one-row frames, while its positive control verifies a two-row conversion and the clean versus mutant one-row behavior.
- Python 3.11.14 with the local `.venv311` confirmed baseline `normal=PASS`, `mutant=PASS`, `outcome=CONFIRMED`; both reports recorded candidate-checkout provenance and active controls. The proof is at the ignored `data/live/baselines/superset-normalize-dttm-edge-cases.json`.
- Focused registry tests, the full Python suite, Ruff, compilation, and whitespace checks pass. The trusted challenge has only been exercised against the pinned baseline and does not validate a future candidate repair.
