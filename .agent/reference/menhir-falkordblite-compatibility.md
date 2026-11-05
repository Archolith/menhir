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

**Production enablement remains blocked.** The current Graphiti Falkor session
does not provide the callback rollback Menhir requires. Both a failed callback and a failed
Redis MULTI/EXEC batch left the first graph mutation committed. A driver swap or a thin
transaction wrapper therefore fails the compatibility gate. This does not prove that a
carefully designed single-query rewrite is impossible; bounded candidates now pass, while the
complete backend remains unqualified. Internal integration may proceed under the review below.

Native Windows is also unqualified: the published FalkorDBLite 0.11.0 release has macOS and
Linux wheels, with no Windows wheel. The Linux run below does not qualify the Windows host.

### Architecture recommendation after comparing both routes (2026-11-04)

**Use an internal Menhir Falkor backend plus targeted, reusable Falkor fixes.** Do not build a
separate adapter service/package or attempt to make Falkor a drop-in Neo4j implementation. This
is an architecture recommendation, not a declaration that the backend is ready. P1 gates
production enablement; the 2026-11-05 minimal-work review below permits an internal vertical slice
before every transaction family is implemented.

| Route | What it requires | Assessment |
|---|---|---|
| Make Falkor emulate Menhir's existing Neo4j interface | Neo4j-facing transport/driver behavior, Cypher/procedure/DDL compatibility, temporal/result semantics, and genuine interactive transaction rollback | Largest engine/compatibility maintenance burden; integration wiring is still needed. Not the default route for one application's backend. |
| Do everything in a Menhir adapter against unchanged Falkor | Native query/schema mappings plus replacements for multi-query callbacks and application-side validation | Appropriate for API differences; unsafe if it pretends autocommit is transactional. Can become expensive if it duplicates business logic for every writer. |
| Internal backend plus focused Falkor improvements | Reuse existing clients/Graphiti provider; map genuine backend differences in Menhir; fix general engine/runtime deficiencies in their owners | Recommended, subject to proving the transaction contract before expanding runtime integration. |

The inspected FalkorDBLite fork imports as `redislite`: it launches Redis, loads the bundled
graph module, and delegates graph operations to `falkordb-py`. It contains neither the native
query executor nor its undo log. A Python transaction context manager added there cannot roll
back graph commands that already committed. Engine changes would belong in **FalkorDB/FalkorDB**
(or a separate core-engine fork), not just the existing **Archolith/falkordblite** wrapper fork.

Menhir's `Neo4jRepository.execute_write` genuinely begins a transaction, runs Python code that
can inspect intermediate results, and commits or rolls back afterward. The projection lifecycle
and structure/document publication use that contract. The installed Graphiti Falkor session's
`execute_write` merely invokes the callback; its `run` sends graph queries individually. This
matches the live partial-write failures. Reusing that provider is useful but does not qualify its
transaction semantics. A generic adapter cannot infer or safely rewrite arbitrary Python callbacks.

The pinned v4.22.1 engine has a query-context undo log; `cmd_query.c` rolls back errors for the
current command. Its registered graph commands have no BEGIN/COMMIT/ROLLBACK transaction API.
Extending this into interactive transactions would require connection/transaction ownership,
isolation and lock lifetime, cancellation/disconnect cleanup, rollback across commands, and
persistence/replication/crash behavior. It is a database-engine project, not a bounded wrapper fix.
Redis MULTI/EXEC does not supply the missing rollback, as both its documentation and our probe show.

Assign changes by ownership:

- **Menhir infrastructure:** backend selection, lifecycle wiring, native query/result/identity and
  schema mappings, readiness, export/restore and backend contract tests. Keep fold, authority,
  provenance policy and other business rules shared. Reuse the existing Graphiti Falkor provider
  and fix its integration where necessary; do not introduce another general graph abstraction.
- **FalkorDB core:** demonstrated general engine defects or missing reusable capabilities whose
  scope is justified independently of Menhir. Do not add Menhir-specific projection operations or
  reproduce Neo4j's whole API in the engine. Proper interactive transactions, if required, need a
  separate design/feasibility decision rather than silent scope expansion.
- **FalkorDBLite:** embedded process ownership, transport, packaging, artifact pins and Windows
  lifecycle. Native Windows additionally needs a working host/module/dependency build; a wrapper
  patch alone does not establish that support.

Native constraints already exist through `GRAPH.CONSTRAINT`; Neo4j DDL failing in the probe
does not mean Falkor lacks uniqueness. The adapter should use its native index/constraint APIs
and wait for enforcement readiness. Likewise, prefer native vector/fulltext APIs to engine-side
Neo4j procedure aliases. The pinned core source also contains DateTime/localdatetime primitives;
that is a reason to assess native temporal support before choosing an encoding, **not** evidence
that the bundled module implements Menhir's timezone-aware `datetime` contract. String substitution
remains diagnostic only. Do not upgrade to the separate Rust 6.x engine without fresh qualification.

**Original next gate (superseded by the minimal-work review below):** close the writer/capability
inventory and prove production-shaped atomic View/history and structure/document strategies. Keep single-query
variants only where they preserve the complete contract without duplicating domain rules. The
prototype's `count`/32-row limits are experimental bounds, not acceptable new product limits.
If that requires broad application rewrites, retain Neo4j and explicitly scope native transaction
engineering before proceeding. Do not keep expanding isolated prototypes as if they were a
working backend, and do not disguise missing guarantees in an `execute_write` facade.

