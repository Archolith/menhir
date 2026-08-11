# menhir — todos as first-class referents with author-declared links

> **Archived 2026-08-11.** Declared links, semantic resolution, lifecycle transactions, reminders,
> and removal of the obsolete `CONCERNS` relation are implemented.

Status: **APPROVED FOR IMPLEMENTATION** — Phase A is implementation-ready after two
review rounds (2026-08-02). Round 1 required three factual corrections; round 2 required
compatibility, indexing, and write-integrity specifications. Both are incorporated.
Date: 2026-08-02
Supersedes: [`menhir-todo-concerns-semantic-relevance.md`](menhir-todo-concerns-semantic-relevance.md),
[`menhir-code-graph-embedding.md`](menhir-code-graph-embedding.md)
Follows: `930a77e`, `5e015ec` (CONCERNS matching), `c6b5e43` (namespace invariant)

## Corrections to the first draft

The first draft overstated the diagnosis in three places. All three are corrected here
and change what may safely be removed.

1. **"Nothing reads CONCERNS" was false.** `get_todo` traverses
   `(n)-[:CONCERNS]->(e:Entity)` and returns `linked_entities`; `add_todo` reports them
   on create. The accurate claim is narrower: **CONCERNS has no retrieval or
   decision-making consumer — it is diagnostic metadata on create/get.** Removing it
   still changes observable MCP output. (The original grep excluded
   `todo_repository.py`, which is where the reader lives.)

2. **`REFERENCES_FILE` is not optional decoration — it powers blast radius.**
   `structure_queries.py:703-709` runs
   `MATCH (f)<-[:REFERENCES_FILE]-(t:Todo {status:'open'})` to surface open todos on
   impacted files. This makes the 83% miss rate worse than first described: blast radius
   is silently missing most relevant todos today. The edge cannot be demoted until blast
   radius reads normalized locations directly.

3. **The hook does not keyword-match.** `hook.py:138` and `:213` call
   `list_todos(status="open", limit=5)` — the global top five by priority then age.
   Only `context_builder` uses `list_todos_matching_query`. So there are three distinct
   surfaces, not two.

## Thesis (unchanged)

Author-declared data is reliable; inferred and optional links are not. Measured:

| Link | Kind | State |
|---|---|---|
| `content`, `code_ref`, `priority` | author-typed | reliable |
| `CONCERNS` | inferred | was 30% wrong; diagnostic-only consumer |
| `REFERENCES_FILE` | inferred | resolves 13 of 77 (83% miss) — **and blast radius depends on it** |
| `CREATED_FROM` | optional param | 0 edges; **exposed MCP/backend API** |
| `HAS_REMINDER` | optional param | 0 edges; **complete implemented feature path** |

`:Todo` has zero inbound edges, so todos cannot participate in provenance.

Direction: make the author's declaration authoritative, and make the todo referenceable.
But treat exposed API surface as a contract, not as dead code, until proven otherwise.

---

## Phase A — authoritative todo locations

### A1. The defect is normalization, not graph linking

`code_ref` mixes conventions — `projects/archolith/menhir/src/menhir/infrastructure/view_repository.py`
(workspace-relative) alongside `src/menhir/infrastructure/neo4j.py` (project-relative) —
while the resolver is `f.structure_path ENDS WITH $file_path`, requiring the *stored*
path to end with the *ref*. A workspace-relative ref is longer than the stored
project-relative path, so it can never match.

### A2. Canonical location model

Multiple properties rather than one normalized string, so Cypher never re-parses
location syntax:

| Property | Notes |
|---|---|
| `location_project` | owning repo, may be null |
| `location_path` | always repository-root-relative, `/` separators |
| `location_line_start` | optional |
| `location_line_end` | optional |
| `location_symbol` | optional |
| `code_ref_raw` | retained verbatim for fidelity and migration debugging |

Rules: normalize separators to `/`; collapse `.` and `..`, and **reject any normalized
path that escapes the repository root** rather than storing it; strip leading workspace
segments **only when `location_project` is known**; parse line and symbol but never
require them; leave ambiguous references unresolved rather than guessing.

