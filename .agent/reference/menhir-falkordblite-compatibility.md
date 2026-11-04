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
Run from this checkout, retaining its `pyproject.toml`; the script reads the fork pin there.

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

No runtime adapter was introduced. Menhir `add_memory`, actual enrichment, full namespace and
uniqueness invariants, projection-lifecycle concurrency/atomicity, saga/retry safety, Menhir-native
schema readiness and representative backup/export remain **NOT RUN**. Related issue #258's
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

## Upstream evidence

- [FalkorDBLite usage and persistence](https://github.com/FalkorDB/falkordblite): runtime ownership and file API.
- [Published 0.11.0 files](https://pypi.org/project/falkordblite/0.11.0/#files): platform wheel availability.
- [Graphiti fork source at the pinned tag](https://github.com/Archolith/graphiti/blob/v0.31.2.post2/graphiti_core/driver/falkordb_driver.py): session callback execution without rollback.
- [Redis transactions](https://redis.io/docs/latest/develop/using-commands/transactions/): execution errors do not roll back other commands; Redis does not provide transaction rollback.
