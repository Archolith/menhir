# L4 commit 6 — live-graph verification checklist

> **STATUS: CONFIRMED LIVE 2026-06-28 — 42/42 assertions PASS, 0 FAIL.**
> Executed against a real (throwaway) Neo4j (bolt 7688) by the bench harness
> `archolith-bench/scripts/probe_l4_walk.py`, which imports the menhir
> ArtifactRepository / ArtifactService / MemoryOracleService as libraries (menhir src
> unmodified) and runs every 6a-6c step below with PASS/FAIL per Cypher assertion. All
> 9 invariants materialize: 5 artifact indexes ONLINE; first-class :Evidence via
> SUPPORTED_BY; LLM-never-trusted-on-create (4); human-trusted-iff-evidence (5);
> promote fail-closed incl. agent_inference-only (3); supersede marks historical + links
> SUPERSEDES + never deletes (7); no resurrection by re-capture (7-extended); oracle
> ranks anchor>topic, status intact, structurally write-free.
> **Decay/recall coupling (the watch item): confirmed by code** — a trusted artifact is a
> PERSISTENT :Entity carrying every field ENTITY_METADATA_FIELDS reads (type=SEMANTIC,
> scope, content, summary, last_accessed, freshness=ACTIVE, edge_count, sharpness,
> source_confidence), so fetch_candidate_metadata's `MATCH (n:Entity)` recalls it like any
> node. It is `user_flagged=false`, so it decays normally — the decay-exempt/flag decision
> remains a deliberate future choice, not a defect.

Commit 6 (the menhir port of the L4 artifact loop) is the only graph-schema change in the
slice, and it's the one piece that can't be verified in the remote sandbox — the full
pytest suite needs `httpx`/`graphiti_core`, and the value of this commit is that nodes and
edges actually materialize in Neo4j. So it ships logic-checked (pure-function probes +
Cypher-capturing stubs, run in-sandbox) and is **confirmed live, commit by commit, at
home**, against a real Neo4j.

This doc is that walk. Each step: apply the code, run the Cypher, confirm the assertion.
The bench (`archolith_bench/l4`, 28 tests green) is the falsifiable spec; live behavior is
checked to *match the bench*, not invented here.

## Setup

```bash
# from the menhir repo root, with the home env (neo4j up, deps installed)
git checkout claude/menhir-chain-handoff-doc-7iuat2
git pull origin claude/menhir-chain-handoff-doc-7iuat2

# 1) run the unit tests that the sandbox could only logic-check:
pytest tests/test_artifacts_domain.py tests/test_artifact_repository.py tests/test_artifact_service.py -q

# 2) open a cypher-shell / browser session on the menhir database for the live asserts below.
```

Use a throwaway id prefix (e.g. `lv_`) so cleanup is trivial. Cleanup at the very end:

```cypher
MATCH (e:Evidence) WHERE e.artifact_id STARTS WITH 'lv_' DETACH DELETE e;
MATCH (a:Entity)   WHERE a.artifact_id STARTS WITH 'lv_' DETACH DELETE a;
```

---

## Step 6a — domain policy (no graph)

Pure functions; nothing to assert in Neo4j. Confirm via `pytest tests/test_artifacts_domain.py`.
The four invariants it encodes — LLM never trusted on create (4), human trusted iff
evidence (5), promote needs evidence (3), no resurrecting historical (7) — are what the
graph steps below must not violate.

---

## Step 6b — ArtifactRepository write/read path

Drive it from a Python shell (`MemoryGraphAdapter(neo4j=<your Neo4jRepository>)`), or call
the repo directly. The asserts are pure Cypher you run afterward.

### 6b.1 — create a TRUSTED human Failure with one git evidence

```python
adapter.create_artifact(
    artifact_id="lv_floor_fail", artifact_type="failure",
    summary="fixed cosine floor dropped facet candidates", source="human",
    status="trusted", evidence=[{"kind":"git","ref":"e8da67d"}],
    anchors=["scoring_service.py"],
)
```

