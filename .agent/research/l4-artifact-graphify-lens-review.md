# L4 artifact implementation — Graphify-lens review

**Date:** 2026-06-28 · **Scope:** the menhir-side L4 institutional-artifact slice
**Files:** `src/menhir/infrastructure/artifact_repository.py`, `src/menhir/domain/artifacts.py`,
`src/menhir/services/artifact_service.py`, `src/menhir/services/memory_oracle_service.py`,
`src/menhir/infrastructure/schema.py`, adapter delegations in `memory_graph_adapter.py` (L827-865)

**Lens:** treat every Cypher touchpoint as an untyped escape hatch. For each, name the schema
it assumes, write its typed query/write contract, and verify the L4 trust invariants are
enforced at the *graph boundary* — not only in the service above it — then prove with tests
that raw flexibility cannot bypass the trust rules.

**Verdict:** the boundary was sound for promote / supersede / re-capture / reads, but had ONE
real hole — `create_artifact` trusted the caller-supplied `status`, so raw repository/adapter
access could mint a `trusted` LLM/no-evidence artifact (inv. 4 + 5 enforced only in the
service). Fixed by clamping status to the policy at the write boundary; bypass tests added.

---

## 1. Every raw graph operation

All Cypher lives in `ArtifactRepository` (the single sanctioned graph boundary); the adapter
(L827-865) is a pass-through, and `ArtifactService` / `MemoryOracleService` never touch Cypher.

| # | Operation | Kind | Cypher shape |
|---|---|---|---|
| 1 | `create_artifact` | WRITE | `MERGE (a:Entity {artifact_id})` + `ON CREATE/ON MATCH SET` + `FOREACH (ev … MERGE (e:Evidence{artifact_id,kind,ref}) MERGE (a)-[:SUPPORTED_BY]->(e))` |
| 2 | `promote_artifact` | WRITE | `MATCH (a:Entity {artifact_id}) WHERE scope='CANDIDATE' AND status='candidate' AND EXISTS{(a)-[:SUPPORTED_BY]->(e) WHERE e.kind<>'agent_inference'} SET trusted/PERSISTENT` |
| 3 | `supersede_artifact` | WRITE | `MATCH (old),(new) WHERE old.artifact_id<>new.artifact_id SET old.historical, old.superseded_by, new.supersedes MERGE (new)-[:SUPERSEDES]->(old)` |
| 4 | `find_artifacts` | READ | `MATCH (a:Entity) WHERE a.is_artifact AND (anchor overlap OR token CONTAINS) OPTIONAL MATCH SUPPORTED_BY … LIMIT` |
| 5 | `fetch_artifact` | READ | `MATCH (a:Entity {artifact_id}) WHERE a.is_artifact OPTIONAL MATCH SUPPORTED_BY …` |

## 2. Assumed graph schema

**Nodes**
- `:Entity` (artifact) — `artifact_id` (idempotency/lookup key, **indexed**), `uuid`, `name`,
  `summary`, `content`, `group_id=''`, `type='SEMANTIC'`, `scope ∈ {CANDIDATE, PERSISTENT}`,
  `is_artifact=true` (**indexed**), `artifact_type ∈ {decision, failure, incident}`,
  `artifact_status ∈ {candidate, trusted, historical}` (**indexed**), `artifact_source ∈ {human, llm}`,
  `artifact_anchors: list[str]`, `source_confidence`, `user_flagged=false`, `created_at`,
  `last_accessed`, `freshness='ACTIVE'`, `edge_count`, `sharpness`, plus `promoted_at?`,
  `superseded_by?`, `supersedes?`.
- `:Evidence` (first-class; **deliberately NOT in `MEMORY_NODE_LABELS`** so it skips the Entity
  backfill) — dedup key `(artifact_id, kind, ref)`, `uuid` (**indexed**), `directness`, `note`,
  `is_structural` (git/test), `created_at`.

**Edges:** `(:Entity)-[:SUPPORTED_BY]->(:Evidence)`; `(:Entity)-[:SUPERSEDES]->(:Entity)`.

**Indexes** (`schema.py::_artifact_index_queries`): `entity_artifact_id_idx`,
`entity_is_artifact_idx`, `entity_artifact_status_idx`, `evidence_artifact_id_idx`,
`evidence_uuid_idx`.

**Status→scope law** (`domain.artifacts.scope_for_status`): `trusted`/`historical` → `PERSISTENT`;
`candidate` → `CANDIDATE` (review tier, never recalled as fact).

## 3. Typed query/write contract per operation

**1. `create_artifact(artifact_id, artifact_type, summary, source, status, body='', evidence=None, anchors=None, source_confidence=0.5) -> {uuid, scope, status, created}`**
- Pre: `source ∈ {human, llm}`; `status ∈ {candidate, trusted}`; evidence rows need non-empty `(kind, ref)`.
- Post: idempotent on `artifact_id`; `:Evidence` deduped on `(artifact_id, kind, ref)`; `SUPPORTED_BY` linked.
- **Invariant:** persisted `artifact_status` ≤ policy(`source`, evidence) — never trusted unless human + ≥1 promotable evidence (inv. 4 + 5). `ON MATCH` refreshes summary/anchors/last_accessed only — never status/scope (no resurrection / silent re-trust, inv. 7). *(boundary guard added — see §4)*

