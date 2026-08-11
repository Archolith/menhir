# roadmap index

Build-sequencing and strategic-direction notes. Grouped by altitude so it's clear what authorizes implementation and what is a proposal. None of the proposals or strategic notes is a ladder rung by itself — the dependency-ordered build order lives in [`../../.agent/research/menhir-research-execution-ladder.md`](../../.agent/research/menhir-research-execution-ladder.md).

## Active build sequencing

Concrete short-horizon plans tied to current work.

| Doc | What it sequences |
|---|---|
| [`menhir-mvp-roadmap.md`](menhir-mvp-roadmap.md) | Current local-MVP path reconciled against `main`, recent plans, and reviews: fresh benchmark, Phase 3 rollout, Hook Center rollout, and local operator hardening. |
| [`weekend-oracle-runtime-roadmap.md`](weekend-oracle-runtime-roadmap.md) | The embedder-blocked window: Oracle Runtime interfaces, Layer-4 schema, Cold Start Brief spec (Days 1–3). Spec, not built code. |
| [`oracle-integration-plan.md`](oracle-integration-plan.md) | Day-3 capstone: buildable-now vs gated map + Context Engine sketch + first ColdStartBrief benchmark sketch. Nothing here is built. |

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