```cypher
// the artifact exists as a PERSISTENT :Entity carrying artifact_* fields, reusing SEMANTIC
MATCH (a:Entity {artifact_id:'lv_floor_fail'})
RETURN a.scope, a.type, a.artifact_type, a.artifact_status, a.artifact_source, a.artifact_anchors;
// EXPECT: scope='PERSISTENT', type='SEMANTIC', artifact_type='failure',
//         artifact_status='trusted', artifact_source='human', anchors=['scoring_service.py']

// evidence is FIRST-CLASS: a real node, linked by SUPPORTED_BY (the net-new structure)
MATCH (a:Entity {artifact_id:'lv_floor_fail'})-[:SUPPORTED_BY]->(e:Evidence)
RETURN count(e) AS evidence_count, collect(e.kind+':'+e.ref) AS refs, e.is_structural;
// EXPECT: evidence_count=1, refs=['git:e8da67d'], is_structural=true
```

### 6b.2 — idempotency + no evidence duplication on re-emit

Re-run the exact `create_artifact` from 6b.1.

```cypher
MATCH (a:Entity {artifact_id:'lv_floor_fail'}) RETURN count(a) AS artifacts;          // EXPECT 1
MATCH (:Entity {artifact_id:'lv_floor_fail'})-[:SUPPORTED_BY]->(e:Evidence)
RETURN count(e) AS evidence_count;                                                     // EXPECT 1 (not 2)
```

### 6b.3 — a CANDIDATE (LLM) artifact lands in the review tier

```python
adapter.create_artifact(artifact_id="lv_guess", artifact_type="failure",
    summary="maybe recency interacts with the floor", source="llm", status="candidate",
    evidence=[{"kind":"agent_inference","ref":"scoring_service.py","directness":0.3}],
    anchors=["scoring_service.py"])
```

```cypher
MATCH (a:Entity {artifact_id:'lv_guess'}) RETURN a.scope, a.artifact_status;
// EXPECT: scope='CANDIDATE', artifact_status='candidate'  (review tier, not recalled as fact)
```

### 6b.4 — promote is fail-closed without evidence (invariant 3)

```python
adapter.create_artifact(artifact_id="lv_hunch", artifact_type="decision",
    summary="consider lowering the floor", source="human", status="candidate")  # no evidence
adapter.promote_artifact("lv_hunch")   # returns False
```

```cypher
MATCH (a:Entity {artifact_id:'lv_hunch'}) RETURN a.scope, a.artifact_status;
// EXPECT: still scope='CANDIDATE', artifact_status='candidate' — the EXISTS{} guard refused it
```

### 6b.4b — promote refuses agent_inference-only evidence (Fix 1, invariant 4 extended)

`lv_guess` carries only `agent_inference` evidence (LLM self-evidence). The guard requires a
NON-agent_inference SUPPORTED_BY :Evidence, so this must be refused — trust is never granted
just because an LLM said so.

```python
adapter.promote_artifact("lv_guess")   # returns False — agent_inference is not promotable
```

```cypher
MATCH (a:Entity {artifact_id:'lv_guess'}) RETURN a.scope, a.artifact_status;
// EXPECT: still scope='CANDIDATE', artifact_status='candidate'
```

### 6b.5 — promote succeeds for a candidate with PROMOTABLE evidence (+ lifts confidence)

```python
adapter.create_artifact(artifact_id="lv_cand", artifact_type="failure",
    summary="floor interacts with recency", source="llm", status="candidate",
    evidence=[{"kind":"test","ref":"test_scoring_service::test_recency"}], anchors=["scoring_service.py"])
adapter.promote_artifact("lv_cand", trusted_confidence=0.9)   # has a test anchor -> returns True
```

