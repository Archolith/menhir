# roadmap index

Build-sequencing and strategic-direction notes. Grouped by altitude so it's clear what authorizes implementation and what is a proposal. None of the proposals or strategic notes is a ladder rung by itself — the dependency-ordered build order lives in [`../../.agent/plans/menhir-research-execution-ladder.md`](../../.agent/plans/menhir-research-execution-ladder.md).

The 0.3.0 MVP release is governed by [`../../.agent/archive/plans/menhir-local-stdio-mvp-release-2026-09-16.md`](../../.agent/archive/plans/menhir-local-stdio-mvp-release-2026-09-16.md) and [tracker #123](https://github.com/Archolith/menhir/issues/123). Nothing below is part of that scope unless the release plan says so.

## Superseded and historical

| Doc | What it was |
|---|---|
| [`menhir-mvp-roadmap.md`](menhir-mvp-roadmap.md) | The July 2026 M0-M5 board. Superseded; now a pointer to the release plan. |
| [`weekend-oracle-runtime-roadmap.md`](weekend-oracle-runtime-roadmap.md) | A past three-day plan for the embedder-blocked window: Oracle Runtime interfaces, Layer-4 schema, Cold Start Brief spec. Historical. |

## Partly built

| Doc | What it sequences |
|---|---|
| [`oracle-integration-plan.md`](oracle-integration-plan.md) | Buildable-now vs gated map + Context Engine sketch + first ColdStartBrief benchmark sketch. The retrieval oracle pieces (R0, R4, R6, R7) are in `src`; the L4 store, ColdStartOracle, Context Engine and brief benchmark are not built. |

## L3/L4 GAP decision-support (proposals, NOT rungs)

The highest-scope-risk overlay (SOS Programs B & D). These compare ways to build it; ctharvey chooses. Neither authorizes implementation.

| Doc | What it offers |
|---|---|
| [`l3l4-overlay-sequencing-options.md`](l3l4-overlay-sequencing-options.md) | Five distinct implementation strategies + comparison matrix + recommended hybrid (C→A→B). |
| [`l3l4-hybrid-sketch.md`](l3l4-hybrid-sketch.md) | The C→A→B hybrid expanded into phases + a decision register flagging the load-bearing choices. |

## Strategic notes (NOT rungs)

Longer-horizon direction. Context, not authorization.

| Doc | Subject |
|---|---|
| [`org-scale-menhir.md`](org-scale-menhir.md) | Org-scale / grouped enterprise direction for menhir. |
| [`doc-drift-watch-mvp.md`](doc-drift-watch-mvp.md) | MVP plan for a doc-drift watch (a strategic slice, not a ladder rung). |

Research mechanism owners live in [`../research/README.md`](../research/README.md).
