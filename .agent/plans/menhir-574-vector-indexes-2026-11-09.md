---
artifact_schema: 1
artifact_uuid: 1901b2ab-c1cc-473f-be7d-1154fd8778ba
artifact_type: plan
artifact_status: PROPOSED
---

# Default vector indexes for similarity search (#574)

## Why

- Every similarity search is a full scan: Graphiti's Neo4j `node_similarity_search` and
  `edge_similarity_search` compute `vector.similarity.cosine()` per row, and Neo4j only uses a vector
  index through `db.index.vector.queryNodes` / `queryRelationships`. A default install creates no
  VECTOR index.
- Production (2026-11-09): 17,026 entity name embeddings, 17,395 fact embeddings. Each scan takes
  1-5 s; every sampled Neo4j transaction during enrichment was this scan; enrichment averages
  ~165 s per episode and recall p50 rose from 4-5 s to 6-7 s as the graph grew.

## Scope

In scope:

1. Create the vector indexes on every start, idempotently, inside `prepare_memory_runtime` (so the
   `candidate-readonly` schema fence still applies).
2. Route Graphiti's node and edge similarity search through `driver.search_interface`, using the
   index when it is ONLINE and falling back to the existing scan otherwise.
3. Move Menhir's own `:Episodic(content_embedding)` and `:TypedAssertion(name_embedding)` cosine
   queries (`memory_queries.py`) to the same mechanism.
4. Report index state at startup and in the operator-visible prepare result.

Out of scope:

- `count_key_embedding` (`count_decision_repository.py`): already prefiltered to one subject's
  counted rows and documented as exact brute force; an approximate index would change
  correctness for no measured gain.
- Community similarity (no Menhir caller), FalkorDB/Kuzu/Neptune providers.
- Creating the indexes on production. That happens on the next approved upgrade.

## Proposed design

### Index DDL (`infrastructure/vector_indexes.py`, new)

One table of `VectorIndexSpec(name, kind, label, property)`:

| name | target |
|---|---|
| `entity_name_embedding_vector` | `(:Entity).name_embedding` |
| `relates_to_fact_embedding_vector` | `()-[:RELATES_TO]-().fact_embedding` |
| `episodic_content_embedding_vector` | `(:Episodic).content_embedding` |
| `typed_assertion_name_embedding_vector` | `(:TypedAssertion).name_embedding` |

`CREATE VECTOR INDEX <name> IF NOT EXISTS FOR ... ON ... OPTIONS {indexConfig: {vector.dimensions:
<d>, vector.similarity_function: 'cosine'}}`, with `d = expected_graphiti_embedding_dimension()`.
Unknown dimension: skip creation and log. Failure (old Neo4j without relationship vector indexes,
permissions): log and continue; that index stays absent and its searches keep scanning. Startup
never fails on these indexes, and they are not added to `PHASE_ONE_REQUIRED_INDEXES`, because a
populating index must not block writers.

An existing index with a different dimension is left alone (dropping is an operator decision) and
logged once; searches with a vector of another length use the scan.

### State cache

`VectorIndexState` reads `SHOW VECTOR INDEXES YIELD name, state, options` and caches
`{name: (state, dimensions)}` for 30 s. A search uses an index only if its cached state is ONLINE
and its dimension equals `len(query_vector)`. A query error that names the index (dropped after
the cache read) invalidates the cache, and that call falls back to the scan.

### Index-backed query with a completeness check

Each round sends two statements, and both start at the index call:

1. Stats: `CALL db.index.vector.queryNodes($index_name, $k, $vec) YIELD node AS hit, score AS
   index_score RETURN count(hit) AS raw, min(index_score) AS lowest`.
2. Search: the same call yielding `n`, then the existing filters on `n`, then the exact
   `vector.similarity.cosine` rescore, `score > $min_score`, ORDER BY, LIMIT, and Graphiti's
   return fields. Relationships use `queryRelationships` and bind `(n:Entity)-[e]->(m:Entity)`
   from the bound `e`, so the edge filters and return query are reused unchanged.

A first version looked the candidates up again by element id in a second statement. The live
plan test showed Neo4j planning that lookup through another index: `group_id`, which in the
default namespace matches nearly every node per candidate. Filtering the yielded rows directly
leaves the planner nothing to scan or seek. The test asserts every index query plan has the
procedure call and no Scan or Seek operator.