```cypher
MATCH (a:Entity {artifact_id:'lv_cand'}) RETURN a.scope, a.artifact_status, a.source_confidence, a.promoted_at;
// EXPECT: scope='PERSISTENT', artifact_status='trusted', source_confidence=0.9, promoted_at set
```

### 6b.6 — supersede marks historical, links, never deletes (invariant 7)

```python
adapter.create_artifact(artifact_id="lv_floor_fix", artifact_type="decision",
    summary="rank candidates, source-aware floor after ranking", source="human",
    status="trusted", evidence=[{"kind":"git","ref":"e8da67d"}], anchors=["scoring_service.py"])
adapter.supersede_artifact("lv_floor_fail", "lv_floor_fix")
```

```cypher
// old kept (NOT deleted), flipped historical, lineage links both ways
MATCH (old:Entity {artifact_id:'lv_floor_fail'})
RETURN old.artifact_status, old.superseded_by;        // EXPECT 'historical', 'lv_floor_fix'
MATCH (new:Entity {artifact_id:'lv_floor_fix'})-[:SUPERSEDES]->(old:Entity {artifact_id:'lv_floor_fail'})
RETURN new.supersedes;                                  // EXPECT 'lv_floor_fail' and the edge exists
```

### 6b.7 — find_artifacts reads back by anchor, with evidence collected

```python
adapter.find_artifacts(tokens=["floor"], anchors=["scoring_service.py"], limit=10)
```

EXPECT: rows for `lv_floor_fix` (trusted), `lv_floor_fail` (historical), `lv_cand` (trusted),
`lv_guess` (candidate — agent_inference only), `lv_hunch` (candidate) — each with its `status`
intact and `evidence` list populated. The store returns history and candidates; it does not hide them.

### 6b.8 — a historical artifact cannot be resurrected by re-capture (Fix 2, invariant 7)

`create_artifact`'s `ON MATCH` refreshes provenance only — it must NOT touch artifact_status or
scope, so re-emitting a superseded id can neither resurrect it nor silently re-trust it.

```python
adapter.create_artifact(artifact_id="lv_floor_fail", artifact_type="failure",
    summary="re-asserted floor failure", source="human", status="trusted",
    evidence=[{"kind":"git","ref":"e8da67d"}], anchors=["scoring_service.py"])
```

```cypher
MATCH (a:Entity {artifact_id:'lv_floor_fail'}) RETURN a.artifact_status, a.scope, a.summary;
// EXPECT: artifact_status STILL 'historical', scope STILL 'PERSISTENT' (NOT flipped to trusted);
//         summary MAY be refreshed — provenance refresh is fine, status/scope are not.
```

---

## Step 6c — ArtifactService (R9-lite) + MemoryOracleService

Same DB, but now go through the service facade — this is the path application/agent code
uses. (`ArtifactService(graph_adapter=adapter)`, `await svc.capture(...)`.)

### 6c.1 — capture forges nothing

```python
await svc.capture(artifact_id="lv_s_llm", artifact_type=ArtifactType.FAILURE, summary="x",
                  source=ArtifactSource.LLM, evidence=[Evidence(kind="git", ref="abc")])
# returns status='candidate' despite evidence (invariant 4)
await svc.capture(artifact_id="lv_s_hum", artifact_type=ArtifactType.FAILURE, summary="x",
                  source=ArtifactSource.HUMAN, evidence=[Evidence(kind="git", ref="abc")])
# returns status='trusted' (invariant 5)
```

```cypher
MATCH (a:Entity) WHERE a.artifact_id IN ['lv_s_llm','lv_s_hum']
RETURN a.artifact_id, a.artifact_status, a.source_confidence;
// EXPECT: lv_s_llm -> 'candidate', source_confidence 0.4 (Fix 4 — llm candidate)
//         lv_s_hum -> 'trusted',   source_confidence 0.9 (Fix 4 — trusted, not the 0.5 default)
```

