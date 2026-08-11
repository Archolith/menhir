# menhir — hyperedge-ready storage without committing to a hypergraph backend

## Status

nice-to-have / backlog — NOT needed immediately, NOT scheduled. A future-proofing refactor
to make Menhir's logical memory model hyperedge-capable and its storage backend swappable,
while continuing to run on Graphiti + Neo4j today. NOT a hypergraph migration. The point is
to *avoid closing the door* if a Graphiti-for-hypergraphs equivalent (or TypeDB/Atomspace-
style backend) becomes worth testing — do it then, as an adapter problem, not a rewrite.

## Framing

> Build Graphiti-like behavior now, but make the logical memory model hyperedge-capable
> so Menhir isn't trapped in a pairwise property-graph worldview.

Headline finding from the audit: **Menhir is already ~70% of the way there.** It has a
service contract (`MemoryBackend`), a storage seam (`memory_graph_adapter` +
`memory_queries`), and a *proto-hyperedge* — the L4 artifact model (`domain/artifacts.py`)
with a first-class `Evidence` node and `SUPPORTED_BY` edges. This plan **consolidates and
generalizes the existing seam**; it does not rebuild.

---

## 1. Current-state audit

Layering that exists today (`src/menhir/`, 34 infra / 30 domain / 22 services files):

```text
MCP tools / agent        mcp/*  -> call MemoryBackend, never services directly
Service contract         core/backend_protocol.py  (MemoryBackend Protocol)          [EXISTS, good]
  impls                  RuntimeProvider (in-process) + BackendClient (HTTP)
Memory services          services/{recall,ingest,scoring,artifact,lifecycle,...}     [EXISTS]
Logical model            domain/{recall,temporal,scope,artifacts,models,belief,...}  [EXISTS, graph-flavored]
Storage seam             infrastructure/memory_graph_adapter.py + memory_queries.py  [EXISTS, Neo4j-only]
Physical                 Graphiti + Neo4j driver
```

Coupling points (verified):

```text
- services/recall_service.py
  - issue: writes raw Cypher / touches the Neo4j driver directly (the ONLY service that does).
  - why it blocks swapping: recall — the most important query path — is welded to Cypher.
  - suggested abstraction: move its Cypher into memory_queries.py behind adapter methods /
    a compiled QueryObject; recall_service should ask the adapter, not the driver.

- infrastructure/memory_graph_adapter.py  (the de-facto StorageAdapter, but concrete-only)
  - issue: method surface is graph/Neo4j-shaped and has ONE implementation; no Protocol,
    no capability flags, no fake.
  - why it blocks swapping: nothing to code a second backend against; tests need live Neo4j.
  - suggested abstraction: extract a `StorageAdapter` Protocol from its surface + capability
    flags; add an in-memory fake impl.

- core/backend_protocol.py  (MemoryBackend)
  - issue: it's the SERVICE contract (queue_episode/recall/build_context/...), returns dicts,
    and abstracts in-process-vs-HTTP — NOT storage-vs-storage. Correct layer, wrong altitude
    for backend-swapping.
  - why it (partly) blocks: the logical model is flattened to dicts at this boundary.
  - suggested abstraction: keep MemoryBackend as-is (app contract); introduce the
    StorageAdapter one layer DOWN. Domain objects stay typed until the storage seam.

- domain/artifacts.py + infrastructure/artifact_repository.py  (proto-hyperedge)
  - issue: first-class Evidence + SUPPORTED_BY + status/supersession + provenance exist, but
    only for Decision/Failure/Incident — not a general multi-participant FactEvent, and edges
    are still emitted per-pair elsewhere (Graphiti Entity-[edge]->Entity).
  - why it blocks: higher-order facts ("Alice approved PR#17 because test X passed on commit Y")
    get flattened to pairwise edges everywhere except L4.
  - suggested abstraction: generalize L4's artifact+Evidence pattern into a first-class
    `Hyperedge`/`FactEvent` with role-labeled participants.

- domain/temporal.py (FactTemporal, bitemporal), scope.py (MemoryScope), models.py (NodeScope,
  memory types), recall.py (ScoredMemory)
  - issue: temporal/provenance/scope are first-class and consistent HERE, but recall paths
    read raw node dicts; temporal fields are re-derived in several spots.
  - suggested abstraction: make TemporalScope + Provenance shared value objects reused by
    Entity, Claim, and Hyperedge (they mostly are — formalize + reuse, don't reinvent).
```