Add `location_schema_version` so the migration is idempotent and a future change to the
parsing rules can tell already-migrated data from legacy values.

### A2a. `code_ref` stays — compatibility contract

`code_ref` is returned by `create_todo`, `list_todos`, `get_todo`, the hook output, the
context builder, and MCP responses. **It must not be silently replaced by
`code_ref_raw`.** Otherwise Phase A fixes blast radius while breaking every display
surface that reads `todo["code_ref"]`.

- Keep storing and returning `code_ref` throughout migration.
- `location_*` fields are authoritative **for queries**.
- `code_ref` remains the backward-compatible human-readable rendering.
- Add `code_ref_raw` only if raw input and display form genuinely need to differ.
- Redefine or remove `code_ref` only through an explicit, announced API migration.

### A2b. Indexes and query shape

**There are currently zero indexes on `:Todo`** — 142 exist in the database, none on
this label. Every todo read is a label scan. Harmless at 231 nodes; not acceptable once
location reads run inside blast radius.

Evaluate an index supporting `:Todo(namespace, status, location_project, location_path)`.
Do not assume the composite is optimal — Neo4j's planner may prefer separate indexes
depending on version and query shape, so **the implementation must include
`EXPLAIN`/`PROFILE` evidence**, not a guess.

Matching semantics, to be fixed before consumers depend on them:

- Primary invariant for blast radius: exact normalized `location_project` +
  `location_path`.
- Line range and symbol are **narrowing filters only**, never required identity fields —
  a todo naming a file but no line must still surface for that file.

### A3. `structure_project` precedence

`structure_project` already exists as an explicit argument and narrows file-edge
matching. It must win. Precedence:

1. Explicit `structure_project`
2. Unambiguous project prefix parsed from `code_ref`
3. Caller-supplied current project/workspace context
4. Otherwise null — no guessing

During migration, a conflict between explicit and parsed project values is recorded as
**unresolved**, never silently normalized.

### A4. Namespace applies to every new read — blocking

Todos always carry a non-null namespace. Scoped reads return
`requested + default`; an omitted namespace preserves cross-silo behavior.

**Every new read path — location queries, blast-radius traversal, and inbound memory
traversal — must accept an optional namespace and apply the identical rule.** Blast
radius and structural queries can be invoked outside the scoped todo APIs, so without
this a location-based consumer would leak todos across silos while `list_todos` and
`get_todo` remain correctly scoped.

### A5. Backend parity

A location query is not one Cypher method. Menhir treats backend parity as an
invariant; missing a layer means local execution works while HTTP-backed clients
silently lack the feature. Layers: `TodoRepository`, `MemoryGraphAdapter`, backend
protocol, local backend, `BackendClient`, the `_BACKEND_METHODS` allowlist, the MCP
surface if exposed, blast-radius implementation, tests, migration utilities.

### A6. Ordering

1. Add normalized location properties; change no existing read.
2. Backfill all `code_ref` values; emit a migration report counting normalized,
   ambiguous-project, malformed line/symbol, and no-path cases.
3. Add location queries with namespace enforcement.
4. Migrate blast radius to direct normalized-location matching.
5. **Compare old `REFERENCES_FILE` results against the new query** before trusting it.
6. Retain or regenerate `REFERENCES_FILE` as derived enrichment.
7. Only then consider removing resolver code.

---

## Phase B — todos as first-class semantic referents

**Phase A is IMPLEMENTED** in `2108f96`, `1b6c085`, `505f56e`. Resolvable todos went
13/77 -> 48/77; 88 locations, 56 with a file edge, 32 unresolved. That 32 is healthy:
"no matching structural entity" is a different state from "could not understand the
declaration", and menhir preserves the distinction rather than collapsing it.

### B-invariant. Todo stays operational

> **Todo remains an operational object. Semantic entities may refer to it, resolve it, or
> reopen it, but knowledge continues to live in memories and semantic objects — not
> inside the todo.**

