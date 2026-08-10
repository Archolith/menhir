# menhir — Memory Namespace (Silo) Isolation Plan

Date: 2026-06-23
Project: `C:\Users\you\IdeaProjects\projects\archolith\menhir`
Status: IMPLEMENTED (2026-06-24) — Phases 0-6 done. Write/read plumbing, backend+API funnel,
DELETE endpoint, Phase 4 conflict/correlation/sharpness namespace-scoping, MCP-tool namespace
args, and the default-namespace data migration (applied: 1006 group_id + 26141 namespace
nodes normalized) are all committed (029c9f7, 668da23, b1ebf0c, 5f6e866, 7290678, afd7ab7,
698ead3, 072b5ed, 8a21eac; bench client 04223a5). Candidate listing intentionally left
global (admin view, not a merge vector). DEFERRED: bench Mode-B LIVE run + tracked evidence
(needs deployed menhir + throwaway Neo4j); deploy of the menhir build; the two TestNanInfScoring
tests (min_similarity floor vs NaN coercion — owner decision).
Owner: ctharvey
Origin: surfaced while verifying `archolith-bench-longmemeval-menhir-mode-b-plan.md`. The
LongMemEval Mode-B benchmark needs per-item memory isolation, which menhir does not have.
Rather than a benchmark-only hack, this plan adds a first-class, production-grade isolation
primitive ("namespace" / silo) to menhir. Mode-B then rides on it for free.

## Purpose

Give menhir a real tenancy/isolation boundary so that memory written under one namespace is
never ingested into, resolved against, or recalled from another. This is valuable for
production (multi-project / multi-tenant / throwaway-eval silos) and is the correct fix for
the broken benchmark assumption (see "What's actually broken today").

## What's actually broken today

A prior subagent claimed menhir's backend "supports `group_id`". It does not — every
`group_id` in `core/backend_impl.py` (lines 507, 743, 1197, 1362) is **conflict-resolution
group id** (`resolve_conflict_group`, `record_conflict_resolution`), unrelated to memory
isolation. Current reality:

- Memory is isolated only loosely by `user_id` / `session_id` properties stamped onto nodes
  *after* creation (`infrastructure/episode_stamping.py::stamp_ingest_metadata`).
- Recall does a **global** vector search and never filters by tenant:
  `services/recall_service.py::recall` calls `graphiti_client.search_scored(query, num_results=...)`
  with no partition; the candidate filter loop (recall_service.py ~lines 449-478) only drops
  `CANDIDATE` scope, `GONE` freshness, and `SESSION` scope when `include_session` is false.
- The benchmark client (`archolith-bench/archolith_bench/harness/menhir_client.py::HttpMenhirClient`)
  POSTs a `group_id` to `/ingest` and `/recall` — endpoints that **do not exist** (menhir
  exposes `/api/memory`, `/api/recall`, `/api/context`) and a field the API silently drops
  (`api/routes.py::RecallRequest` / `MemoryRequest` have no such field). So real Mode-B HTTP
  runs would write/read one shared graph; the existing run shows 0.0 vs 0.0 (no isolation).

## Key enabling discovery

The underlying engine (`graphiti_core`) already has a native partition: **`group_id`**.
Menhir's read path is **half-wired** for it:

- `infrastructure/graphiti_client.py::search_scored` already accepts
  `group_ids: list[str] | None` and forwards it to `self.client.search_(query, config, group_ids=group_ids)`
  (graphiti_client.py lines 875-931). The read partition exists; recall just never passes it.