### 6c.2 — promote refusal reasons surface (no graph mutation on refusal)

```python
await svc.capture(artifact_id="lv_s_noev", artifact_type=ArtifactType.DECISION, summary="x",
                  source=ArtifactSource.HUMAN)                 # candidate, no evidence
await svc.promote("lv_s_noev")   # -> {"status":"refused","reason":"no_promotable_evidence"}
await svc.capture(artifact_id="lv_s_ai", artifact_type=ArtifactType.FAILURE, summary="x",
                  source=ArtifactSource.LLM,
                  evidence=[Evidence(kind="agent_inference", ref="x")])  # candidate, self-evidence only
await svc.promote("lv_s_ai")     # -> {"status":"refused","reason":"no_promotable_evidence"} (Fix 1)
await svc.supersede("lv_s_hum","lv_s_hum")  # -> {"status":"refused","reason":"self_supersede"}
```

```cypher
MATCH (a:Entity) WHERE a.artifact_id IN ['lv_s_noev','lv_s_ai']
RETURN a.artifact_id, a.artifact_status;   // EXPECT both still 'candidate' (unchanged)
```

### 6c.3 — oracle ranks anchor above topic, returns status intact, never writes

```python
hits = await MemoryOracleService(graph_adapter=adapter).find(
    text="tighten the similarity floor", anchors=["scoring_service.py"], limit=10)
# EXPECT: top hit matched_on includes "anchor"; historical lv_floor_fail present with status
# intact; no exception; MemoryOracleService has no create/promote/supersede attribute.
```

### 6c.4 — end-to-end parity with the bench headline

Feed the same corpus the bench fixture uses (`archolith_bench/fixtures/l4_failure_demo.json`)
through `svc.capture`, then run the oracle for the floor task. The set of surfaced artifacts
+ their statuses should match what `python scripts/run_l4_bench.py` produces in `with_l4`:
the TRUSTED Failure surfaces, the CANDIDATE stays a hypothesis, the superseded reads
historical. If live diverges from the bench, the bench wins — fix the port.

---

## Watch items (confirm, don't assume)

- **Decay/recall coupling.** Artifacts are PERSISTENT SEMANTIC nodes, so the existing
  decay + recall + conflict machinery applies to them once trusted. Confirm a trusted
  artifact is recalled like any PERSISTENT node, and decide whether artifacts should be
  decay-exempt or flagged (they are `user_flagged=false` today). This is the main reason
  the port is gated — verify it, don't assume it. Note Fix 4 now stores trusted artifacts at
  `source_confidence=0.9` (not the neutral 0.5), so scoring won't under-weight them.
- **Index + schema readiness (RESOLVED in code, confirm live).** The five artifact indexes
  are now in both `get_phase1_bootstrap_queries()` AND `PHASE_ONE_REQUIRED_INDEXES`, so an
  existing install reports `schema_not_ready` until they exist and bootstraps them. Confirm at
  home: after startup, `SHOW INDEXES` lists `entity_artifact_id_idx`, `entity_is_artifact_idx`,
  `entity_artifact_status_idx`, `evidence_artifact_id_idx`, `evidence_uuid_idx` ONLINE.
- **:Evidence label.** Registered in `schema.py` (`ARTIFACT_NODE_LABELS`), deliberately kept
  OUT of `MEMORY_NODE_LABELS` so it does not inherit the full Entity backfill.
- **DERIVED_FROM.** Only SUPPORTED_BY (evidence) + SUPERSEDES (lineage) ship in v0. The
  DERIVED_FROM provenance edge (LLM artifact -> source memory) is owed when the proposer
  emitter lands — not this slice.

## Rollback

Additive only. To back out: stop writing artifacts (remove the service wiring), then
`DETACH DELETE` `:Evidence` nodes and drop the `artifact_*`/`is_artifact` properties +
`SUPPORTED_BY`/`SUPERSEDES` edges. No existing label or property is modified.