Good news: **only recall_service leaks the driver**; ingest/scoring/lifecycle already go
through the adapter. The seam is real; it's just concrete and graph-shaped.

---

## 2. Target architecture

Add ONE layer (StorageAdapter) below the existing service contract; keep everything else:

```text
Agent / MCP tools
      | (unchanged)
MemoryBackend  (core/backend_protocol.py)              service contract — KEEP
      |
Memory services (recall/ingest/scoring/artifact/...)   memory semantics — KEEP
      |
Logical model (domain/*): Entity · Episode · Claim ·   TYPED objects flow to here
  Hyperedge/FactEvent · TemporalScope · Provenance
      |
Query Planner (compile QueryObjects)                   NEW thin layer (mostly formalizing recall_service)
      |
StorageAdapter Protocol (+ capability flags)           NEW Protocol extracted from memory_graph_adapter
      |
Backend adapters: Neo4jGraphitiAdapter (today) ·        Neo4j = wrap existing adapter
  InMemoryAdapter (tests) · [Postgres/Kuzu/TypeDB later]
      |
Physical store
```

---

## 3. New interfaces / types

### 3a. Logical model — generalize the L4 pattern (domain/)

```python
@dataclass(frozen=True)
class TemporalScope:            # reuse domain/temporal.py fields; shared by all below
    learned_at: str | None; observed_at: str | None
    valid_from: str | None; valid_until: str | None
    invalidated_at: str | None; superseded_by: str | None
    sequence_index: int | None = None; transaction_id: str | None = None

@dataclass(frozen=True)
class Provenance:               # generalize domain/artifacts.Evidence + source_confidence
    source_episode_ids: tuple[str, ...] = ()
    source_file: str | None = None; source_commit: str | None = None
    source_tool_call: str | None = None; extraction_model: str | None = None
    confidence: float = 0.5; created_by: str | None = None

@dataclass(frozen=True)
class Participant:              # role -> ref (Entity/Claim/Episode/Hyperedge uuid)
    role: str; ref: str; ref_kind: str = "entity"

@dataclass(frozen=True)
class Hyperedge:               # THE future-proofing type; generalizes L4 artifact+Evidence
    id: str
    kind: str                  # fact_event | decision | failure | incident | relation
    predicate: str
    participants: tuple[Participant, ...]     # actor/object/evidence/reason/commit/test/...
    qualifiers: dict[str, str]
    temporal: TemporalScope
    provenance: Provenance
    status: str                # candidate | trusted | historical  (reuse ArtifactStatus)
    embedding_refs: tuple[str, ...] = ()
```

`Entity`, `Episode`, `Claim` (binary special-case of Hyperedge) as dataclasses with the
same `TemporalScope` + `Provenance`. Existing `ScoredMemory`, `FactTemporal`, `MemoryScope`,
`Evidence`, `ArtifactStatus` are the seeds — wrap, don't replace.

### 3b. StorageAdapter Protocol (core/storage_adapter.py — NEW)

```python
@runtime_checkable
class StorageAdapter(Protocol):
    capabilities: "AdapterCapabilities"
    async def upsert_entity(e: Entity) -> None; async def get_entity(id) -> Entity | None
    async def upsert_episode(...); async def upsert_claim(...); async def upsert_hyperedge(h: Hyperedge) -> None
    async def get_hyperedge(id) -> Hyperedge | None
    async def find_entities(q: EntityQuery) -> list[Entity]
    async def find_claims(q: ClaimQuery) -> list[Claim]
    async def find_hyperedges(q: HyperedgeQuery) -> list[Hyperedge]
    async def supersede_fact(old_id, new_id) -> None; async def invalidate_fact(id, reason) -> None
    async def run_memory_query(q: MemoryQuery) -> MemoryQueryResult
```

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    supports_native_hyperedges: bool = False   # Neo4j today: False (emulated via FactEvent node)
    supports_temporal_indexes: bool = True
    supports_vector_search: bool = True; supports_keyword_search: bool = True
    supports_graph_traversal: bool = True; supports_reasoning: bool = False
    supports_transactions: bool = True; supports_provenance_queries: bool = True
