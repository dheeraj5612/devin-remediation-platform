# Agent notes

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