Index scores are not exact on quantized indexes, the 5.26 default: they were measured 0.0075-0.0095
off. So the indexes are created with `vector.quantization.enabled: false`, retrying without the
option on servers that lack it. Results are always rescored exactly, and the completeness check
uses a margin of 1e-4, or 0.02 for a quantized index. With `bound = lowest + margin`, the result
equals the scan's top `limit` (up to ANN recall) when `raw < k`, or when `bound <= min_score`, or
when the `limit`-th kept row scores at least `bound`. Otherwise `k` doubles (start
`max(4*limit, 64)`, cap 1024), and past the cap the call uses the scan. Both scales are
`(1+cos)/2`, so `min_score` keeps its meaning.

Filters that are already selective use the scan directly, because a scan of a few rows is exact and
cheap while oversampling would rarely reach them: `edge_uuids`, `edge_source_node_uuids`,
`edge_target_node_uuids`, and the `source/target_node_uuid` arguments. This covers edge dedup's
endpoint-pair search.

### Graphiti hook (`infrastructure/graphiti_vector_search.py`, new)

`MenhirSearchInterface(SearchInterface)` implements `node_similarity_search` and
`edge_similarity_search` as above. Every other hook method (fulltext, BFS, rerankers, community,
embeddings) calls the stock `graphiti_core.search.search_utils` function with a driver view whose
`search_interface` is `None`. That way the stock path runs unchanged and cannot recurse. The scan
fallback for the two vector methods uses the same view. Set on the `Neo4jDriver` in
`graphiti_client.py` before `Graphiti(...)` is constructed.

Parity note: stock `edge_similarity_search` applies `source/target_node_uuid` only when `group_ids`
is not None. The hook keeps that behaviour.

### Menhir's own lanes

`search_episode_embeddings` and `search_assertion_embeddings` use the same statements and
completeness rule, with their existing WHERE clauses applied to the yielded rows, falling back to
today's query. The repository uses an index only when it is given a catalog. The adapter passes
one; a bare repository, as in the unit tests, scans as before.

### Status

`prepare_memory_runtime` returns `vector_indexes: {name: state | "missing" | "skipped:<reason>"}`
and logs one line. The search interface logs once per index each time it switches between index
and scan.

## Alternatives considered

- Patch the Graphiti fork's Neo4j queries. Cleaner, but it is a second repository and a fork
  release, and it would not cover Menhir's own lanes. The hook exists for exactly this.
- Filter inside one Cypher statement after `queryNodes`. It cannot tell "filtered to nothing" from
  "the index ran out" without the raw count, which the completeness check needs.
- Require the indexes in readiness. That would refuse writers while a large index populates.

## Risks

- ANN recall below 100%: a near-duplicate entity can occasionally be missed by dedup at production
  size. Oversampling (k >= 4x limit) keeps HNSW recall high; parity tests run on fixture graphs.
- Index memory: unquantized, about 100 MB of vectors per 17k x 1536-d index (four indexes,
  two of them large), outside the Neo4j page cache. Check the production Neo4j container memory
  before the upgrade that creates them.
- A concurrent write between the stats and search statements can change the candidate set.
  The effect is limited to how one search is judged complete, and the next search sees the new
  state.
- Population time on first start: the scan path serves until the index is ONLINE.
- Mixed dimensions: nodes whose vectors have the wrong length are not indexed, so the index path
  skips them where the scan raises. Mixed-dimension graphs already fail preflight and need repair.

## Invariants

- With no ONLINE index, every search returns exactly what it returns today.
- The `candidate-readonly` mode creates no schema.
- Fulltext, BFS and reranker searches are untouched.
- `min_score` and group/namespace filters mean the same thing on both paths.

## Validation

- Unit: the decision logic (state, dimension, selective-filter, completeness or doubling or scan),
  hook delegation for every non-vector method, DDL text.
- Online (throwaway `neo4j:5-community` on ubuntu-server): the indexes are created on an empty
  database and on a pre-existing one; node and edge index results equal scan results on a seeded
  fixture, with and without `group_ids`, `min_score`, label and date filters, and a small group
  inside a large default group (doubling then scan); a POPULATING or absent index uses the scan;
  dropping the index mid-cache falls back; episode and assertion lanes match their scan.
- Existing focused suites: graphiti client, recall pipeline, degraded startup, embedding
  dimensions.

## Docs to update

- `.agent/architecture.md` (search path), `.agent/CHANGELOG.md`, `CHANGELOG.md` (Unreleased).
