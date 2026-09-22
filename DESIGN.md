# Proofline — an evidence workbench

Proofline is the product identity for the **Devin Remediation Platform** UI. It is not an official Devin product or a replacement for the underlying orchestration system.

## Audience and the job to be done

The audience is an engineering reviewer or operator evaluating an agent-produced repair. This audience is inferred from the repository's actual HMAC admission path, approved case registry, bounded correction loop, independent validator, persisted evidence, and take-home presentation workflow—not from invented user interviews.

The important question is not “Did the agent finish?” It is **“What exactly was checked, against which candidate, and what should I review next?”** The UI must make a candidate, a simulated success, a recorded live result, and a human approval visibly different things.

### Inspection findings

The starting application had one server-rendered dashboard: generic rounded KPI cards, a donut duplicating status counts, a gradient funnel, a wide table, and selected-job evidence below the entire portfolio. It lacked a landing page, navigation, product identity, search, filtering, distinct error recovery, and a mobile-first review order. The existing logic and normalized `app/report.py` were substantially stronger than their presentation.

The actual six-job simulation records four terminal verified states and two escalations, with first-pass verification **3/6** and correction recovery **1/3**. These are synthetic adapter outcomes. The separate checked-in live application record contains a specific malformed-YAML oracle acceptance and matching candidate/validated SHA. It is dated, narrowly scoped, and **never merged into workspace metrics**.

## Research: borrow interaction principles, not surfaces

Reviewed 22 September 2026 UTC:

