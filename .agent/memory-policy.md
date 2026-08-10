# Memory Policy

Compact policy companion for [memory-design.md](memory-design.md).

Use this file first when you need behavior and policy without loading the full design document.

## Scope

This file covers:

- graph semantics
- scoring
- memory types
- emotional quotient
- edge behavior
- scope and lifecycle

## Sections

### `memory.policy.graph`
- source sections: `Core Data Structure`
- use for:
  - what counts as a node or edge
  - why the system is graph-first, not tree-first
  - episode anchor semantics

Key points:
- memories are graph nodes, not tree leaves
- one concept may be reachable from several branches
- episode nodes exist for provenance and queueing, not just recall

Detail notes:
- the system is explicitly not a tree because a useful memory may sit under several conceptual branches at once
- cycles are natural and should be handled by traversal limits and visited-set tracking, not forbidden in the structure
- episodic anchors are first-class graph objects for provenance, but default recall should not treat them like durable knowledge nodes
- retrieval supports both direct lookup and bounded traversal from any relevant node

### `memory.policy.scoring`
- source sections: `Scoring`
- use for:
  - retrieval ranking
  - adjacency / recency / prominence behavior
  - preset intent tuning

Key points:
- recall relevance and lifecycle sharpness are separate
- candidate generation is two-phase
- prominence relies on cached graph signals rather than full scans at read time

Detail notes:
- relevance combines semantic similarity with adjacency, recency, and prominence rather than relying on embeddings alone
- candidate generation is vector search first, then local graph scoring on a bounded candidate set
- prominence is intentionally cached via `edge_count`; if that cache is unsynced, the prominence lane should safely degrade to zero instead of inventing confidence
- preset tuning changes the weighting shape by intent without changing the underlying scoring lanes
- a minimum similarity threshold (0.15) gates candidates before scoring — but this is a RANK CUT on graphiti's RRF reranker score (a dual-method top hit ≈ 2.0 under rank_const=1), not a cosine [0,1] cutoff; sub-threshold matches are excluded regardless of other signals. Provenance-injected sources (pending/file-linked/fact-edge) are floor-exempt. The 0.15/RRF vs [0,1]-prior scale mix is a known accident (see `scoring_service.GRAPHITI_RRF_DUAL_METHOD_MAX` and plan 1b's `similarity_scale="normalized"`).
- relevance tier labels (high/medium/low) are derived from the semantic-similarity lane (currently the raw RRF score), not the final combined score

### `memory.policy.types`
- source sections: `Memory Types`
- use for:
  - type-specific expectations
  - why episodic, semantic, procedural, preference, and identity memories behave differently

Key points:
- not all memories are emotional
- type affects how retention and retrieval should be interpreted

Detail notes:
- episodic and preference memories may carry emotional signal directly, while semantic and procedural memories mainly rely on uniqueness and graph importance
- identity memories are usually protected and may bypass normal decay behavior
- type is assigned during extraction but may be corrected later by consolidation or user action
- type should eventually act as a content contract, not only a classification label

### `memory.policy.emotion`
- source sections: `Emotional Quotient`
- use for:
  - emotion metadata
  - derived sharpness signal inputs
  - why emotions are structured rather than freeform

Key points:
- emotions are structured metadata, not freeform prose

Detail notes:
- the current emotional model uses discrete labels with valence and arousal instead of one blended sentiment score
- sharpness uses emotional arousal only where it is meaningful; non-emotional memory types fall back to uniqueness
- cached sharpness is for pruning/lifecycle decisions, not hot-path retrieval ranking
- repeated summary-only revisions can lose detail, which is why compression/rehydration needs a fallback story

### `memory.policy.edges`
- source sections: `Edge Design`
- use for:
  - edge metadata
  - edge weight dynamics
  - edge lifecycle expectations

Key points:
- relationships carry their own behavioral metadata
- edge strength should evolve without requiring a node rewrite
- edge lifecycle should support consolidation and contradiction handling