```

### 3c. Query objects (domain/queries.py — NEW; the 4 families that matter)

`TemporalMemoryQuery`, `CodeStructureQuery`, `BlastRadiusQuery`, `HyperedgeQuery` — exactly
the field lists in the handoff. These already exist implicitly as recall_service /
query_structure params; formalize them as dataclasses the adapter compiles. **Do not**
build a generic query AST — only these four.

---

## 4. Physical storage now (Neo4j, emulated hyperedges)

Reuse the L4 pattern already shipped in `artifact_repository.py` for ALL higher-order facts:

```text
(:FactEvent {id, kind, predicate, status, confidence, valid_from, valid_until, learned_at})
(:FactEvent)-[:PARTICIPANT {role:"actor"}]->(:Entity)
(:FactEvent)-[:PARTICIPANT {role:"evidence"}]->(:Evidence)     # Evidence already first-class
(:FactEvent)-[:PARTICIPANT {role:"code_state"}]->(:Entity {type:"commit"})
(:FactEvent)-[:DERIVED_FROM]->(:Episodic)
```

Binary facts stay as today's Graphiti `Entity-[edge]->Entity` (a Claim); only promote to
FactEvent when arity ≥ 3 or a role beyond subject/object is present. `capabilities
.supports_native_hyperedges=False` → the adapter transparently emulates; a future TypeDB/
hypergraph adapter flips it True and stores natively. Postgres equivalent tables:
`entities, episodes, claims, fact_events, fact_event_participants, temporal_scopes,
provenance_refs, embeddings`.

---

## 5. Refactor plan by phase

```text
Phase 0  Extract StorageAdapter Protocol from memory_graph_adapter's real surface +
         AdapterCapabilities. memory_graph_adapter becomes Neo4jGraphitiAdapter (implements
         it). Zero behavior change. (wrap, don't rewrite)
Phase 1  InMemoryAdapter implementing StorageAdapter — enough for recall/ingest round-trip +
         hyperedge emulation. Unlocks backend-neutral tests without Neo4j.
Phase 2  Close the leak: move recall_service's raw Cypher into memory_queries behind adapter
         methods / a compiled MemoryQuery. recall_service depends on StorageAdapter, not driver.
Phase 3  Formalize the 4 QueryObjects; adapters compile them (Neo4j->Cypher, InMemory->scans).
         Migrate recall + query_structure + blast_radius to run through run_memory_query.
Phase 4  Generalize L4 artifact+Evidence into the Hyperedge/FactEvent type + adapter
         upsert_hyperedge/find_hyperedges(by role); artifact_service becomes a thin caller.
         Ingest emits FactEvents for arity>=3 facts (opt-in, behind a flag).
Phase 5  (optional, when a backend is worth testing) PostgresAdapter or TypeDBAdapter behind
         the same Protocol; capability flags drive graceful degradation.
```

## 6. Concrete file/module changes

```text
NEW  core/storage_adapter.py         StorageAdapter Protocol + AdapterCapabilities
NEW  domain/logical_model.py         Entity/Episode/Claim/Hyperedge/Participant/TemporalScope/Provenance
NEW  domain/queries.py               the 4 QueryObject dataclasses + MemoryQuery/MemoryQueryResult
NEW  infrastructure/in_memory_adapter.py   InMemoryAdapter (tests + reference semantics)
EDIT infrastructure/memory_graph_adapter.py  -> implements StorageAdapter (rename class Neo4jGraphitiAdapter)
EDIT infrastructure/memory_queries.py  absorb recall_service Cypher; add compile(MemoryQuery)->Cypher
EDIT services/recall_service.py       remove driver usage; depend on StorageAdapter + QueryObjects
EDIT domain/artifacts.py + infrastructure/artifact_repository.py  express as Hyperedge(kind=decision/...)
EDIT core/backend_impl.py (RuntimeProvider)  inject the adapter; unchanged MemoryBackend surface
```

## 7. Testing plan

```text
- Round-trip: entity/episode/claim/hyperedge upsert+get with no loss (InMemory + Neo4j).
- Backend-neutral: SAME QueryObject returns equivalent results on InMemoryAdapter and
  Neo4jGraphitiAdapter (parametrized fixture).
- Hyperedge emulation: store "Agent modified File A because Test B failed on Commit C" (>=3
  participants); retrieve by ANY role — files-changed-because(TestB), commits-for(FileA),
  evidence-for(change), action-linking(A,B,C).
- Temporal: learned vs valid time, supersession, invalidation, out-of-order ingest,
  "what did we believe at T?" (reuse domain/temporal tests against the adapter).
- Blast-radius: structure + memory + git-time join through run_memory_query on both adapters.
- Regression: existing recall/ingest suites pass unchanged through Phase 0-2.
```

## 8. Risks & tradeoffs

```text
- Over-abstraction: mitigated by scoping to 4 QueryObjects + wrapping (not rewriting) the
  adapter; no generic query AST.
- Perf: run_memory_query indirection must compile to the SAME Cypher recall_service runs
  today (assert via a golden-Cypher test) — no added round-trips.
- Capability leakage: don't force everything to the lowest common denominator; expose flags
  and let services opt into native features when present.
- Graphiti owns extraction+dedup+temporal: the adapter wraps Graphiti for ingest, exposes
  a neutral read surface for recall. Don't try to abstract Graphiti's writer away yet.
- FactEvent dual-write: emitting FactEvents alongside Graphiti binary edges risks
  duplication — gate behind a flag, reconcile in recall (prefer FactEvent when present).
```

## 9. Smallest useful first PR (the MVR — ~80% of the swap benefit)

```text
Phase 0 + Phase 1 + the round-trip/backend-neutral tests:
  1. core/storage_adapter.py: StorageAdapter Protocol + AdapterCapabilities.
  2. memory_graph_adapter.py implements it (rename Neo4jGraphitiAdapter) — no behavior change.
  3. infrastructure/in_memory_adapter.py: fake covering entity/episode/claim/hyperedge
     upsert+get + find_hyperedges-by-role (emulated).
  4. tests: round-trip + one backend-neutral hyperedge test (3-participant fact by-any-role)
     green on BOTH adapters.
Outcome: a named storage boundary + a fake + proof multi-participant facts survive without
triple-flattening — the door is open, nothing is rewritten, recall/ingest untouched.
```

## Open questions

```text
- Do FactEvents REPLACE Graphiti binary edges for arity>=3, or overlay them? (recommend overlay
  + prefer-FactEvent-in-recall, behind a flag, until benched.)
- Does the L4 Evidence node become the general provenance carrier, or stay L4-only? (recommend
  general — it already models kind/ref/directness/trust.)
- Bench gate: prove on archolith-bench / LongMemEval that FactEvents don't regress recall before
  emitting them in production ingest.
- Relationship to Chunk E (L3/L4 overlay) in ../../archive/plans/menhir-frontier-undone-work-chunks.md — this is the
  storage-shaped half of the same overlay; sequence together.
```

## Non-goals

```text
- No hypergraph DB migration. No generic query AST. No abstracting Graphiti's writer.
- Don't collapse multi-participant facts to triples early; don't treat embeddings OR traversal
  OR created_at as "the memory system". Keep temporal + provenance first-class.
```

## References

- `core/backend_protocol.py` (MemoryBackend), `core/backend_impl.py` (RuntimeProvider)
- `infrastructure/memory_graph_adapter.py`, `memory_queries.py`, `artifact_repository.py`
- `domain/artifacts.py` (L4 proto-hyperedge), `domain/temporal.py`, `scope.py`, `recall.py`, `models.py`
- `services/recall_service.py` (the one Cypher leak)
- Related: `../../archive/plans/menhir-frontier-undone-work-chunks.md` Chunk E (L3/L4 overlay), `docs/research/schemas/layer4-knowledge-artifacts.md`, `.agent/plans/l4-artifact-loop-v0.md`