- [Linear Triage](https://linear.app/docs/triage): a review queue separates incoming work from accepted workflow. Transfer: attention-first ordering and URL-addressable filters. Do not copy the sidebar, monochrome application shell, or keyboard command model wholesale.
- [Checkly Results](https://www.checklyhq.com/docs/concepts/results/): result inspection includes per-run evidence and distinguishes attempts/retries. Transfer: verdict → proof → exact artifacts, with explicit denominators. Do not invent an uptime-style timeseries for six synthetic runs.
- [Resend Logs](https://resend.com/docs/dashboard/logs/introduction): searchable records link directly to their supporting detail. Transfer: a restrained ledger, inspect links, and a reversible path back to the filtered view. Do not show unfiltered export as a filtered export.
- [CommandGate](https://www.commandgate.ai/): evidence and review precede an action. Transfer: make the human boundary explicit. This platform has no browser-side execution/merge affordance, so none is invented.
- Approved Print Margin direction: carry over concise proposition and input/result adjacency, not its palette or composition. The previous approval context was available; its preview URL was not available for a fresh visual inspection in this session.

## Three genuinely different concepts

The companion [composition studies](docs/design/concepts.html) use the same explicitly simulated dataset.

| Direction | Art direction and working model | Strength | Why selected / rejected |
|---|---|---|---|
| **Graphite Dispatch** | Dark technical console, phosphor signals, branch topology, monospaced terminal rows | Dense and operational | Rejected: familiar “developer console” territory; the session looks more important than the evidence |
| **Audit Casebook** | Warm paper, burgundy ink, large serif editorial headings, numbered folios | Distinctive and careful | Rejected: too much of a static report metaphor for active runs, filters, and changing candidate state |
| **Cobalt Evidence Desk / Proofline** | Warm-white field, electric cobalt, sharp rules, oversized native grotesk type, commit-like proof marks, sparse lime acceptance | Strong product identity with high information clarity | **Selected:** an inspectable ledger and explicit before/after proof make the core claim visible without a charting framework or ornamental dashboard cards |

## The selected system

### Identity

The original mark is a pair of incoming rails joining a single checkpoint. It appears in the interface's proof line and human-review boundary. `app/static/mark.svg` is its canonical vector. `wordmark.svg` is an original geometric path treatment, not a downloaded font or a vendor logo. The same source generates an SVG favicon, multi-size ICO, touch icon, and a social preview; no font files are distributed.

### Color

| Token | Value | Semantic use |
|---|---|---|
| Paper | `#f8f9f5` | Quiet working surface |
| Ink | `#18251f` | Body type, strong rules, handoff strip |
| Muted | `#58655e` | Supporting metadata, never the only state cue |
| Cobalt | `#2455ef` | Navigation, primary actions, evidence links |
| Cobalt wash | `#eef2ff` | Independent comparison header / readiness |
| Lime | `#d8f56a` | Checkpoint emphasis on dark or cobalt fields |
| Acceptance | `#285724` on `#edf5df` | Accepted oracle result |
| Attention | `#9f3425` on `#fff1e9` | Escalation, failure, surviving regression |
| Simulation | `#80580f` on `#fff6dc` | Mode boundary, not a live success cue |

Every status has visible wording; icons supplement it. Green never means merged or approved. A test mutant's `ASSERTION_FAILED` is desirable only in the proper normal/mutant context; an application contract has a different result model.

### Typography, composition, and motion

Use the native Arial/Helvetica sans stack, with aggressively edited scale and spacing rather than remote font downloads. The landing proposition is 65–105px; operational headings are 36–52px. A native monospace stack differentiates IDs, labels, and evidence coordinates. The custom path wordmark provides a separate brand voice. Main content is bounded at 1280px, with 56px desktop and 20px mobile side gutters. Corners are 0–3px: no floating card grid, glass, purple gradient, or blob decoration.

Spacing follows a compact 4/8/12/16/24/32/48/64 family. Information hierarchy is structural: large proposition, flat accounting strip, aligned ledger, ruled proof comparison. Motion is limited to brief hover transitions and is removed under `prefers-reduced-motion`. There are no looping animations or simulated progress counters.

## Signature interface: the proof line

The landing shows a **dated recorded example**, with baseline and candidate tied to exact evidence. The workbench's handoff line shows persisted counts at admission, sessions, candidate discovery, evaluation, and verification. These are counts of records, not a duration plot or a claim of conversion probability. Human review is deliberately outside the measured stages.

A run detail puts the two sides of the repair next to each other. Application cases show the application oracle. Test-repair cases show both normal and mutant controls, explaining why a mutant assertion failure is desirable. Candidate and validated SHAs are adjacent and copyable. A `VERIFIED` state with a missing/mismatched SHA receives an **unlinked verification warning**, not a green endorsement. The canonical report and underlying metrics are not rewritten by this presentation guard.

## Core experience

- `/`: public-facing proposition, source-backed recorded example, concise responsibility chain, honest links into the product. The public origin for canonical/social metadata is derived from the actual request; configure trusted proxy/host handling when deploying.
- `/dashboard`: full workspace accounting, a signature handoff line, GET search (issue/case/ID/SHA), workflow and state filters, deterministic attention/newest/oldest ordering, 20-row pagination, and a full-evidence export.
- `/jobs/{id}`: verdict first, proof comparison, exact artifacts, review boundary, and a milestone trail that expands to **all** persisted events. Back links preserve the normalized filter state.
- `/cases`: approved contracts, allowed paths, acceptance rules, and live-readiness gates. Copying `make doctor` does not run it.
- `/evidence`: explicitly dated checked-in live application evidence. No connection to the active database is implied.

Tab counts follow the search and workflow filter. Rows additionally follow the selected state. Portfolio metrics and workflow counts always cover the full configured storage. **Export all evidence** says what it does and is intentionally not filtered. All core navigation, filtering, disclosure, and export work without JavaScript.

### Mobile is a different reading order, not a shrunken desktop

The workbench places the ledger before accounting and charts. Each run is a readable two-column record with a full-width finding and an explicit Inspect target. It never requires horizontal scrolling. Detail pages place the review boundary before the long event trail, with metadata in a native disclosure. Inputs use 16px text on narrow screens; main controls are at least 44px high.

### States and recovery

- Initial content is server-rendered, not a fake skeleton.
- Refresh has a real busy state, disables duplicate refresh, and preserves filters, draft input, focus, open disclosures, event expansion, and scroll position.
- Successful refresh explicitly says no work was started. A failed refresh retains the last snapshot, announces the failure, and allows retry; errors can be dismissed by touch or Escape.
- An empty database and an empty filter result have different explanations and recovery links. No data is not treated as a storage failure, and a storage failure does not become a zero total.
- Queued/active jobs, missing timestamps/artifacts, unavailable original contracts, missing archive data, unlinked verified states, HTTP 404, and storage HTTP 503 have explicit presentations.
- Clipboard denial selects the exact value for manual copying rather than silently claiming success.

## Engineering and safety boundaries

Keep FastAPI/Jinja and the established report model. `app/views.py` is read-only route composition; `app/presentation.py` contains pure filter/format helpers. Extracted local CSS and progressive JavaScript have no runtime package/build step. `pyproject.toml` includes static files in package data. The original admission, idempotency, worker, bounded correction, validator, and no-auto-merge logic remain intact.

No browser route creates a Devin session, starts a worker, enables LIVE mode, resets jobs, or merges a PR. No simulation link is presented as a real GitHub artifact. The existing JSON report schema remains unchanged.

Use a strict same-origin CSP: no inline/remote scripts, no remote styles, no object embeds, self-only form targets, and no framing. Operational pages and JSON are uncached and non-indexable. **Noindex and read-only are not authentication.** This app still needs a private network or an authenticated gateway for operational pages, as already documented in the repository.

## Accessibility and validation

Native labelled controls, search/navigation landmarks, table captions and explicit roles, visible focus, skip link, status/error announcements, keyboard `/` search, Escape dismissal, reduced-motion support, selectable exact values, and a fully usable no-JavaScript path are part of the implementation. The mobile table retains explicit table/row/cell roles when CSS changes its display layout.

The browser review is reproducible through `scripts/ui_review.py` and the `Product UI review` workflow. It launches isolated running applications for populated simulation, empty storage, disabled live mode, a storage failure, queued work, and a 45-record pagination fixture. It exercises real navigation, GET filters, JSON download, refresh loading/success/failure, copy fallback, event expansion, keyboard focus, no-JS operation, responsive reflow, and axe checks. It never uses paid adapters.

See [the critique log](docs/design/CRITIQUE.md) and the actual workflow artifacts for the performed checks, before/after screenshots, and remaining limits. Automated axe results are not a claim of manual screen-reader or complete WCAG certification.