The failure mode this prevents: todos slowly accumulate semantic behavior until menhir
has two competing knowledge representations. Links always point *inward* — a memory,
decision, finding, or observation references the todo. The todo never grows fields or
edges that make it a second kind of memory node.

The domain boundary underneath it: **structural objects explain where something lives;
semantic objects explain what something means or decides.** Those stay separate.

### B0. Scope constraint (post-Phase-A review)

Typed todo links are for **durable semantic entities and memories only**, never
arbitrary graph entities. Structural nodes are not eligible: a file does not "address"
a todo, and admitting them would recreate the CONCERNS problem in a typed costume.

Every link write validates namespace compatibility between memory and todo, and uses
idempotent `MERGE`.

**Do not widen `add_memory` with all four relation types up front.** The main ingestion
API stays untouched until the inbound model and namespace rules are proven in slice 1;
otherwise the ingestion surface grows before the graph semantics stabilize.

**Slice 1 IMPLEMENTED** in `25eeb2f`. **Slice 2 IMPLEMENTED** in the commit that follows
it. Atomicity is delivered by a single Cypher statement rather than a transaction scope:
`Neo4jRepository.execute` exposes none, and Neo4j wraps one statement in an implicit
transaction, so the edge and the status move together or neither does.

### B slice 1 — reference model and reads

- `MENTIONS_TODO` and `ADDRESSES_TODO` only.
- Inbound relations returned by `get_todo`.
- Namespace validation on write.
- Tests for many-to-many and idempotency.

### B slice 2 — lifecycle transactions

- `resolve_todo`, `reopen_todo`.
- Atomic edge creation plus status mutation in one transaction.
- Failure and rollback tests.
- `RESOLVES_TODO` / `REOPENS_TODO` arrive here, not in slice 1.

Gate between slices: slice 2 starts only once slice 1's namespace rules and inbound
reads are proven.

### Original B design (retained for detail)

### B1. Typed, many-to-many links

A singular `add_memory(todo_uuid=...)` is too narrow: one decision can resolve several
todos, one todo can accumulate several memories, and mentioning a todo differs from
resolving it. Proposed:

```python
todo_links: list[TodoLinkInput] | None

TodoLinkInput(todo_uuid: str,
              relation: Literal["mentions", "addresses", "resolves", "reopens"])
```

Distinct edge types rather than a relation property, since menhir commonly traverses by
semantic type:

```
(:Entity)-[:MENTIONS_TODO]->(:Todo)
(:Entity)-[:ADDRESSES_TODO]->(:Todo)
(:Entity)-[:RESOLVES_TODO]->(:Todo)
(:Entity)-[:REOPENS_TODO]->(:Todo)
```

Avoid a bare `ADDRESSES` — too generic in a graph that will accumulate other semantic
object types.

### B2. Lifecycle invariant

Stated precisely, to remove an apparent contradiction in the first revision:

> **Creating a semantic edge alone never changes lifecycle. Explicit lifecycle commands
> may atomically create the corresponding evidence edge and update status.**

So writing `RESOLVES_TODO` on its own records evidence and nothing more. A deliberate
`resolve_todo(todo_uuid, memory_uuid)` verifies both nodes and performs edge creation
plus the status change **in one Neo4j transaction**. Implicit coupling is what risks a
"resolved" memory pointing at an open todo, or a closed todo with no edge; an explicit
transactional command is not that.

### B4. Write-time integrity

Read-time namespace visibility (A4) is not sufficient on its own. Writes must enforce:

- Every target todo UUID exists.
- Memory and todo are namespace-compatible.
- Identical links are idempotent — `MERGE`, not `CREATE`.
- An unsupported `relation` value rejects the whole operation.
- Multi-link writes declare their failure mode explicitly; default to atomic across all
  links, so a partial link set is never persisted.

### B3. Namespace and read surface

Inbound traversal obeys A4. Decide how these relations appear in `get_todo` and recall
output before writing edges that later need re-interpreting.

---

## Phase C — legacy relationship cleanup

### C-invariant. Explicit over inferred