Evidence: [Lite wrapper](https://github.com/FalkorDB/falkordblite/blob/ed2164762af1f433871fceb9b8ebbfd078ec2405/redislite/falkordb_client.py),
[pinned query rollback](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/commands/cmd_query.c),
[query-context undo log](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/query_ctx.c),
[registered commands](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/module.c),
[Redis transaction errors](https://redis.io/docs/latest/develop/using-commands/transactions/),
[native constraint API](https://docs.falkordb.com/commands/graph.constraint-create),
[temporal primitives](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/arithmetic/time_funcs/time_funcs.c),
and [the inspected Graphiti fork](https://github.com/Archolith/graphiti/blob/v0.31.2.post2/graphiti_core/driver/falkordb_driver.py).
This comparison inspected source and existing results; it added no runtime changes or new live tests.

### Minimal-work architecture review (2026-11-05)

The ownership decision still fits #109: one internal Menhir Falkor implementation, the existing
Graphiti Falkor provider, and one managed Lite runtime. The execution order had drifted toward
an expanding standalone proof suite instead of a usable backend. Stop adding synthetic or
captured-query proof families by default. Existing proofs are evidence and test fixtures, not a
second persistence architecture to port wholesale into production.

Code-backed reasons to change the order:

- `core/bootstrap.py:153` still constructs `Neo4jRepository`, while
  `infrastructure/graphiti_client.py:481` independently constructs `Neo4jDriver`. No selectable
  Falkor runtime path exists. These are the two seams to connect, sharing one embedded server,
  socket/data location and graph name. The pinned driver already accepts `falkor_db=...`; the
  successful spike creates its async client on the Lite server's existing Unix socket.
- The active scalar rebuild is constructed in `MemoryGraphAdapter.scalar_state_service`
  (`memory_graph_adapter.py:273`), and scalar state is enabled in `config/feature_flags.py:53`.
  No runtime construction of `ScalarStateProjectionMaterializer` or `ScalarProjectionReconciler`
  was found under `src/menhir`. Finishing every T5 prototype transition before integrating the
  active path is the wrong dependency order. This is dormant cutover infrastructure, not evidence
  that active scalar View behavior can be omitted.
- Temporal compatibility remains unresolved across actual Menhir and Graphiti operations. The
  passing string-temporal proofs cannot define the production representation. Resolve shared
  time storage, comparison, hydration and timezone behavior before expanding those proofs.

Shortest implementation order:

1. Resolve the time/result mapping on the pinned engine using actual active queries and the
   Graphiti provider. Reuse the existing instrument for a focused blocker test only if needed.
   If preserving the contract requires broad engine work, stop this route immediately.
2. Wire one internal, unreleased Linux slice through existing bootstrap and Graphiti construction:
   start/open a file, native schema readiness, direct repository write/read, real Graphiti-backed
   `add_memory`/enrichment/recall, and restart. Keep Neo4j as the working default. This is integration
   work, not permission to expose a partially correct backend as supported.
3. Implement native query/schema variants at existing owners. Reuse shared folds, payload builders
   and policy hooks. Adapt the three active explicit callback entry points (project, document,
   binding refresh) as operation-specific atomic commands; do not build a general callback compiler
   or a runtime dependency on `scripts/`. Cover active scalar refresh/retirement and saga recovery.
4. Close all #109 acceptance requirements, including projection-lifecycle atomicity, hook safety,
   namespace/uniqueness, failures/retries and export/restart, on the actual backend. Retain dormant
   T5 contract tests as release gates without activating the T5 cutover as a side project. Package
   supported Linux using the existing Lite distribution first; qualify Windows separately.

Do not add a new adapter service, parallel storage protocol hierarchy, custom runtime wheel,
engine transaction API, native Windows port, new engine version or default-backend migration to
the next implementation slice. No broad rename of existing `neo4j` variables/types is required
to prove that slice. Reuse the existing repository surface and add only capabilities exercised
by real callers. The P1 correctness gate moves to production enablement; it no longer prevents
internal integration from discovering incompatibilities sooner. All acceptance requirements remain.
Do not chase a zero-failure result in the original compatibility spike: its unchanged Neo4j-syntax
probes and unsafe-autocommit counterexamples can remain negative controls. Supported behavior must
pass backend-native contract tests instead; native constraint/vector APIs need no Neo4j aliases.
The earlier token bands remain conditional estimates, not justification to spend the full budget;
re-estimate after the temporal decision and real vertical slice rather than extrapolating proof count.

### Internal shared-runtime slice (2026-11-05)

`infrastructure/falkor.py` now owns a Lite file/server and exposes native query rows plus the
existing Graphiti async driver on the same Unix socket and graph name. Injection through
`build_memory_services(graph_repository=...)` and `GraphitiClient.from_settings_with_capabilities`
preserves the default Neo4j path. It is an internal seam, with no CLI/environment backend selector.
Matching Graphiti construction failure propagates rather than silently degrading the injected pair.
Callers close the Graphiti client before the repository; failed persistence leaves the owner open
for recovery. The repository never replays ambiguous transport failures and refuses arbitrary
callback transactions before invocation, retaining the existing saga revocation checks.

The time representation uses the pinned Graphiti provider's canonical UTC ISO strings and Python
microsecond precision. Explicit instant encode/decode preserves offsets, normalizes naive inputs
as UTC, and rejects invalid/finer-precision input instead of silently truncating it. Arbitrary
strings and query text are not rewritten. Calendar-date targets remain `YYYY-MM-DD`. The active
TEMPORAL repository uses native parameter expressions and Python-computed UTC window bounds;
Neo4j retains typed temporal expressions. Other time-query owners and mixed-format imports remain
unmapped; this does not qualify every temporal operation or activate the previous diagnostic facade.

A disposable Linux test builds the real Menhir services with the shared repository, awaits native
Graphiti index initialization, creates/lists/completes a namespaced TEMPORAL memory through the
actual graph adapter, saves/reads an entity through Graphiti, checks the same entity/time from
Menhir, compares canonical instants in the store, then closes/reopens the file and reads it again.
No LLM/embedder service calls were made in this initial slice. The following milestones add native
startup and controlled-model queued enrichment/recall. No standalone proof helper was added.

Verification: **168 local tests passed, 6 skipped**, covering the changed owners plus neighboring
Graphiti, degraded-startup, service-pipeline and authority contracts. One skip is the Windows host's
missing IANA timezone data; DST-fold preservation passes in the native Linux environment. That
environment passed **19 repository tests**, including the live shared-runtime/restart case.
Default Ruff passes for the new owner/tests; scoped correctness checks pass for existing owners.
No full CI, public backend readiness or complete enrichment/recall qualification is claimed.

### Native phase-one startup/readiness (2026-11-05)

`infrastructure/falkor_schema.py` now implements the existing core startup contract through
`MemoryGraphAdapter`, so the shared runtime can call `prepare_memory_runtime`. Required indexes
map to native label/property/kind metadata; uniqueness uses `GRAPH.CONSTRAINT` and operational
`db.constraints` records. An ordinary index cannot satisfy uniqueness. Composite property order
is ignored because native uniqueness is defined over the property set. Bootstrap covers the
phase-one required indexes and all ten unconditional uniqueness rules; optional performance indexes
and feature-activation DDL remain unmapped. Graphiti-owned fulltext/range requirements are conditional
on the existing availability decision. No Neo4j-named DROP is applied to shared native indexes.

Enforcement must become operational within a bounded wait before data defaults run. Existing
node/edge backfill policy is shared with an explicit UTC string clock; Neo4j retains `datetime()`.
Readiness also checks the existing version stamps and unresolved conflict defaults. Thus a failed
backfill is retried even when DDL is already complete, without introducing another persistent marker.
Completed startup skips bootstrap. Existing trust/flag fields are retained; duplicate ingest keys
refuse bootstrap before data defaults change. No duplicate rows are deleted or repaired automatically.

The actual startup path exposed a Neo4j-only index-count query in `GraphitiClient`; its native
variant now handles Falkor's tuple result. It also exposed transport replay below the repository:
the pinned Redis constructor defaults allow ten retries. Both client pools now explicitly use zero
retries. The Falkor async SDK first makes a sync INFO probe, so construction uses the sync retry
object, then installs the async zero-retry policy before Graphiti can dispatch mutations.

Verification: **183 local tests passed, 6 skipped**; **32 tests passed** in the disposable Falkor
environment, and **5 existing Neo4j bootstrap integration tests passed**. The final shared-runtime
case also passed a targeted rerun asserting a different server PID after close/reopen. Live checks
cover startup interruption/recovery, idempotent readiness, retained user metadata, duplicate-data
refusal and both zero-retry pools. Static checks pass on the changed owners. No full CI claimed.
That milestone left queued enrichment/recall as the next slice; backend selection stays internal.

## Reproduce

Use a disposable **Linux** Python 3.12 environment. The instrument accepts no existing database
path or service address: every run creates a new temporary embedded database and removes it.
Run from this checkout, retaining `pyproject.toml`, `src/menhir` and the `scripts/falkordblite_*.py` helpers;
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
| `memory_graph_adapter.write_project_structure` | Identity/generation lock plus multi-query project publication/pruning | Compound-query candidate passes the full-writer fixtures below; production wiring, scale and broader qualification remain open. |
| Same module: `write_document` | Identity lock and structural document MERGE together | Production-shaped fused query passes the live checks below; no runtime backend wired. |
| Same module: `refresh_indexed_binding` | Identity/scan lock and conditional metadata update together | Runtime native guarded command implemented; live CAS, refusal, slot cleanup and late-error rollback checks pass (milestone below). |
| `scripts/backfill_legacy_retention.main` | Revalidate manifest, then apply all reviewed links or none | Operator-only; retains Neo4j requirement. |

Related contract owners include `scalar_projection_materializer.py` (graph reads, Python fold,
View writes, persisted-state hash verification), `structure_queries.py`, namespace helpers,
schema constraints and graph/SQLite saga recovery. Existing projection tests own the callback
rollback reference behavior; raw reads/search/schema and Graphiti internal batches still need
the broader P0 audit. No backend abstraction was wired into runtime construction.

The explicit callback inventory was rechecked across all **543** files under `src/menhir` in
the current worktree. It still has eight direct runtime `execute_write` calls and the sole
`begin_transaction` at the repository boundary. Following the callbacks to their query owners
distinguishes the remaining work:

- `dirty_targets`/`retire_targets` lock/check the definition, then call `_dirty_targets_tx` for
  one UNWIND/MERGE/SET batch. That helper validates returned row count and decoded target identity
  **after** mutation; a Falkor variant must validate existing identities and complete target-set
  coverage before writes or reject inside the atomic operation, not raise after autocommit.
- `reconcile_all_targets` additionally reads every existing member, validates uniqueness/version,
  computes desired/current differences in Python, and applies both present and absent batches.
  Its membership snapshot/difference publication remains unqualified.
- `write_project_structure` holds the identity/scan lock across `StructureGraphWriter.write_project`:
  current file-mtime read, incremental changes, conditional role/entity and relationship prunes,
  entity/edge batches, inferred project targets and symbol/call rewrites. Partial scans deliberately
  suppress destructive absence-based pruning. The final fingerprint/coverage/Beacon marker is
  another query whose returned count must be exactly one; failure must undo every earlier write.
  This is the large structural transaction gate. The compound-query follow-up below now exercises
  it against real writer fixtures. Do not replace it with independently committed batches or treat
  the simpler document proof as equivalent evidence.
- `write_document` has a much smaller shape: identity validation plus one `_merge_entity` call.
  `refresh_indexed_binding` is identity/scan validation plus one conditional MATCH/SET body.
- Projection `commit` remains the real materializer/history/receipt/certificate gate. Its current
  prepared proof covers source validation and synthetic outputs, not the complete production path.

This closes the classification of the known explicit runtime callback entry points. It does not
close P0: indirect/raw query sequences, Graphiti internal operations, schema/readiness and saga
recovery still need coverage. Graph/SQLite sagas require their existing ownership/recovery protocol;
a graph transaction must not be presented as an atomic transaction spanning both databases.

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

The prepared-materialization experiment below explores a bounded payload and atomic receipt.
Its production hash/provenance and concurrent-input behavior must match the existing callback
contract before an adapter can proceed. Do not move production folds outside transactions
without that proof; no engine rewrite has been demonstrated necessary.
Large single-query batches must also account for upstream memory/rollback risks #1482/#1062;
this experiment deliberately uses small fixtures and does not run their crash/OOM reproducers.

### Production-shaped document publication (2026-11-04)

`document_publication_statement` in the existing projection-proof helper obtains the single
MERGE statement and parameters by invoking the **actual** `StructureGraphWriter.write_document`
with a capture repository. It does not recreate document defaults, provenance metadata or update
rules. It adds a backend query variant that validates the current project identity and rejects
duplicate identities/document keys **before** mutating the identity probe or document. The identity
probe and unchanged MERGE body execute together in one Falkor graph query.

The existing `admit_structure_writer`/`release_structure_writer` protocol surrounds publication;
the compiler is not a standalone runtime repository and cannot authorize an unadmitted caller.
The document's durable project identity must equal the claim. If the production writer ever emits
more than one statement, compilation refuses instead of silently retaining only one of them.
This technique is deliberately specific to this writer, not a translator for arbitrary callbacks.

All **13** live checks pass against fresh graphs using real `ProjectIdentity`, `StructureWriteFence`
and document `Entity` nodes:

- Creation and update match the actual writer's stored fields, including create-only session
  metadata, UUID and creation-time preservation. The reference path executes the real identity
  lock plus document writer; the candidate reuses that writer's MERGE and payload construction.
- Changed/missing host, changed root/generation/state, missing identity, duplicate identity and
  duplicate document keys refuse without changing any node, relationship or identity probe.
- A deliberately invalid property write **after** publication rolls back both document creation
  or update and the identity probe. This is native single-query rollback, not compensation.
- The real migration fence refuses new admissions while an already-admitted writer can finish;
  releasing it clears both identity and global registrations. Refusing all admitted writers merely
  because a migration raised its fence would strand the drain, so that is not the query's rule.
- Two valid, admitted concurrent writers serialize onto one document with the existing
  last-writer behavior. The query does not invent a new document revision/order policy.

No datetime replacement is required for this document writer: its existing timestamps are already
ISO strings. The migration-fence helper still uses the original probe's fixed-string translation
for `datetime()`; the drain test qualifies admission behavior, not native temporal types.

This passes the document-publication slice only. It does not qualify full project scans/prunes,
scan-generation publication, native constraint/readiness setup, lease reaping/orphan cleanup,
cross-process/crash recovery, or real View/history certification. Existing ambiguous-commit handling
must remain fail-closed; document MERGE is not authority to retry an old write after a peer update.
No general `execute_write` implementation, backend runtime selection or production service changed.

### Full project writer compound-query follow-up (2026-11-04)

The pinned engine already supports pure JavaScript UDFs. A small probe-owned `requireOk` function
throws when a predicate is not exactly true; native execution then rolls back the entire graph
query. The live check verified this after a preceding CREATE. This supplies an explicit postcondition
mechanism without a deliberate invalid-property hack, compensating writes or a new engine feature.
It does **not** introduce interactive transactions or change the current Graphiti callback behavior.

`project_publication_statement` now invokes the real `StructureGraphWriter.write_project` to
produce a compound-query candidate:

1. Capture the actual read-only mtime queries/results, then let the writer retain its existing
   Python incremental/full/partial-scan policy. Revalidate those exact mtime inputs in the commit.
2. Capture the existing write statements and payloads; compose them as ordered, correlated CALL
   subqueries in one graph command. Parameters are rebound without changing strings/comments.
   Unexpected additional reads are refused rather than answered with fabricated state.
3. Validate identity and scan generation inside that command, then mutate the publication probe
   and execute the writes. Actual batch-edge counts are returned. Planning uses placeholders only
   for diagnostic write counters; it does not emulate arbitrary callback results.
4. Assert the actual final completion count is **exactly one** inside the query. Zero/multiple
   markers and later failures must undo every earlier entity, edge, prune and metadata write.

The native fixture installs Entity `(structure_project_id, structure_path)` and ProjectIdentity
`project_id` indexes/unique constraints and waits for `OPERATIONAL` before publication. This is
evidence for those native schema APIs, not the complete Menhir/Graphiti schema-readiness gate.
The normal structure admission/release protocol still surrounds the operation.

The full-writer fixture covers directories, file/config/test/entrypoint/scanner-document roles,
dependencies, endpoints, imports, test links, symbols and intra-project calls, inferred external
projects and nested repositories. Initial and incremental complete/partial publications match
the real writer's semantic graph and entity/edge counts. Partial scans preserve absent files;
complete scans prune them. A foreign project and a separately ingested document remain intact.
Failure cases cover changed mtime inputs, stale scan generation, ambiguous legacy/current mtimes,
zero/multiple completion markers, invalid properties after earlier writes, late failure after the
completion query, missing UDF readiness and competing old/new scan generations.

**Temporal scope matters:** only this writer's zero-argument edge clock `datetime()` is mapped
to native `localdatetime()` in the tested UTC runtime. It stores native temporal values rather
than the earlier diagnostic strings. Parameterized `datetime(value)` is not rewritten. A separate
probe observed `localdatetime('2026-01-01T00:00:00+02:00')` returning wall time 00:00 with the
Python driver attaching UTC, rather than normalizing the instant to 22:00 UTC. Do not alias
timezone-bearing datetime constructors to it. Timezone/precision semantics and runtime UTC
configuration still need a complete backend contract.

The orderly-restart check now compares the completed project's nodes/edges/temporal values and
confirms the UDF is restored, in addition to the sentinel. UDF startup/version/ownership checks,
query size/scale, OOM/timeout behavior, crash/ambiguous-response recovery and the broader P0/P1
writer contract remain unqualified. Some nested failures surface as a later postcondition error;
the tests require rejection plus complete rollback, not preservation of one incidental error text.
Provider exception mapping must be qualified before runtime wiring.

This is a **production-shaped compound candidate**, not a general callback translator or a
shipping adapter. It reuses current writer policy instead of duplicating it and introduces no new
project-size limit. The source-bound scalar proof still writes synthetic Views; real View/history
certification and parameterized temporals remain outstanding. No engine/runtime production change.

Sources: [pinned UDF invocation](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/arithmetic/udf_funcs/udf_funcs.c),
[native UDF load command](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/commands/udf/cmd_udf_load.c),
and [temporal constructors](https://github.com/FalkorDB/FalkorDB/blob/v4.22.1/src/arithmetic/time_funcs/time_funcs.c).

### Prepared numeric materialization follow-up (2026-11-04)

`falkordblite_prepared_materialization.py` implements an isolated candidate contract;
`falkordblite_prepared_proof.py` owns its live cases. Neither is imported by production runtime.
Preparation uses Menhir's actual `SCALAR_STATE_PROJECTION.fold` and canonical scalar hashes.
An immutable JSON snapshot is retained; payload, hash and certificate fields are derived again
at dispatch, rather than accepting a caller-supplied result hash or arbitrary write callback.

The snapshot includes definition properties, work state, all synthetic input properties for the
subject/namespace, the old synthetic View and the generation's receipt. The commit query checks
all of these before replacing the payload and recording the receipt/certificate together.
Exact committed post-state permits a no-write replay; changed input, work, View or receipt
state is never blindly acknowledged as a replay.

The database clock bounds preparation to 30 seconds or the earliest future assertion activation,
whichever comes first. The query refuses an expired plan. A fresh fold after activation includes
the newly eligible assertion. This is validity at the atomic-query decision point, not a promise
that a View remains current indefinitely. Recovery after expiration needs a separate read-only
receipt/freshness check; that production recovery path has not been implemented.

The candidate deliberately accepts only `count` scalar targets with explicit namespaces and at
most 32 input rows. A bounded read fetches one extra row to detect overflow instead of certifying
a truncated set. This bounds result/write payload sizes, not the engine's total scan/sort memory.
Other scalar kinds fail closed. Stored payload identity, contributors, episodes and hash are
covered by offline contract checks; the original variant represents no assertion/provenance
relationship closure. The source-bound extension below adds that closure for a restricted profile.

Live cases cover present/retired payloads and exact replay, source modification/insertion/deletion,
changed definition/generation/View, conflicting receipt, duplicate View identity, injected failure
after payload/certificate writes, competing publishers, activation-time expiry, namespace isolation,
input overflow, and replay after committed state changes. A child writer exits after the database
reply but before acknowledging to its parent; the parent-retained plan recognizes the persisted
result without mutating it again. This is a worker-process-loss experiment, not a database crash,
a real transport-fault injection or recovery after loss of the parent and its retained plan.

**This establishes a viable bounded prepared-write mechanism, not full P1 acceptance.** It uses
synthetic `Prepared*` labels, JSON View payloads and flat input properties. Binding the snapshot
to real materializable-assertion joins, authority/provenance changes, real View retirement/history,
all supported value types and every remaining transaction writer is still required. Production
code and the old callback path are unchanged; their earlier negative gates remain failures.

### Production-schema follow-up (2026-11-04)

The same probe now executes `TypedAssertionRepository.materializable_assertions_for_entity`
against real `TypedAssertion`, `TurnEvidence`, `Episodic` and `ADMITTED_ON` fixtures. Its native
read passes: pending, superseded and foreign-namespace assertions are excluded, the grounding
turn resolves to the admitted source episode, and an authority-tier change alters the canonical
projection hash. These fixtures store ISO timestamps as strings; they do not qualify Neo4j
temporal property values.

This exposed a fixture bug: the earlier prepared experiment used `value_kind='number'`, which
the fold accepts but the real `ScalarStateKind` rejects. The candidate now accepts only the
production `count` kind. An offline test crosses the actual View-kind boundary, and the canonical
present/absent hash fixtures changed with that target identity. Other kinds remain unsupported.

The initial production-schema run had two executable acceptance checks deliberately **FAIL**,
exposing missing production guards in the original synthetic candidate:

- Prepare from the real read, change only `ADMITTED_ON` to another source episode, then commit.
  The actual read/fold hash changes, assertion properties stay identical, and the synthetic
  commit still returns `apply` with its original hash.
- Prepare, quarantine the grounding `TurnEvidence`, then commit. The assertion read/fold hash
  stays identical, but the evidence is no longer eligible for a current FACT View. The synthetic
  commit still returns `apply`; it has no evidence-liveness gate.

These are failures of extending the original isolated candidate to production, not new defects in
the unchanged Neo4j runtime. They established the requirement to read the production assertion/provenance
closure and derive the fold from that same snapshot, then revalidate it **inside** the atomic
commit. Include source identity/link changes, evidence finalization/quarantine, tenant scope,
namespace-fence generation and duplicate/ambiguous evidence. Merely comparing copied assertion
properties or the projection hash is insufficient. A separate preflight read leaves a race.

The real `ViewRepository.record_scalar_state` create/supersede path fails natively on unknown
`datetime`. A separate, explicitly diagnostic facade replaces `datetime($parameter)` with the
parameter's ISO string (and the existing probe substitutes a fixed string for `datetime()`).
Under those restricted fixtures the **actual writer** passes creation, supersession with
`SUPERSEDES`, same-value authority/provenance refresh with `MENTIONS`, and retirement, retaining
both historical versions. This neither qualifies temporal ordering/types nor combines those
multi-query operations atomically with the prepared certificate. No runtime translator was added.

P1 remains unpassed. The synthetic View overwrite must still be replaced with guarded real View
history/provenance writes in the same commit as the work certificate and receipt, including
unchanged-value refresh, retirement, rejection/rollback, concurrent publication and replay.

### Real materializer and Neo4j certification oracle (2026-11-04)

Crossing the actual materializer/View/hash-reader boundary exposed two shared Menhir bugs:

- The assertion read returns a source episode UUID, while the View writer resolves it to its
  grounding turn. The expected certificate hashed the episode alias and therefore rejected the
  correctly installed View. The materializer now independently resolves the evidence anchors
  before writing and uses those same IDs for the expected hash; the installed hash is still checked.
- The View writer passes `timeout_s` to repository writes, but the explicit transaction adapter
  did not accept it. The adapter now requires an equal or narrower whole-transaction budget,
  forwarded through `ProjectionLifecycleRepository.commit` to Neo4j `begin_transaction(timeout=...)`.
  Unbounded generic callbacks retain their existing default. Invalid or unenforceable statement
  budgets fail before RUN rather than being ignored. This adds no Falkor transaction emulation.

A disposable Neo4j 5.26.30 Community instance passes the real episode-to-turn bridge, initial
certificate, history/supersession, same-value authority refresh, retirement, and conflicting-receipt
rollback. A rejected certificate leaves the prior current View and history unchanged. The selected
online materializer/lifecycle/atomicity files pass **37 tests**; the focused local transaction,
View, hash-reader and neighboring Falkor contract regression set passes **122 tests**, with
**8 online tests skipped**.
These are focused results, not full-suite CI or Falkor atomicity evidence.

The existing Falkor instrument also exercises the actual materializer and installed hash reader
through its explicitly diagnostic string-temporal facade. This checks normalized provenance and
real history/retirement on the pinned engine; it does not combine those autocommit writes with a
work receipt or qualify temporal types. P1 still requires real View/history, receipt and certificate
publication in one guarded native query, plus refusal, replay and competing-writer coverage.
The complete disposable Falkor probe reports **93 checks: 83 PASS, 10 FAIL** (`BLOCKED`);
the new real-materializer diagnostic passes and the ten existing failed capability gates remain.

### Atomic real-View create/supersede candidate (2026-11-05)

`PreparedRealViewScalar`, `prepare_real_view` and `commit_real_view` now qualify a restricted next
step beyond the synthetic View payload. Preparation uses the same production assertion/evidence
snapshot and real fold, then captures the actual materializer's two known write bodies: the View
create/supersede statement and the scalar provenance rewrite. No mutation occurs during preparation;
unknown mutation shapes fail closed. Both bodies execute as ordered correlated subqueries inside
one native graph command with `ProjectionDefinitionState`, `ProjectionWorkState` and
`ProjectionDerivationReceipt`. The candidate writes real `Entity` Views, `SUPERSEDES`, `MENTIONS`,
`CURRENT_ANCHOR`, `CONTRIBUTED_TO` and `SUPERSEDED_ANCHOR` relationships, rather than `PreparedView`.

The command revalidates definition, work generation, source/evidence identities, namespace fence,
preparation deadline, receipt absence and prior history node properties/edge identities before
writing. The history snapshot includes current and prior versions for the exact View key and
refuses foreign subjects/namespaces. The evidence snapshot excludes the writer's advisory lock
nonce/time fields from comparison; generation and fence identity remain authoritative. Otherwise
the writer's own lock stamp would invalidate every replay.

Each write's returned result is a required dependency of the next stage. Pure native UDF
postconditions check the resulting current View, canonical value/time, contributors/authority,
normalized grounding anchors, current-key marker, retained history count, supersession edge and
live provenance edges before installing the work certificate and receipt. Replay rechecks source
eligibility and these current-output/provenance postconditions without writing. The real installed
hash reader agrees with the prepared certificate for both initial publication and supersession,
including a numeric `10.0` input normalized to the persisted scalar string.

All **16 focused native cases pass**: initial/superseding publication, exact no-write replay,
late failure after work/receipt mutations for both creation and supersession, changed evidence,
work, definition and prior history refusal, conflicting receipt, writer/provenance/postcondition
rejection, competing publishers, replay source change, missing replay provenance, and explicit
profile refusal. Failure checks compare the complete graph before/after; a failed certification
restores the old current marker, history/provenance edges, work state and receipts. The same plan
can then succeed. Competing workers produce one `apply` and one `stale_work` with one receipt.

This remains an isolated experiment: **nondefault namespaces, count targets, diagnostic string
temporals, present folds, creation/supersession only**, with the existing 32-source-record and
30-second preparation bounds plus a 32-prior-View bound. Those are proof limits, not product limits
or engine scan/memory bounds. Same-value refresh and retirement are explicitly refused before
mutation. Native temporal ordering/types, complete historical-metadata drift detection on replay,
process/crash/ambiguous-response recovery, scale, native constraint/bootstrap ownership and the
remaining callback writers are still unqualified. Captured statement bodies are trusted experiment
code, not a serialized job API. The remaining ten compatibility failures are unchanged; **P1 is
not passed and P2 runtime wiring has not started**.

Final verification: **65 focused local tests passed, 1 online test skipped**; default Ruff and
format checks passed for all three changed Python files. The complete disposable Linux probe
reports **109 checks: 99 PASS, 10 FAIL**, including all 16 new atomic real-View cases and orderly
restart. The failed capability names are unchanged from the previous milestone. No full CI or
native Windows qualification is claimed. Artifact graph audit remains unavailable because the
local Menhir graph service is not reachable; document validation is performed separately.

### Atomic production-source snapshot follow-up (2026-11-04)

`PreparedProductionScalar`, `prepare_production` and `commit_production` now implement that source
requirement in the existing prepared-materialization helper. Preparation reads real assertion,
turn, episode, admission-link and namespace-fence state in **one** query. Hydration and the real
fold/hash run from that returned snapshot; there is no separate provenance preflight or copied
`PreparedAssertion` source. The commit re-reads and compares the source components **inside**
the same query that installs the synthetic payload, work certificate and receipt. Replay performs
the same source check before acknowledging the existing result.

The profile remains deliberately narrow: nondefault explicit namespaces, `count` targets, ISO
string timestamps, one finalized/unquarantined grounding turn and one unfinalized episode bridge
with an unambiguous admission link. Its evidence/publication generation must match the sole
namespace fence. Missing, foreign or ambiguous evidence is refused before dispatch. Legacy direct
episode anchors, finalized-episode bridges and other scalar kinds remain unsupported. Pending and
superseded assertions are captured for invalidation but excluded from the fold. The query fetches
one extra row and refuses more than 32 joined source records; it never certifies a truncated set.
This bounds returned records, not the engine's join/scan memory or execution cost.

The snapshot includes raw source-node properties and node/link identities, including evidence
aliases and other grounding links that could change contributor resolution. The first native run
exposed a guard error: comparing a nested map containing null identity fields can return `null`
even when another component differs. A `WHEN inputs<>$inputs` guard therefore missed changes.
The final query explicitly compares each property map and identity, normalizes absent identities,
and treats uncertain property-map comparisons as unequal. Do not replace this with whole-bundle
equality. Live regressions cover the evidence-property and null-to-identity transitions it missed.

The admission-link and quarantine acceptance checks now **PASS** against this source-bound
variant: both return `stale_inputs` without changing any nodes or relationships. Additional checks
cover fence/evidence generations, finalization, authority, pending/superseded eligibility, tenant
changes, input insertion/deletion, parallel admission links, secondary grounding, identity aliases,
duplicate fences, fresh preparation of invalid evidence, overflow, empty/multiple fold inputs,
replay after quarantine, injected failure after writes, and competing publishers with one receipt.
The original property-only candidate is still isolated and is not a production-safe alternative.

**This passes the bounded source-guard sub-gate, not P1.** Outputs still use synthetic `Prepared*`
work/receipt/View storage and a JSON View overwrite. Real View history, provenance edges and
evidence-fence coordination must be published atomically with certification next. The separate
string-temporal history diagnostic remains the only passing real View writer experiment; native
temporals, general value types, all transaction writers and crash/recovery qualification remain open.

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
- Transaction follow-up probe, before the prepared extension: 22 checks, 13 passing and 9 failed gates. The nine
  failures are the prior seven incompatibilities plus the two Menhir materialization refusal
  counterexamples; the publication candidate and orderly-restart checks passed. Exit code remains 1.
- Prepared follow-up offline selection: the prepared-materialization tests, compatibility tests,
  projection lifecycle repository tests and scalar materializer tests: 43 passed, 4 online checks
  skipped, one upstream Pydantic warning. Default Ruff and F811/F821/ASYNC checks passed.
- Final prepared live run: 39 total checks, 30 passing and the same nine failed compatibility
  gates; all 17 prepared-materialization cases passed, including orderly restart afterward.
  The aggregate decision remains BLOCKED because the existing callback/runtime paths are unchanged.
- Production-schema follow-up: 45 offline checks passed, 4 online Neo4j checks skipped; default
  Ruff, scoped F811/F821/ASYNC and formatting checks passed. Live probe: 44 checks, 32 passing,
  12 failed gates (the prior nine plus admission-link fencing, evidence liveness, and native View
  datetime support). All 17 isolated prepared cases and orderly restart passed. The diagnostic
  string-temporal View-history case passed; it does not qualify temporal semantics or P1.
- Atomic source follow-up: 62 local tests passed, 4 online Neo4j checks skipped; default Ruff,
  F811/F821/ASYNC and formatting checks passed. Final live probe: 66 checks, 56 passing and 10
  remaining failed compatibility gates. All 24 source-bound cases, all 17 original prepared cases,
  the competing-publisher checks and orderly restart passed. The former admission-link and evidence
  liveness failures now pass in the source-bound variant; the overall decision remains BLOCKED.
- Document-publication follow-up: 75 local tests passed, 11 online Neo4j checks skipped, one
  upstream Pydantic warning; default Ruff, F811/F821/ASYNC and formatting checks passed. The
  focused selection included compatibility/prepared tests, lifecycle/materializer tests and
  structure-fence/publication reference tests. Live probe: 79 checks, 69 passing, the same 10
  remaining failed gates; all 13 document checks and orderly restart passed. No full CI claimed.
- Full-project follow-up: 82 local tests passed, 11 online checks skipped, one upstream warning;
  default/scoped Ruff and formatting passed. Final live probe: 92 checks, 82 passing and the same
  10 remaining failed compatibility gates. All 13 compound-project cases passed, including zero
  and multiple completion-marker rejection. Orderly restart preserved the complete project graph,
  native temporal values and guard library. The aggregate decision remains BLOCKED; no full CI claimed.

### Queued enrichment and recall milestone (2026-11-05)

The normal `IngestService.ingest_episode` durable queue/worker now reaches actual Graphiti
extraction, persistence, Menhir stamping/retention and `RecallService.recall` on disposable Lite
storage. Model, embedding and reranker outputs are controlled; external HTTP is forbidden by the
test. Production feature flags are unchanged. The test refuses swallowed backend exceptions and
error logs, checks namespace isolation, requires extracted entities/facts, drains the worker and
asserts that only the owned physical graph exists.

Graphiti clones normally select a graph per group. Menhir instead keeps these clones on its owned
graph, with logical `group_id` tenancy, so direct repositories can see and retain extracted nodes.
The existing provider is reused through a private subclass. No adapter service or fork-wide change
is needed. Episode query owners bind canonical UTC clock/deadline parameters, preserve claim-attempt
fences and hydrate claimed event/lease times before handing them to the worker. Neo4j keeps its
existing temporal expressions. Shared reset policy accepts the explicit backend clock.

Required recall variants use native vector cosine, string fact guards, UTC access stamps and
collection-based View receipt validation. Receipt collections include every incoming relationship;
duplicates, missing/wrongly typed evidence, foreign tenants and extra incoming edges fail closed.
Native result provenance keeps joins inside each UNION branch because the native planner cannot
resolve filtered node aliases exported by a correlated UNION; it preserves receipt precedence,
earliest timestamps, first-five receipt selection, distinct turn ids and namespace scoping.

This is a bounded semantic ingestion profile with distinct entities. It does not qualify scalar
View refresh/retirement, correlation merges/vetoes (the existing `=~` predicate is unsupported),
resolved-only timeline queries, erasure, mixed-format import, every maintenance operation, or atomic
callback owners. In particular, `execute_write` still refuses callbacks before invocation. Continue
with the required native query/atomic owners in the existing minimal completion order; do not
activate T5 or open Windows/engine work. No public backend selection or production readiness claim.

Local selection: **359 passed, 5 skipped** across episode lifecycle, heartbeat, reset/query building,
idempotent submission, namespace isolation, retention, recall visibility/provenance/source lanes and
shared Falkor repository/schema contracts. These focused tests cover the changed owners and their
immediate policy callers; the complete repository suite and exact-SHA CI were not run. Live Linux
pipeline/receipt/time/provenance checks are in `tests/infrastructure/test_falkor_pipeline.py`.
The final native selection passed **36 tests**: that file plus `test_falkor.py` and
`test_falkor_schema.py`, including shared runtime/readiness and file restart. Scoped
F811/F821/ASYNC checks and default Ruff on the runtime, shared policy and new tests pass.
Artifact validation retains the same 19 baseline findings, none on the edited reference.

### Native atomic binding refresh milestone (2026-11-05)

`MemoryGraphAdapter.refresh_indexed_binding` now dispatches one native command after the existing
writer admission and before its existing finally/release. The command combines the unchanged
conditional binding update with the shared project-publication guard: bound identity, host, root,
claim generation and graph-issued scan generation. The Neo4j callback retains the same guard and
update. This is an explicit operation mapping, with no callback replay or general query compiler.

Native refresh requires a matching target claim and a positive scan token before dispatch. A
stale guard raises `StaleStructureScan`; a valid guard with an unchanged binding or replaced
fingerprint returns False. Another identity with the same display name is untouched. Admission
still enforces the migration freeze. The real raise/lower fence API is exercised; raising a native
fence binds its UTC instant instead of using Neo4j datetime(). Both registrations are released on success, no-op,
refusal and failure. Inert admission/publication probes are not business publication state.

Live tests change the claim generation, root, host, identity state or scan generation between
admission and publication. All are refused without changing bindings. A valid update followed by
a query-time zero-step range error (dependent on the updated row count) rolls back the binding;
this exercises execution-time rollback rather than parser rejection. Arbitrary `execute_write`
callbacks remain refused. Project and document atomic publication, scalar transitions, erasure
and the other recorded native query gaps remain unqualified. Public enablement stays gated.

Verification covers the changed adapter/writer/guard and neighboring beacon, watcher, identity and
migration contracts. No full CI or complete backend acceptance was run. Commands from the feature
worktree (PowerShell; native runs use the disposable Linux container):

```powershell
.venv/Scripts/python.exe -m pytest -q --tb=short tests/test_beacon_provider.py tests/infrastructure/test_cf257_structure_write_fence.py tests/infrastructure/test_cf257_stale_claim.py tests/infrastructure/test_cf257_detached_write_identity.py tests/test_structure_watcher.py
# 91 passed
.venv/Scripts/python.exe -m pytest -q --tb=short tests/infrastructure/test_falkor_pipeline.py -k requires_guard
# 4 passed, 12 deselected
docker exec -w /work menhir-109-compat python -m pytest --run-online -q --tb=short tests/infrastructure/test_falkor_pipeline.py -k 'binding_refresh or normal_ingest'
# 13 passed, 3 deselected
```

The native selection includes eight live binding cases, the existing queued pipeline and four
pre-dispatch refusal cases. The actual raise/lower-fence case also passed a targeted rerun after
adding its native clock (1 passed), and the 11 existing offline fence tests passed again. Existing Neo4j tests `test_unchanged_fingerprint_refresh_rejects_stale_scan`,
`test_binding_refresh_cannot_update_a_same_name_other_identity` and
`test_late_scan_and_missing_token_cannot_replace_newer_publication` passed (**3 tests**) in
`tests/infrastructure/test_gateb_structure_publication_online.py`, through a loopback relay to the
dedicated disposable Neo4j container. Default Ruff/format checks on the changed tests and scoped
F811/F821/ASYNC checks on all changed Python owners pass. Artifact validation retains the prior
baseline findings; the edited reference and roadmap add none. Graph-backed artifact audit remains
unavailable at its previously failing local endpoint; it was not retried unchanged.

## Upstream evidence

- [FalkorDBLite usage and persistence](https://github.com/FalkorDB/falkordblite): runtime ownership and file API.
- [Published 0.11.0 files](https://pypi.org/project/falkordblite/0.11.0/#files): platform wheel availability.
- [Graphiti fork source at the pinned tag](https://github.com/Archolith/graphiti/blob/v0.31.2.post2/graphiti_core/driver/falkordb_driver.py): session callback execution without rollback.
- [Redis transactions](https://redis.io/docs/latest/develop/using-commands/transactions/): execution errors do not roll back other commands; Redis does not provide transaction rollback.
