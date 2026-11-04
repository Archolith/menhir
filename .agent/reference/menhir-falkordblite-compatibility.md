---
artifact_schema: 1
artifact_uuid: 1c402463-e686-46c3-91c3-7984ccfe9e05
artifact_type: investigation
artifact_status: COMPLETE
---

# Menhir #109: FalkorDBLite compatibility results

Measured 2026-11-04 from the `feat/109-flat-file-graph` worktree, based on `63d707e7`.
Issue: https://github.com/Archolith/menhir/issues/109. Broader first-run experience: #461.

## Decision

**Stop before implementing a general backend adapter.** The current Graphiti Falkor session
does not provide the callback rollback Menhir requires. Both a failed callback and a failed
Redis MULTI/EXEC batch left the first graph mutation committed. A driver swap or a thin
transaction wrapper therefore fails the compatibility gate. This does not prove that a
carefully designed single-query rewrite is impossible; that route remains unqualified.

Native Windows is also unqualified: the published FalkorDBLite 0.11.0 release has macOS and
Linux wheels, with no Windows wheel. The Linux run below does not qualify the Windows host.

## Reproduce

Use a disposable **Linux** Python 3.12 environment. The instrument accepts no existing database
path or service address: every run creates a new temporary embedded database and removes it.
Run from this checkout, retaining `pyproject.toml`, `src/menhir` and both probe scripts;
the script reads the fork pin there and loads this checkout's repository code.

```sh
python -m pip install 'archolith-graphiti-core[falkordb,falkordblite]==0.31.2.post2' 'falkordblite==0.11.0'
python scripts/falkordblite_compatibility.py
```

Expected exit code with these pins: **1**, with `decision: BLOCKED` and individual failed
capabilities in JSON. This is the experiment's result, not a passing backend qualification.
The probe does not load `.env`, use Menhir configuration, or call an LLM/embedding service.

Executed in a disposable `python:3.12-slim` Docker container, using `docker cp` to copy the script
and `pyproject.toml` (the Docker daemon did not accept the Windows bind mount). Environment:

- Linux x86_64, glibc 2.41; Python 3.12.15.
- `archolith-graphiti-core==0.31.2.post2`, `falkordblite==0.11.0`.
- Python clients: `falkordb==1.8.1`, `redis==8.1.0`.
- Embedded Redis 8.10.2; graph module reports version `42201` (not FalkorDB 6.x).

## Measured checks

| Check | Result | Evidence / interpretation |
|---|---|---|
| Fresh graph, parameters, MERGE replay | PASS | Two identical node/edge writes left one LINK edge. Not a concurrent uniqueness proof. |
| Namespace predicate and separate graph | PASS | Namespace `one` returned two nodes; a separate graph returned zero. Not Menhir-wide isolation qualification. |
| Single failed query | PASS | CREATE followed by an invalid map property raised and left zero FailedQuery nodes. Only this failure shape was tested. |
| Redis MULTI/EXEC rollback | FAIL | First GRAPH.QUERY created a node; the second returned an unknown-function error; one FailedBatch node remained. |
| Fork schema/index bootstrap | PASS | The actual FalkorDriver initialization task completed. |
| Fork entity save/vector recall | PASS | EntityNode.save followed by the fork's node_similarity_search returned the expected UUID using a fixed 3D vector. |
| Fork edge save/read/replay | PASS | EntityEdge save twice, get_by_uuid and count returned one edge with the original fact. |
| Fulltext lookup on fork-created index | PASS | `db.idx.fulltext.queryNodes` returned the expected Entity UUID. |
| Fork write callback rollback | FAIL | First `tx.run` created a node; a Python exception propagated; one FailedPublication node remained. |
| Embedded-server restart persistence | PASS | Saved, closed the old server and reopened the RDB with a different server PID; sentinel `durable` was retained. |

The container's PID 1 did not reap the old Redis process immediately. The probe confirmed its
state was `zombie` (no executing server), then required a different PID on reopen. A process
that remains live fails the restart check. This measures orderly SAVE/shutdown/reopen, not
power-loss durability, automatic crash recovery or backup/export correctness.

## Query incompatibilities measured

