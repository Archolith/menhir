# direction — architectural synthesis (read first)

Cluster 0 of the research corpus. The big-picture architecture the rest of the corpus implements. Read these before the mechanism docs.

| Doc | Status | Owns |
|---|---|---|
| `semantic-operating-system.md` | active | Four-layer knowledge model (Source/Structural/Semantic/Institutional), structural-vs-semantic truth boundary, evidence-as-first-class, knowledge-promotion lifecycle, Programs A–E + 6-phase build. |
| `oracle-architecture.md` | active | The runtime stack statement: Layers store → Oracles reason → Combiner synthesizes → Context Engine packages → Mutators write; cold-start pipeline. Concise companion to the SOS doc. |
| `llm-reviewer-seams.md` | speculative | Where a bounded LLM reviewer should exist in menhir — the structural placement of an LLM review seam at the oracle/mutator boundary. |

> `event-fold-view-architecture.md` and `quantstate-agent-counter.md` briefly lived here in July.
> Their three-document cluster was archived under `.agent/archive/plans/` on 2026-08-10 after D0,
> D1, and the Event → Fold → View abstraction shipped and were measured. Use
> `.agent/research/menhir-research-execution-ladder.md` for current status and
> `.agent/architecture.md` / `.agent/data_models.md` for the live mechanism.

Master index: [`../README.md`](../README.md).