- The write path drops it: `graphiti_client.py::add_episode` (lines 712-867) calls
  `self.client.add_episode(name=..., episode_body=..., source_description=..., reference_time=...)`
  with **no `group_id`** (graphiti_core's `add_episode` accepts `group_id: str = ""`). So all
  writes land in graphiti's default group.

Therefore Option B is not a from-scratch data-model change. The primitive exists in graphiti;
we must thread a namespace end-to-end and add menhir-side defense-in-depth.

## Design decision

Adopt a two-layer isolation model:

1. **Primary partition = graphiti-native `group_id`.** A menhir *namespace* maps 1:1 to a
   graphiti `group_id`. Graphiti tags every episodic/entity node it creates with the group and
   performs entity resolution / dedup *within* a group — so isolation holds by construction at
   the engine layer, including the expensive vector/BM25 search (no cross-silo candidates are
   ever scored).
2. **Defense-in-depth = menhir `namespace` node property.** Stamp `namespace` onto nodes
   (mirroring how `user_id`/`session_id` are already stamped) and enforce it in menhir's own
   Cypher (`fetch_candidate_metadata`, adjacency, conflict scan, candidate review). This means
   a single mis-set graphiti group cannot leak memories across silos; the menhir filter is an
   independent gate.

Terminology: user-facing term is **namespace** (a "silo"). Internally it is the graphiti
`group_id`. One reserved value `DEFAULT_NAMESPACE = "default"` (mapped to graphiti group_id `""`
for backward compatibility with all existing data).

Rejected alternatives:
- *Map onto `user_id`/`session_id` only (Option A):* recall is global and post-stamp is
  best-effort `coalesce`; it cannot guarantee non-leak and does not constrain the search layer.
- *Pure menhir property without graphiti group_id (B2-only):* search still scores cross-silo
  candidates then filters — wasteful and one filter bug from a leak. We want the engine
  partition as the load-bearing boundary.

## Phases

### Phase 0 — Spike & verify graphiti group semantics (throwaway Neo4j)

Goal: confirm the engine behaves as assumed before threading anything.

Actions:
1. Stand up a throwaway Neo4j (do not touch the production graph). Reuse the env knobs in
   `config/settings.py` (`NEO4J_URI`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`).
2. Script a direct `graphiti_core` check: `add_episode(..., group_id="A")` and `group_id="B"`,
   then `search_(query, config, group_ids=["A"])` — verify B's nodes never appear, and that
   entity resolution does not merge A/B entities.
3. Confirm graphiti stamps `group_id` on both Episodic and Entity nodes (Cypher
   `MATCH (n) RETURN DISTINCT labels(n), n.group_id`).

Exit criteria:
- Documented proof that graphiti `group_id` partitions both write (incl. entity resolution)
  and search. If graphiti merges entities across groups, escalate — the whole approach pivots.

**RESULT (2026-06-23): GO.** Ran a throwaway spike against the real remote graph
(`bolt://neo4j.example.internal:7687`, graphiti provider=openai) using unique group ids
`_ns_spike_<id>_A/_B`, hard-deleted by prefix afterward (8 nodes removed; production untouched).
Installed `graphiti_core.add_episode` accepts `group_id: str | None` ("graph partition the
episode is a part of"); node dedup is scoped `group_ids=[node.group_id]`
(`graphiti_core/utils/maintenance/node_operations.py:220`). Empirical verdicts:
- **Entity resolution is group-scoped** — the shared entity "Acme Corporation" was created as
  TWO separate nodes, one per group; no cross-group merge.
- **No edges bridge group A <-> B** (0 cross-group relationships).
- **No B-distinctive entities leaked into group A** (0 Osaka/bankruptcy entities in A; a scoped
  search for B-terms in group A surfaced only A's own legitimate "Acme" node).
- **Unscoped search still returns all groups** (partition is opt-in, not accidental).
The engine partition is load-bearing; Phases 1-7 proceed as written.

### Phase 1 — Write path: persist + propagate namespace

The hard part is that ingestion is asynchronous: `queue_episode` records a pending episode,
and a **background enrichment worker** later calls `add_episode`. The namespace must survive
that hop, so it is persisted on the pending episode node and read back at enrichment time.

Actions:
1. `infrastructure/graphiti_client.py::add_episode` — add `group_id: str = ""` parameter; pass
   `group_id=group_id` into `self.client.add_episode(...)` (line ~819). (Output this change
   first — it is the load-bearing one.)
2. `core/backend_impl.py::queue_episode` (line 164) and the `RuntimeProvider` variant
   (line 1028) — add `namespace: str | None = None`; default to `DEFAULT_NAMESPACE`. Pass into
   `ingest_service.queue_episode_for_enrichment`.
3. `services/ingest_service.py::queue_episode_for_enrichment` (line 376) — accept `namespace`
   and persist it as a property on the pending Episodic node (alongside the existing
   `processing_*` fields). Define the constant in `domain/` (e.g. `domain/session.py` or a new
   `domain/namespace.py`).
4. `services/enrichment_steps.py` (the `add_episode_with_timeout` call site, line ~1058) — read
   the episode's persisted `namespace`, translate to graphiti group_id (`"" if namespace ==
   DEFAULT_NAMESPACE else namespace`), and pass `group_id=` into `graphiti_client.add_episode`.
5. `infrastructure/episode_stamping.py::stamp_ingest_metadata` (line 24) — add a `namespace`
   parameter; stamp `n.namespace = $namespace` on the Episodic SET (line ~45) and on the Entity
   SET using the same lock/`coalesce` pattern as `user_id` (lines ~88-91) so promoted/persistent
   nodes are not silently re-homed.

Exit criteria:
- Two episodes ingested under namespaces "A" and "B" produce nodes whose graphiti `group_id`
  **and** menhir `namespace` property both reflect the correct silo (verify in Neo4j).

### Phase 2 — Read path: scope recall + context to namespace

Actions:
1. `services/recall_service.py::recall` (line 363) — add `namespace: str | None = None`
   (and/or `namespaces: list[str] | None` for future fan-in). Translate to graphiti group_ids
   and pass into `search_scored(query, num_results=candidate_k, group_ids=...)` (line 393).
2. Defense-in-depth: in the candidate filter loop (recall_service.py ~lines 449-478) drop any
   candidate whose `namespace` is not in the allowed set. Requires `fetch_candidate_metadata`
   to return `namespace`.
3. `infrastructure/memory_queries.py::fetch_candidate_metadata` (line 157) — return
   `n.namespace`; optionally accept an allowed-namespace param to filter in Cypher.
   `fetch_adjacency_pairs` (line 168) — constrain traversal to same-namespace nodes so
   adjacency bonuses cannot bridge silos.
4. `services/context_builder.py::build_context` and `core/backend_impl.py::build_context`
   (lines 217 / 1087) — same `namespace` threading.
5. `infrastructure/schema.py` — add a Neo4j index on `:Episodic(namespace)` and
   `:Entity(namespace)` for filter performance.

Exit criteria:
- Recall under namespace "A" never returns "B" memories, proven two ways: (a) graphiti search
  returns no B candidates, and (b) the menhir filter independently rejects any B node injected
  into the candidate set in a unit test.

### Phase 3 — API + protocol + MCP tool surface

Actions:
1. `core/backend_protocol.py` — add `namespace` to the `queue_episode`, `recall`, and
   `build_context` protocol signatures (around lines 51, 80, 94).
2. `api/routes.py` — add `namespace: str | None = None` to `RecallRequest` (line 94),
   `MemoryRequest` (line 132), `ContextRequest` (line 116); thread through the `recall`,
   `ingest_memory`, and `context` handlers. Resolve a default from a new
   `x-yawn-namespace` header in `_resolve_caller_session` style, falling back to
   `DEFAULT_NAMESPACE`.
3. MCP tools: `mcp/tools/ingest/add_memory*.py`, `mcp/tools/recall/recall_memories.py`,
   `recall_context_memories.py`, `build_context.py` — expose an optional `namespace` argument;
   default preserves today's behavior.
4. `mcp/contracts.py` / formatters — surface namespace where memory provenance is shown.

Exit criteria:
- HTTP and MCP callers can read/write a chosen namespace; omitting it is byte-for-byte
  backward compatible (everything lands in `default`).

### Phase 4 — Namespace-scope the cross-cutting subsystems

These currently operate graph-wide and would otherwise leak across silos.

Actions:
1. Conflict detection: `backend_impl.scan_for_conflicts` and the conflict repositories
   (`infrastructure/candidate_repository.py`, conflict queries) — scope similarity scans to a
   single namespace. Conflicts must not be raised between memories in different silos.
2. Candidate review (`NodeScope.CANDIDATE` staged tier) — carry and enforce namespace through
   `list_candidates` / `promote_candidate` / `approve_candidate`.
3. Maintenance/consolidation (`services/maintenance_scheduler.py`,
   `infrastructure/consolidation_queries.py`) — ensure compression/consolidation never merges
   across namespaces.
4. Document explicitly out-of-scope for v1 (and why): structure graph (project scan), todos,
   temporal entries. Decide whether these are global or namespaced in a follow-up; default v1
   = they remain global, callers are warned.

Exit criteria:
- A conflict scan / consolidation pass over a graph holding ≥2 namespaces produces zero
  cross-namespace edges, merges, or conflict pairs (verified by query).

### Phase 5 — Backward compatibility, migration, lifecycle

Actions:
1. `DEFAULT_NAMESPACE = "default"` maps to graphiti group_id `""`. All existing nodes (no
   `namespace` property, graphiti group `""`) are treated as the default silo with no migration
   required for correctness.
2. Optional one-shot migration: stamp `namespace = "default"` on existing `:Episodic`/`:Entity`
   nodes so queries can rely on the property being present (script under menhir's existing
   maintenance/CLI patterns; gate behind an explicit flag).
3. Add a **namespace reset/delete** operation: `DELETE /api/namespace/{namespace}` (and a
   backend method) that removes all nodes/edges in a graphiti group. Required for throwaway
   eval silos and the benchmark; must refuse `default` and any production-reserved namespace.

Exit criteria:
- Pre-existing memory recalls exactly as before when no namespace is supplied.
- A throwaway namespace can be fully created and torn down without touching `default`.

### Phase 6 — Benchmark Mode-B integration (consumes the primitive)

Actions:
1. `archolith-bench/archolith_bench/harness/menhir_client.py::HttpMenhirClient` — point paths
   at the real API (`/api/memory`, `/api/recall`, `/api/namespace/{ns}` for reset) and send
   `namespace` (the per-item `new_group()` id) instead of the imagined `group_id`/`/ingest`.
2. Make each LongMemEval item ingest+recall under its own throwaway namespace; reset between
   items via the Phase 5 delete endpoint.
3. Run Mode-B against a throwaway menhir + Neo4j; record tracked evidence under
   `archolith-bench/benchmarks/longmemeval-menhir-YYYY-MM-DD.md` (methodology, commit, model,
   endpoint, item count, isolation proof). Update the industry coverage matrix entry from
   `candidate-before-launch` to `tracked-evidence-ready` only if the run is clean.

Exit criteria:
- Mode-B shows non-trivial, non-identical arms (no-memory vs menhir-recall) with per-item
  isolation demonstrably enforced.

### Phase 7 — Tests & docs

Actions:
1. Unit tests (do not modify existing tests except for import/signature changes from the
   structural threading):
   - write/read isolation at the `graphiti_client` layer (mock/stub graphiti),
   - recall defense-in-depth filter rejects foreign-namespace candidates,
   - stamping writes `namespace` on Episodic + Entity with the lock/coalesce rules,
   - API request models accept and thread `namespace`,
   - conflict scan is namespace-scoped,
   - default-namespace back-compat (no namespace == legacy behavior).
2. Docs: update `menhir/.agent/` architecture + data-model notes and the README API section;
   add a CHANGELOG entry.

Exit criteria:
- Full suite green; documented namespace model; CHANGELOG updated.

## Files in scope (anchors)

Write/stamp:
- `src/menhir/infrastructure/graphiti_client.py` (`add_episode` ~712, `search_scored` ~875)
- `src/menhir/services/ingest_service.py` (`queue_episode_for_enrichment` ~376)
- `src/menhir/services/enrichment_steps.py` (`add_episode_with_timeout` call ~1058)
- `src/menhir/infrastructure/episode_stamping.py` (`stamp_ingest_metadata` ~24)

Read:
- `src/menhir/services/recall_service.py` (`recall` ~363, candidate filter ~449)
- `src/menhir/services/context_builder.py` (`build_context`)
- `src/menhir/infrastructure/memory_queries.py` (`fetch_candidate_metadata` ~157, `fetch_adjacency_pairs` ~168)
- `src/menhir/infrastructure/schema.py` (indices)

Surface:
- `src/menhir/core/backend_protocol.py`, `src/menhir/core/backend_impl.py`
  (`queue_episode` 164/1028, `recall` 195/1063, `build_context` 217/1087)
- `src/menhir/api/routes.py` (`RecallRequest` 94, `ContextRequest` 116, `MemoryRequest` 132,
  handlers `recall` 208, `context` 233, `ingest_memory` 255; new `DELETE /api/namespace/{ns}`)
- `src/menhir/mcp/tools/{ingest,recall}/*.py`, `src/menhir/mcp/contracts.py`

Cross-cutting:
- conflict + candidate + consolidation paths (`candidate_repository.py`,
  `consolidation_queries.py`, `maintenance_scheduler.py`)

Domain:
- new `DEFAULT_NAMESPACE` constant (`domain/namespace.py` or `domain/session.py`)

Consumer (separate repo):
- `archolith-bench/archolith_bench/harness/menhir_client.py`

## Risks & open questions

- **Graphiti entity resolution across groups (Phase 0 gate).** If graphiti merges entities
  regardless of `group_id`, the engine partition is not load-bearing and Phase 1-2 must lean
  harder on the menhir property + a search post-filter. Verify before building.
- **Async namespace propagation.** The namespace must be persisted on the pending episode and
  faithfully read at enrichment time; a miss sends memory to `default`. Cover with a test that
  ingests under "A" and asserts the enriched entity's group_id/namespace == "A".
- **Promoted/persistent nodes.** The stamping lock pattern must not re-home already-promoted
  nodes into a new namespace; mirror the existing `locked` logic exactly.
- **Default mapping.** Treating `default` as graphiti group `""` keeps back-compat but means
  the default silo is "everything pre-existing"; document that callers wanting true isolation
  must pass an explicit namespace.
- **Structure graph / todos / temporal stay global in v1** — explicitly out of scope; flag so
  no one assumes full tenancy.
- **Index cost.** New namespace indices are cheap but must be added via the existing
  `build_indices_and_constraints` path, not ad hoc.

## Verification matrix

| Check | Expected |
|-------|----------|
| Phase 0 graphiti spike (write+search isolation, no cross-group entity merge) | Pass / documented |
| Ingest A + B → node `group_id` and `namespace` correct | Pass |
| Recall A returns zero B nodes (graphiti search) | Pass |
| Recall defense-in-depth filter rejects injected B candidate (unit) | Pass |
| Conflict scan over 2 namespaces → no cross-namespace pairs | Pass |
| No-namespace recall == legacy behavior (back-compat) | Pass |
| `DELETE /api/namespace/{ns}` removes silo, refuses `default` | Pass |
| Full pytest suite | Pass |
| Bench Mode-B real run with per-item isolation | Non-identical arms, isolation proven |

## Non-goals

- Do not namespace the structure graph, todos, or temporal entries in v1.
- Do not change the default behavior for callers that omit `namespace`.
- Do not retrofit graphiti `group_id` onto historical nodes for correctness (default mapping
  handles it); optional cosmetic migration only.
- Do not build per-namespace auth/quotas here — that is a separate tenancy-policy concern.