| Menhir/Neo4j construct | Result | Classification |
|---|---|---|
| `vector.similarity.cosine` | Unknown function | Query variant; Graphiti's native Falkor vector path worked. |
| `elementId(n)` | Unknown function | Query/identity variant; do not substitute unstable IDs without reviewing callers. |
| `SHOW INDEXES YIELD name ...` | Syntax error | Schema/introspection variant. |
| Named `CREATE CONSTRAINT ... IF NOT EXISTS ... REQUIRE ... IS UNIQUE` | Syntax error | Schema variant; native Falkor uniqueness semantics were not qualified. |
| `datetime()` | Unknown function | Temporal representation/query variant; converting parameters to strings alone does not translate Menhir queries. |
| Multi-statement callback with application validation | First write survives rejection | Transaction/invariant blocker for the current adapter mapping. |

Current code explains why this matters. `Neo4jRepository.execute_write` explicitly begins a
transaction, runs the callback and commits or rolls back; connection/commit ambiguity is not
retried. `projection_lifecycle_repository_writes.py` publishes definitions by first mutating
and reading coordination state, then validating in Python, then mutating definition/work state.
`MemoryGraphAdapter.write_project` also wraps the structural writer inside `execute_write`.
Mapping those callbacks to FalkorDriverSession.execute_write changes their failure behavior.

## Still unqualified from #109

### Transaction follow-up: P0/P1 (2026-11-04)

An AST scan of all 664 Python files under `src/menhir` and `scripts` at baseline `209b6005`
found eight direct runtime `execute_write` call sites, one operator backfill call and the
existing Graphiti probe call. The sole direct `begin_transaction` is in `Neo4jRepository`.
This inventories static transaction entry points, not all Cypher semantics, alias calls or
the entire P0 capability/writer matrix.

| Runtime/operation owner | Transaction requirement | Qualification state |
|---|---|---|
| `projection_lifecycle_repository_writes.publish_definition` | Read/lock current version, validate in Python, update definition and invalidate work | Guarded single-query candidate passes bounded live checks. |
| Same module: `dirty_targets`, `retire_targets` | Validate definition and advance exact target generations together | Candidate not implemented. |
| Same module: `reconcile_all_targets` | Read complete membership, compute additions/removals, apply both atomically | Python set computation needs a deliberate replacement strategy. |
| `projection_lifecycle_repository_reads.commit` | Fence, callback writes, Python result validation, receipt/certificate in one transaction | Actual callback rejection counterexamples reproduced. |
| `memory_graph_adapter.write_project_structure` | Identity/generation lock plus multi-query project publication/pruning | Not qualified for FalkorDB. |
| Same module: `write_document`, `refresh_indexed_binding` | Identity lock and structural writes/metadata together | Not qualified for FalkorDB. |
| `scripts/backfill_legacy_retention.main` | Revalidate manifest, then apply all reviewed links or none | Operator-only; retains Neo4j requirement. |

Related contract owners include `scalar_projection_materializer.py` (graph reads, Python fold,
View writes, persisted-state hash verification), `structure_queries.py`, namespace helpers,
schema constraints and graph/SQLite saga recovery. Existing projection tests own the callback
rollback reference behavior; raw reads/search/schema and Graphiti internal batches still need
the broader P0 audit. No backend abstraction was wired into runtime construction.

The probe now calls `falkordblite_projection_proof.py` against fresh temporary graphs:

- A positive control runs real `ProjectionLifecycleRepository.publish_definition`,
  `dirty_targets` and `commit`, and installs/certifies a synthetic View successfully.
- The deliberately unsafe autocommit facade translates only `datetime()` to a fixed string
  parameter. With real Menhir lifecycle commit code and a synthetic materializer, an empty hash and an existing
  conflicting receipt both raise the expected error **after leaving the View at value 2**;
  rollback should have retained its original value 1. These are expected failed gates, not fixes.
- The guarded `PUBLISH_DEFINITION` candidate makes its decision before mutation in one graph
  query. Advance/replay, stale version, conflicting hash, duplicate definition identity,
  certificate invalidation and unrelated-definition preservation pass.
- An invalid map property injected after definition/work mutations inside that candidate query
  raises and restores the complete prior graph snapshot.
- Eight pairs of concurrent version-2/version-3 publishers end at exactly one version-3
  definition with no missing/extra work-generation invalidation. Both linearization orders
  were observed in the initial eight-pair run; this is not exhaustive concurrency qualification.
- The orderly-restart check remains separate. Worker connections are disconnected after they
  join, so pooled sockets cannot be mistaken for independent runtime owners during shutdown.