**2. `promote_artifact(artifact_id, trusted_confidence=0.9) -> bool`**
- Pre (in Cypher): `scope='CANDIDATE'` AND `artifact_status='candidate'` AND ≥1 `SUPPORTED_BY` evidence with `kind<>'agent_inference'`.
- Post: → `trusted`/`PERSISTENT`, `promoted_at` set, confidence lifted. **Invariant 3** (no evidence ⇒ no trust) and **inv. 4** (agent_inference alone can't trust) enforced *in Cypher*. Historical can't be promoted (scope guard) — inv. 7.

**3. `supersede_artifact(old_id, new_id) -> bool`**
- Pre (in Cypher): both exist AND `old.artifact_id<>new.artifact_id` (no self-supersede).
- Post: `old → historical` + `superseded_by`, `new.supersedes`, `SUPERSEDES` edge. **No DELETE** anywhere (**inv. 7**).

**4. `find_artifacts(tokens, anchors, limit≤200) -> list[artifact dict w/ evidence]`** / **5. `fetch_artifact(id) -> dict|None`**
- READ-only; status returned **intact** (fact/hypothesis/stale bucketing is the brief's job, not the store's). `MemoryOracleService` is read-only *by construction* (exposes no create/promote/supersede).

## 4. Invariants at the boundary — findings

| Invariant | Enforced at graph boundary? | Where |
|---|---|---|
| 3 — promote needs evidence | ✅ | Cypher `EXISTS{… kind<>'agent_inference'}` |
| 4 — LLM never born trusted | ❌→✅ **(fixed)** | was service-only; now clamped in `create_artifact` |
| 5 — human trusted iff ≥1 evidence | ❌→✅ **(fixed)** | was service-only; now clamped in `create_artifact` |
| 6 — structural evidence is git/test, never LLM | ✅ | `_evidence_rows` derives `is_structural` from kind |
| 7 — supersede never deletes; no resurrection | ✅ | no DELETE; `ON MATCH` never touches status/scope; promote scope-guard excludes historical |
| self-supersede refused | ✅ | Cypher `old.artifact_id<>new.artifact_id` (+ service) |

**The hole (now closed).** `create_artifact` derived `scope` straight from the caller's
`status` (`"CANDIDATE" if status=="candidate" else "PERSISTENT"`) and never re-derived trust.
`ArtifactService.capture` is safe (no `status` param; calls `decide_status`), but anyone
holding the repository or `MemoryGraphAdapter` could call
`create_artifact(source="llm", status="trusted", evidence=[])` and mint a PERSISTENT trusted
artifact — exactly the "raw flexibility bypasses the typed contract" failure the Graphify lens
hunts for.

**Fix** (`artifact_repository.py::_policy_clamped_status`, applied in `create_artifact`): the
repository — the single graph writer — now enforces the create-time policy itself. A requested
`trusted` status is honored only when `decide_status(source, promotable_evidence)` would grant
it; otherwise it is downgraded to `candidate`/`CANDIDATE`. The guard only ever downgrades; an
explicit `candidate` is honored as-is; a legitimate human+git write stays trusted. This makes
trust non-forgeable at the boundary, mirroring the existing Cypher promote guard.

## 5. Tests proving raw flexibility cannot bypass trust (added)

`tests/test_artifact_repository.py` — call the repository **directly** (raw) with
`status="trusted"` and assert the persisted params are clamped:
- `test_raw_create_cannot_forge_trust_for_llm_source` — LLM + trusted + git evidence → `candidate`/`CANDIDATE` (inv. 4).
- `test_raw_create_cannot_forge_trust_without_evidence` — human + trusted + no evidence → `candidate` (inv. 5).
- `test_raw_create_agent_inference_only_cannot_forge_trust` — human + trusted + agent_inference-only → `candidate` (inv. 4 extended).
- `test_raw_create_legitimate_trust_is_not_over_clamped` — human + trusted + git → stays `trusted`/`PERSISTENT` (no false-positive).
- `test_raw_create_explicit_candidate_is_honored` — explicit candidate never upgraded.

Pre-existing tests already pin the other boundary guards in the Cypher (promote scope/status/
evidence, supersede historical/SUPERSEDES/no-DELETE/self-guard, `ON MATCH` no status/scope).

## Residual gaps (not addressed here)

- **Hand-written Cypher** outside `ArtifactRepository` can still violate everything — the
  repository is the lowest *sanctioned* boundary, not a database-enforced constraint. A true
  Graphify-grade guarantee would need DB constraints (e.g., a property/edge constraint), which
  Neo4j community indexes can't express; out of scope for this slice.
- `create_artifact` does not validate `artifact_type`/`source`/`status` against their enums in
  Cypher (the clamp handles `status`; unknown `source` → safe `candidate`). Typed validation of
  `artifact_type` is left to callers/domain enums.
- `status='historical'` is not a valid create input (it is a supersede outcome); `create_artifact`
  does not special-case it — callers must not pass it.