Detail notes:
- edge weight is a capped ratchet: incremented +0.1 per traversal to a maximum of 5.0, with NO decay (verified 2026-07-03)
- v1 deliberately ties edge lifecycle to endpoint lifecycle rather than independent edge aging; decay protection is a designed trade, not a missing feature
- the decay-protection loop: recall touches `last_accessed` on returned nodes, which indefinitely shields them from lifecycle decay; retrieval thereby curates the archive (what survives) by choosing what gets returned — this is the mechanism by which rich-get-richer operates, by design but now documented
- when nodes are merged or deleted, edge repair should be deterministic and auditable rather than inferred ad hoc
- bridged edges are a structural safety measure to avoid graph shattering when intermediate nodes disappear

### `memory.policy.scope`
- source sections: `Memory Scope`
- use for:
  - `SESSION`, `PERSISTENT`, `PROMOTED`
  - short-term vs durable behavior
  - access and ownership expectations

Key points:
- `SESSION` is conversation-local and ephemeral
- `PERSISTENT` is durable memory
- `PROMOTED` is protected durable memory with higher retention weight

Detail notes:
- session writes may be journal-first or graph-first, but the semantic distinction is the same: session scope is the working layer before durable retention
- queue state, retries, and audit trails are increasingly sidecar concerns even though semantic truth stays in the graph
- multi-bot operation needs lease-safe queue recovery; strict global serialization is a separate problem
- one semantic store is preferable to separate short-term and long-term stores, because recall and traversal rules stay consistent
- `user_flagged` controls lifecycle retention; `bootstrap_scope` controls startup injection. They are deliberately independent.
- startup selectors are `general`, exact `workspace:<normalized-key>`, or null (retention-only). A workspace bootstrap reads general + its exact workspace pins; a general bootstrap never reads workspace pins.
- structural graph nodes are excluded from recent/startup memory lanes even though they share the `Entity` label.

### `memory.policy.lifecycle`
- source sections: `Memory Lifecycle (Freshness States)`
- use for:
  - freshness states
  - compression / rehydration
  - contradiction and conflict handling

Key points:
- lifecycle is driven by retention policy, not by retrieval ranking alone
- compression and rehydration are explicit state transitions
- pruning should respect structural importance and operator signals

Detail notes:
- v1 keeps a smaller freshness model than the long-term design because extra states are only useful once real usage proves they help
- compression should preserve fallback access to richer content long enough to recover from bad summaries
- contradiction handling should keep both versions until resolution instead of silently overwriting the older one
- sharpness thresholds, prominence brakes, and rehydration rules together define when a memory is safe, compressible, or recoverable

### `memory.policy.recall_usefulness`
- source: `rate_recall` tool + `domain/self_reinforcement.py` (R8 rails)
- use for:
  - what "useful" means when an agent rates a recall
  - why self-rated usefulness is an operational signal only

Key points:
- usefulness is judged by OUTCOME, not by how plausible the result looked
- self-rated usefulness is `agent_inference` grade — the weakest evidence kind
- it feeds the usage dashboard only; it never raises memory heat, promotion, or rank

Detail notes:
- the canonical definition of a valuable retrieval is `PRODUCTIVE_OUTCOMES` in
  `domain/self_reinforcement.py` (`user_confirmed`, `test_passed`, `code_compiled`,
  `external_supported`, `answer_accepted`, `contradiction_resolved`, `manual_approved`) —
  all externally grounded, never "it felt helpful"
- the `rate_recall` rubric mirrors that stance behaviorally: `useful` = you used specific
  content from the result; `partial` = it confirmed/narrowed but you needed other sources;
  `noise` = irrelevant/stale/wrong; `unused` = you did not consult it
- Guard 1 (ProductiveTouchGate) still gates durable heat on a real productive outcome, NOT on
  a self-rating — this keeps the RetrievalGravityWell failure mode closed. A self-rating may
  later be cross-checked against a productive outcome, but only as a separate, reviewed change
- the rubric's job is consistency (comparable ratings over time), not ground truth; watch the
  dashboard's Rated % coverage to see whether ratings are being applied at all

## Read Next

- Need runtime behavior -> [architecture.md](architecture.md)
- Need exact field names -> [data_models.md](data_models.md)
- Need term definitions -> [glossary.md](glossary.md)
- Need full design discussion -> [memory-design.md](memory-design.md)