**P1 remains unpassed; P0 is a bounded transaction inventory, not a completed backend audit.**
The candidate covers definition publication only, without runtime deployment. It does not
support arbitrary client callbacks, structure batches, post-write Python hash checks or all
existing/invalid data shapes. Process death, transport-level ambiguous commits and complete
native uniqueness/readiness qualification are not claimed.

The next design should prepare a bounded materialization payload before mutation, then validate
every relevant input/generation and persist payload plus receipt atomically. Its rejection,
hash/provenance and concurrent-input behavior must match the existing callback contract before
an adapter can proceed. This is a proposed contract change, not a proven solution or evidence
that an engine rewrite is necessary. Do not move folds outside transactions without that proof.
Large single-query batches must also account for upstream memory/rollback risks #1482/#1062;
this experiment deliberately uses small fixtures and does not run their crash/OOM reproducers.

Future implementation gates are indexed in [the memory backlog](../memory-backlog.md#memorybacklogembedded_graph).
The [Archolith FalkorDBLite fork](https://github.com/Archolith/falkordblite) owns the separate
Windows portability investigation; adding Windows binaries will not resolve the transaction blocker.

No runtime adapter was introduced. Full qualification of Menhir `add_memory`, actual enrichment,
namespace/uniqueness invariants, all projection-lifecycle concurrency/atomicity paths, saga/retry
safety, Menhir-native schema readiness and representative backup/export remains **NOT RUN**;
the bounded follow-up does not replace those gates. Related issue #258's
stale-save expiry protection on non-Neo4j backends is another required check, not resolved here.
Crash durability, concurrent processes, Linux arm64, macOS, Windows binaries, native dependency
bundling and redistribution obligations remain unqualified.

Proceed only after a separate design shows how each critical transaction preserves its
invariants with a modest query-variant/adapter layer and tests the failure/interleaving paths.
Redis pipelining or MULTI/EXEC alone is insufficient evidence. Retain Neo4j meanwhile.

## Verification

- The Linux probe above exercised real embedded storage and the published fork without mocks.
- Native Windows invocation returned `native_platform: FAIL` before importing the runtime.
- Focused offline tests cover failure reporting, unsupported-platform refusal and dependency-pin
  refusal; these do not substitute for live storage checks.
- `.venv/Scripts/python.exe -m pytest tests/test_falkordblite_compatibility.py tests/test_bump.py -q --tb=short`:
  37 passed, one upstream Pydantic deprecation warning. The worktree has its own fresh environment;
  the canonical checkout's existing environment held an older fork and could not load current tests.
- Ruff checks, including `F811,F821,ASYNC`, passed on the two Python files.
- `menhir artifacts validate . --repository menhir`: 19 existing findings. A validation of the
  `.agent` corpus archived from baseline HEAD returned the same 19 findings; no new findings.
- Full repository CI and the complete #109 checklist are not claimed by this investigation.
- Follow-up local selection: `pytest tests/test_falkordblite_compatibility.py
  tests/infrastructure/test_projection_lifecycle_repository.py -q --tb=short`:
  21 passed, 4 online Neo4j checks skipped, one upstream Pydantic warning. The new live
  projection checks run as executable assertions in the disposable Falkor probe.
- Final local selection also included `tests/infrastructure/test_scalar_projection_materializer.py`:
  25 passed, 4 online checks skipped; default Ruff and the scoped correctness/async gates passed.
- Final expanded native probe: 22 checks, 13 passing and 9 failed compatibility gates. The nine
  failures are the prior seven incompatibilities plus the two Menhir materialization refusal
  counterexamples; the publication candidate and orderly-restart checks passed. Exit code remains 1.

## Upstream evidence

- [FalkorDBLite usage and persistence](https://github.com/FalkorDB/falkordblite): runtime ownership and file API.
- [Published 0.11.0 files](https://pypi.org/project/falkordblite/0.11.0/#files): platform wheel availability.
- [Graphiti fork source at the pinned tag](https://github.com/Archolith/graphiti/blob/v0.31.2.post2/graphiti_core/driver/falkordb_driver.py): session callback execution without rollback.
- [Redis transactions](https://redis.io/docs/latest/develop/using-commands/transactions/): execution errors do not roll back other commands; Redis does not provide transaction rollback.
