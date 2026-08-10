# direction — architectural synthesis (read first)

Cluster 0 of the research corpus. The big-picture architecture the rest of the corpus implements. Read these before the mechanism docs.

| Doc | Status | Owns |
|---|---|---|
| `semantic-operating-system.md` | active | Four-layer knowledge model (Source/Structural/Semantic/Institutional), structural-vs-semantic truth boundary, evidence-as-first-class, knowledge-promotion lifecycle, Programs A–E + 6-phase build. |
| `oracle-architecture.md` | active | The runtime stack statement: Layers store → Oracles reason → Combiner synthesizes → Context Engine packages → Mutators write; cold-start pipeline. Concise companion to the SOS doc. |
| `llm-reviewer-seams.md` | speculative | Where a bounded LLM reviewer should exist in menhir — the structural placement of an LLM review seam at the oracle/mutator boundary. |

> `event-fold-view-architecture.md` and `quantstate-agent-counter.md` were relocated here
> 2026-07-11, then moved back to `.agent/plans/backlog/` on 2026-08-07 (curator audit):
> both describe fully-shipped mechanisms, not open research, so they belong in the
> operational corpus alongside their companion `aggregation-as-consolidation.md`.

Master index: [`../README.md`](../README.md).
