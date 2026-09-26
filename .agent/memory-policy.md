# Memory Policy

Compact policy companion for [memory-design.md](memory-design.md).

Use this file first when you need behavior and policy without loading the full design document.

## Scope

### Chronological memory timestamps (#145)

The four generic memory lists (recent, flagged bootstrap, scope, and type) must order by the actual access
instant, falling back to creation time when access time is missing or malformed. Valid legacy ISO strings
and native Neo4j timestamps remain readable before any data backfill. Unknown dates sort after known dates;
equal instants use UUID as the tie-break. Filtering and limits remain in the database.

New writes to recallable memory nodes use native Neo4j zoned datetimes. Graphiti and existing Cypher-native
writers already do this; direct TEMPORAL, candidate, L4, TODO-reminder mirror, and View refresh paths stamp
native dates too. Dedicated TODO/WorkArtifact metadata and structural-only records retain their separate contracts.

Use a shared guarded Cypher conversion, with explicit calendar/time validation before parsing legacy text;
do not introduce APOC or a new stored sort key. Missing timezone/local date values are interpreted as UTC.
The age predicates that consume these memory properties use the same conversion; an unknown age cannot
justify destructive decay. This provides compatibility while old application versions may still write text.
No startup schema rewrite or production backfill is part of the fix. Any optional conversion of existing
properties must be bounded, previewed, backed up, and applied manually after approval.

Compatibility accepts extended ISO calendar dates and date-times with seconds, up to nine fractional digits,
and `Z` or numeric offsets through +/-18:00. Native zone annotations retain their explicit offset. Date-only
and naive/local values mean UTC. Invalid dates, unsupported types, and unsupported timestamp spellings are
unknown; valid creation time supplies the fallback. Returned properties retain their original stored values.

Writer census: Graphiti persistence, episode lifecycle, schema missing-value fills, and recall touches already
use native dates. The five direct repositories above are aligned by this change. Schema startup deliberately
does not rewrite existing strings. Older deployed binaries, manual imports, and arbitrary property-map inputs
can still introduce text; this is a compatible reader policy, not a database type constraint.

Optional manual normalization: first deploy the compatible readers and align/drain old writers. Back up the
graph, preview a bounded explicit UUID manifest of non-structural Entity/Episodic properties, and compute each
proposed value with `memory_timestamp_cypher`; reject unknown values for individual review. Apply only reviewed
values in small transactions that recheck namespace, labels, and the original property before writing; skip
concurrent changes. Reread the converted manifest, compare instants and list ordering, then repeat the inventory
until no reviewed legacy values remain. Never replace an invalid date with the current time. No conversion is
executed by this PR. Such a backfill needs its own reviewed maintenance command and authorization.

Separate relevance scoring still uses `domain.utils.days_ago`, which defaults legacy ISO text to 30 days.
This PR fixes chronological lists and database age predicates, not that scoring contract. Converting stored
text to native dates can therefore change relevance/sharpness scores; preview that effect before any backfill.

### Generic reads of completed and superseded memories (#143)

Generic recall, recent/startup context, resources, and REST reads keep history searchable. They carry the
stored `status`, `artifact_status`, and `superseded_by` fields without changing original content or ranking.
An explicit `status=completed` is labeled as historical context, not an outstanding obligation. An
`artifact_status=historical` or nonblank `superseded_by` is labeled as superseded content, not current guidance.
Text context retains that label in the same budgeted item as its content, including clipped timelines and
pinned hook output. Context deduplication preserves records with distinct lifecycle states.

Absent, blank, and unknown state values do not invent completion or supersession. Open reminders and trusted
artifacts retain their ordinary presentation. Dedicated reminder lists continue to include only open records;
explicit UUID inspection remains available for historical records. A timeline's current-belief marker describes
fact belief time, not an outstanding task, and uses that explicit wording on historical memories.

These fields already exist on TEMPORAL and L4 institutional artifact nodes. This change requires no database
migration and does not activate the L4 production flow or change the separate WorkArtifact lifecycle. Readers
observe the state acquired during their read; a concurrent close/supersession is reflected on a subsequent read.

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
- the View-evidence rules below describe an unreleased local implementation; production activation
  is blocked on coordinated schema/backfill/writer rollout and the missing runtime publication,
  tombstone-key, and generic-repair services
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

Semantic recall applies the same SESSION admission to enriched entities and pending source
fallbacks. `include_session=False` excludes SESSION records. With inclusion enabled and a
caller session id, only that session's stamped records qualify; missing owners are excluded.
Runtime recall uses the request session, falling back to the process session. Direct sessionless
reads with inclusion enabled retain the existing broad opt-in, including ownerless records.
The stdio HTTP bridge forwards the same session used for writes in `x-menhir-session-id`,
which trusted-header auth modes honor. OAuth continues deriving session identity from verified
claims and ignores this header. A new stdio process starts a new conversation; promote a memory
before expecting it to remain available across that restart.
PERSISTENT/PROMOTED records remain eligible under their other guards. Missing/blank pending scope
is treated as SESSION. Pending selection filters before its limit and rechecks refreshed records
before exposing pending content or injecting READY-linked entities. Session identity is a
conversation-context selector; credential-bound namespaces remain the authorization boundary.

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
- decay age pre-filters use the minimum compression/deletion days among non-exempt type policies,
  derived at process startup. Exempt IDENTITY thresholds do not lower those minima; zero thresholds
  on a non-exempt policy do. Policy edits require a process restart.
- materialized Views are governed by their fold/projection lifecycle, not ordinary memory decay; generic compression and deletion must exclude them
- a current FACT View's `MENTIONS` links retain its `:Episodic`/`:TurnEvidence` contributors from automatic orphan cleanup; a durable contributor UUID without a live evidence node is an audit receipt, not recall authority
- explicit erasure still wins: it retires dependent current Views, scrubs erased contributor UUIDs from retained versions, and resets projection watermarks atomically before deleting evidence

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
