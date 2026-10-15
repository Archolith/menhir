# Memory Atlas v3 — high-fidelity browser mockups

**Status:** standalone browser UX concept, not a Menhir implementation. All six example records, dates, source IDs, counts and authority indicators are synthetic fixtures. The mockup never calls an API or reads production Menhir.

## Open and navigate

Open [index.html](index.html) in any modern browser. It has **all HTML, CSS, JavaScript and SVG inline**, so there is no build step, no package installation and no external asset dependency.

The left navigation and hash fragments expose five linked pages:

| Page | URL fragment | Key interaction |
| --- | --- | --- |
| Overview | `#overview` | Local search, current/historical filters, featured memory |
| Memory reader | `#reader` | Expand anchored source text in place; independently reveal evidence, history and related memories; follow edges |
| View Studio | `#view` | Switch scalar_state/scalar_history/timeline/METRIC; click Observe → Fold → Project and inspect contributors |
| Scalar Explorer | `#scalar` | Switch anchored, no-anchor, ambiguous-anchor and expired cases; inspect asserted operations |
| Evidence trail | `#evidence` | Choose derived View / receipt / original document to inspect each provenance identity |

Use the **Light / Dark** button to compare palettes; press **/** to focus search. Narrow screens expose a horizontal, scrollable top navigation. Keyboard buttons and reduced-motion styling are included for review.

## Design qualities to evaluate

1. Memory reading is the primary experience, not a mandatory global graph.
2. View type, ViewClass, ViewAudience and currentness are immediately apparent. **Current materialization ≠ current-state authority.**
3. Scalar currentness is **only a display of the canonical fold result** (synthetic here). The browser must not compute a production value. Missing anchor and conflicting latest absolutes abstain; expiry is not zero.
4. Evidence and source quotations have separate identities: View UUID, receipt UUID, Graphiti episode UUID, and source identity must never be conflated.
5. A dedicated evidence trail supports forensic inspection without overwhelming a short memory statement.

## Product boundary and engineering follow-up

- This v3 prototype is an additional **visual-design target** for [the refined execution plan](../../../.agent/plans/menhir-progressive-memory-inspector-2026-10-14.md). It does not change the P0–P6 sequencing or authorize implementation.
- The older [v2 multi-page prototype](../memory-atlas-v2/index.html) and its [View and Scalar study](../memory-atlas-v2/views-scalars.html) remain accessible as earlier interaction explorations.
- Backend implementation must reuse existing Menhir Explorer auth, namespace selection, View repository, scalar folds, source grants, evidence and time contracts. Feature flag and security gates remain prerequisites.
- **Future project-status briefing is explicitly out of scope**; it remains a distinct product interface rather than an inferred project View.
- The v3 design mock is intentionally small/fixture-only: no real pagination, no authenticated reads, no request concurrency, no source erasure checks, and no graph writes.

## Local validation

Browser-based smoke checks were performed on the exported standalone file using a headless Chromium Playwright session, with no network dependencies:

- Five page routes, theme switch, search and filter behavior.
- Memory inline expansion and source disclosure.
- View kind and derivation-stage changes.
- Four scalar sample outcomes and per-row assertion disclosure.
- Provenance selection.
- Responsive layout at 390px with no horizontal overflow.
- No JavaScript console page errors in the tested flows.

These checks validate **mockup interaction only**. They are not evidence of Menhir backend integration, auth correctness or release readiness.
