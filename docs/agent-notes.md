# Agent notes

## Scope and base

- Integration base: `cb0ddd1`.
- Final integrated branch: breadth, readiness, simulation, and presentation changes are complete on `codex/breadth-presentation`.

## Decisions

- Preserve the durable SQLite job and event timeline, signed webhook admission, repository and issue binding, allowed path checks, one session correction bound, restart recovery, and independent validation.
- Add a third application workflow for the Superset malformed YAML defect. Its trusted oracle remains outside the candidate checkout and requires candidate provenance.
- Keep dashboard and report evidence boundaries explicit. Simulation is labelled synthetic, live proof requires current baselines and operator gates, and the product does not claim merge, ROI, ACU savings, or customer impact without measured records.
- Keep the test quality cases tied to Superset test baseline `5ecb19cf92ae8dedbf5b33ec92324cc77c4ee10e`.
- Keep the application case tied to baseline `dedfe23a805151decca6deaf15b032e040e42e82`; its known fixed reference is `22ec1f598808859c42dccff21665786224127122`.

## Verification

- Maintained code annotation checks, Ruff `0.16.8`, Python compilation, diff whitespace checks, and docstring-stripped AST equivalence passed for the maintained source.
- The full suite passed **133 tests** under Python `3.14.7`; the final test log is `/private/tmp/drp-breadth-evidence-final5/run-mjve6o23/output.log`.
- The local source oracle confirmed baseline `dedfe23a805151decca6deaf15b032e040e42e82` as `REGRESSION` and known reference `22ec1f598808859c42dccff21665786224127122` as `PASS`, both with trusted provenance. The sanitized record is [`evidence/application-oracle.json`](../evidence/application-oracle.json), with ignored baseline proof at `data/live/baselines/import-unparseable-yaml.json`.

## Follow up

- Live proof remains pending. Keep the ignored `.env` protected and keep `ENABLE_LIVE=false` until local gates, current baseline evidence, and the disposable validation environment pass.
- The oracle result is local validator evidence only. It is not a Devin session or candidate PR result.