> **Prefer explicit, typed relationships over inferred ones unless inference enables a
> capability that cannot reasonably be expressed by explicit author intent.**

This is what the whole todo redesign converged on. Applied to the remaining relations:

- **CONCERNS is removed.** It existed only because there was no author-declared
  relationship. There now are several — `TodoLocation`, `MENTIONS_TODO`,
  `ADDRESSES_TODO`, `RESOLVES_TODO`, `REOPENS_TODO` — so the one remaining inferred edge
  is trying to infer intent from prose. "What todos concern yawn.market?" is better
  answered deterministically by declared links and locations than probabilistically by
  entity-name extraction. Inference enables no capability here that explicit intent
  cannot express.
- **CREATED_FROM stays.** Cheap, and it expresses *triggering provenance*, which is a
  different fact from semantic resolution. Nothing replaces it.
- **HAS_REMINDER stays**, and is now worth more than when it was flagged: lifecycle
  transitions carry semantics, so closing a todo naturally completes its reminder and
  reopening returns it to open. `resolve_todo`/`reopen_todo` already maintain it.

| Relationship | Disposition |
|---|---|
| `REFERENCES_FILE` | Keep as derived enrichment until blast radius is migrated; rebuild from normalized location where possible |
| `CONCERNS` | Stop writing **by default** only if no entity -> open-todos consumer is approved; preserve existing edges initially; removal changes `get_todo`/`add_todo` output |
| `CREATED_FROM` | **Keep provisionally.** `episode_uuid` is public MCP and backend API and the tool documents the edge. Zero edges means no caller supplies it, not that the concept is wrong. Add instrumentation or a test proving whether any supported caller *can* provide an episodic UUID; deprecate only if architecturally impossible. Consider renaming to `TRIGGERED_BY_EPISODE`. It is distinct from B1's relations: triggering provenance, not subsequent work |
| `HAS_REMINDER` | **Unvalidated, not dead.** Supplying `due_date` creates a TEMPORAL entity, links it, completes it on close, deletes it on delete, and the MCP docs promise date-window surfacing. Keep `due_date` as authoritative todo data; add one integration test proving reminders actually surface through temporal recall/hook; remove the TEMPORAL projection only if that path is broken or conceptually wrong |
| `completed` property | Safe cleanup after confirming no migration or legacy reader references it |

---

## Deviation from the review

The review recommended three separate plan documents. This keeps one document with hard
phase boundaries instead, because the phases share a single evidence base and B and C
depend on outcomes measured in A — splitting would duplicate the evidence table three
times and obscure that dependency. If the phases end up owned by different sessions,
split then.

## Generalizable precedent

`:TodoLocation` establishes a pattern worth reusing:

> An identity-bearing object owns subordinate location records; those locations resolve
> independently to repository structure without themselves becoming semantic objects.

The same shape applies to workspace artifacts — plans, reviews, reports, handoffs:

```
(:Artifact)-[:HAS_LOCATION]->(:ArtifactLocation)
(:ArtifactLocation)-[:RESOLVES_TO]->(:FileEntity)
```

The properties that make it work are the ones to carry over: the subordinate node keeps
its own label (never `:Entity`), holds no namespace copy, resolves or fails per record,
retains the raw declaration, and is deleted with its owner.

## Explicitly not proposed

- **Embeddings** for todo linking — the noise lived in unembedded code-structure nodes,
  and no retrieval consumer would have benefited.
- **LLM adjudication** per todo — breaks the documented ":Todo never queued for LLM
  processing" property and adds a provider dependency to todo creation.

## Open questions

1. Is an entity -> open-todos retrieval consumer wanted? Gates CONCERNS' fate, and it
   must beat the existing keyword `list_todos_matching_query` to justify itself.
2. Should low-value CONCERNS edges carry a confidence score rather than being filtered at
   write time? Non-destructive and lets each consumer choose a threshold.
3. Are reminders inside menhir's intended product boundary at all?
4. Does anything besides blast radius want "todos at this location" — the hook, the
   context builder?
