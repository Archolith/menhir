# Infrastructure Audit — `src/cth_mcp_memory/infrastructure/`

**Date:** 2026-06-05  
**Auditor:** OpenCode (z-ai/glm-5.1)  
**Scope:** All 33 files under `src/cth_mcp_memory/infrastructure/` + associated test files under `tests/`  
**Categories:** Cypher correctness, Graphiti client compatibility, schema bootstrap, episode lifecycle, thread safety, dead code, naming residue, env var bypasses, query injection, error handling, data integrity, test coverage, naming consistency, architectural drift, operational fragility  

---

## Findings

### I-01 — EmbeddingCache: no thread-safe access to module-level singleton

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **File** | `embedding_cache.py:85-92` |
| **Category** | Thread safety |
| **Description** | `EmbeddingCache` is an `OrderedDict` subclass. `get()` and `set()` perform multiple dict operations (pop, move-to-end, `__setitem__`) that are not atomic. The module-level singleton `_cache` returned by `get_embedding_cache()` is shared across all callers. If the scheduler's enrichment worker and the MCP server's recall path run concurrently (same process, `asyncio` + threads), a concurrent `set()` during a `get()` can corrupt the OrderedDict internal linked list, raising `KeyError` or `RuntimeError` from CPython internals. |
| **Fix** | Wrap `get()` and `set()` in a `threading.Lock`. Alternatively, switch to `functools.lru_cache` or `cachetools.LRUCache` which is thread-safe. The test file `test_embedding_cache.py` only tests single-threaded behavior; add a concurrent stress test similar to `test_thread_safety.py`. |
| **Existing test** | `test_embedding_cache.py` — covers LRU logic and stats but no concurrency tests |

---

### I-02 — `os.getenv()` in `paths.py` and `llama_endpoint.py` bypasses centralized settings

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **File** | `paths.py:12-22`, `llama_endpoint.py:9-24` |
| **Category** | Env var bypass |
| **Description** | `paths.py` reads `WORKSPACE_ROOT`, `SCHEDULER_DIR_NAME`, and `cth_mcp_memory_MCP_TELEMETRY_DB` via `os.getenv()` directly. `llama_endpoint.py` reads `SCHEDULER_URL`, `LLAMA_URL`, and `cth_mcp_memory_MCP_TELEMETRY_DB` the same way. The project has a `settings.py` (via pydantic-settings) that is the canonical config source. These `os.getenv()` calls bypass validation, default coercion, and any future config-layer changes (e.g., `.env` file support, type coercion). `SCHEDULER_URL` in `llama_endpoint.py` has a hard-coded fallback to `http://localhost:3456` which is not in settings. |
| **Fix** | Route all env var reads through `settings.py`. Add `scheduler_url`, `llama_url`, `workspace_root`, `telemetry_db_path` to the settings model. Keep `os.getenv()` only for very early bootstrap (before settings load) — document the exception. |
| **Existing test** | `test_settings.py` — exists but does not verify that these modules use settings |

---

### I-03 — Schema bootstrap: `PHASE_ONE_REQUIRED_INDEXES` naming mismatch with generated index names

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `schema.py:18-27` vs `schema.py:30-62` |
| **Category** | Schema bootstrap |
| **Description** | `PHASE_ONE_REQUIRED_INDEXES` lists names like `"entity_type_idx"`, `"entity_scope_idx"`, `"episodic_type_idx"`, etc. But `_node_index_queries()` generates names like `entity_type_idx` (for Entity label), `episodic_type_idx` (for Episodic label) — these match. However, `PHASE_ONE_REQUIRED_INDEXES` includes `"episode_uuid"` and `"episode_group_id"` and `"episode_content"`, but the generated index queries name them `episodic_..._idx` (with `episodic` prefix). The names `"episode_uuid"`, `"episode_group_id"`, `"episode_content"` do not match any `CREATE INDEX ... IF NOT EXISTS` statement in `_node_index_queries()`. `phase_one_schema_ready()` will therefore never report these indexes as online. |
| **Fix** | Either (a) rename the required index names to match what `_node_index_queries()` actually creates, or (b) add the missing `CREATE INDEX` statements for `episode_uuid`, `episode_group_id`, `episode_content`. Most likely (a) is correct — these are supposed to be `episodic_uuid_idx`, `episodic_group_id_idx`, `episodic_content_idx` but the required list was never updated after the naming convention was applied. |
| **Existing test** | `test_phase_one_schema.py` — verifies query content but does not check index name correspondence |

---

### I-04 — Schema bootstrap: `_SCHEMA_V` bump logic has no re-backfill guard for edges

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `schema.py:84-190` |
| **Category** | Data integrity |
| **Description** | Node defaults queries use `_yawn_schema_v` to decide which nodes need backfill. Edge defaults queries (`_edge_defaults_queries()`) also use `_yawn_schema_v`. But the edge `MERGE` pattern is `MATCH ()-[r:{edge_label}]-() WHERE r._yawn_schema_v IS NULL OR r._yawn_schema_v < {_SCHEMA_V}` — this will match ALL edges on every startup if edges were never stamped (which they wouldn't be by Graphiti-created edges). For a large graph with millions of RELATES_TO edges, this is an O(N) write on every boot. |
| **Fix** | Either (a) add a separate flag/file to track that edge backfill has completed once and skip on subsequent boots, or (b) batch the updates with `LIMIT` and process incrementally. The node version check pattern is fine because nodes get stamped; edges created by Graphiti never get `_yawn_schema_v` set until this backfill runs. |
| **Existing test** | `test_phase_one_schema.py` — tests query generation but not runtime performance impact |

---

### I-05 — `structure_queries.py:_write_edges_batch` uses f-string for relationship type (Cypher injection surface)

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `structure_queries.py:1323-1332` |
| **Category** | Query injection |
| **Description** | `_write_edges_batch` formats the relationship type directly into the Cypher string via `f"... MERGE (a)-[r:{rel_type}]->(b) ..."`. The `rel_type` argument is always a compile-time constant (`"CONTAINS"`, `"DEPENDS_ON"`, `"TESTS"`, `"IMPORTS"`, `"EXPOSES"`, `"CALLS"`, `"DEFINES"`), so this is not exploitable today. However, the comment acknowledges "Neo4j doesn't support parameterized relationship types" — if a future caller passes user input as `rel_type`, it would be a Cypher injection. |
| **Fix** | Add an allowlist validation at the top of `_write_edges_batch`: `ALLOWED_REL_TYPES = {"CONTAINS", "DEPENDS_ON", "TESTS", "IMPORTS", "EXPOSES", "CALLS", "DEFINES"}`; raise `ValueError` if `rel_type` not in set. |
| **Existing test** | `test_structure_queries.py` — exists, may cover this |

---

### I-06 — `todo_repository.py:create_todo` CONCERNS query can be slow on large graphs

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `todo_repository.py:172-184` |
| **Category** | Performance |
| **Description** | The `CONCERNS` edge-creation query does `MATCH (e:Entity) WHERE e.scope = 'PERSISTENT' AND size(e.name) >= 4 AND toLower($lower_content) CONTAINS toLower(e.name)`. This is an O(N) scan of all PERSISTENT entities with name length ≥ 4, with a `CONTAINS` substring match per entity. For a graph with thousands of entities, this is expensive and runs on every `create_todo` call. |
| **Fix** | Add a composite index on `(scope, name)` or pre-filter the candidate entities by extracting keywords from `content` first (similar to how `search_by_query` works with `_query_words()`). Use `UNWIND $keywords AS kw MATCH (e:Entity {scope: 'PERSISTENT'}) WHERE toLower(e.name) = kw` for exact keyword matches. |
| **Existing test** | `test_todo.py` — covers the happy path but not performance at scale |

---

### I-07 — `scheduler_trace.py` module-level `_registered` flag is not reset-safe

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `scheduler_trace.py:23-24` |
| **Category** | Operational fragility |
| **Description** | `_registered = False` is a module-level global. Once `register_scheduler_task_source()` succeeds, `_registered = True` and the function becomes a no-op forever. If the scheduler restarts or changes URL, re-registration is impossible without a process restart. The `asyncio.Lock` guard is correct for concurrency but the permanent flag makes testing and operational recovery harder. |
| **Fix** | Add a `force=False` parameter to `register_scheduler_task_source()`. When `force=True`, reset `_registered = False` before acquiring the lock. Alternatively, store the registered URL and re-register if it changed. |
| **Existing test** | `test_scheduler_trace.py` — exists |

---

### I-08 — `consolidation_queries.py` and `correlation_queries.py` lack unit test coverage

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `consolidation_queries.py`, `correlation_queries.py` |
| **Category** | Test coverage |
| **Description** | Neither `consolidation_queries.py` (entity decay, session promotion, conflict resolution, edge count sync) nor `correlation_queries.py` (RELATES_TO merge, cosine similarity) has a dedicated test file. There is no `test_consolidation_queries.py` or `test_correlation_queries.py` in the test directory. These modules contain complex Cypher with CASE expressions, `coalesce`, `toFloat`, and `cosineSimilarity` that warrant direct testing of generated query strings. |
| **Fix** | Create `test_consolidation_queries.py` and `test_correlation_queries.py` following the pattern of `test_cypher.py` — test that generated queries contain expected clauses, field names, and MATCH/SET patterns. Add integration tests for actual Neo4j execution in the live test suite. |

---

### I-09 — `schema.py` node defaults assigns `randomUuid()` for missing `session_id`

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `schema.py:98,125` |
| **Category** | Data integrity |
| **Description** | The backfill query sets `n.session_id = coalesce(n.session_id, randomUuid())` for Entity and Episodic nodes that have no `session_id`. This means every pre-existing node without a session_id gets a unique random UUID, defeating the purpose of session-scoping. If a recall query filters by `session_id`, these backfilled nodes will never match any real session. The intent was likely to assign a shared sentinel like `"legacy"` or `"unknown"` rather than per-node random values. |
| **Fix** | Change `randomUuid()` to a fixed sentinel like `'legacy'` or `'no-session'`. This preserves session-scoped filtering while still having a non-null value. |
| **Existing test** | `test_phase_one_schema.py` — checks that `session_id` is in the query blob but does not validate the backfill value |

---

### I-10 — `graphiti_patches.py` monkey-patches graphiti-core without version upper bound

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `graphiti_patches.py` |
| **Category** | Graphiti client compatibility |
| **Description** | The patches apply if `graphiti_version >= (0, 28, 0)` with no upper bound. If graphiti-core 0.29+ changes the patched internals (e.g., renames `JsonOutputParser`, changes the prompt structure, removes the `add_episode` kwargs), the monkey-patches will silently fail or corrupt behavior. The version guard is one-sided. |
| **Fix** | Add an upper bound: `graphiti_version >= (0, 28, 0) and graphiti_version < (0, 29, 0)`. Log a warning when patches are skipped due to version mismatch. Add a startup check that validates the patched attributes still exist post-patch. |
| **Existing test** | None directly — patches are applied at import time |

---

### I-11 — `neo4j.py` double-checked locking uses Python `threading.Lock` correctly but `_driver` field is not `volatile`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `neo4j.py` |
| **Category** | Thread safety |
| **Description** | The `_get_driver()` method uses double-checked locking with a `threading.Lock`. The first check `if self._driver is not None` reads `_driver` without holding the lock. In CPython with the GIL, this is safe because reference assignment is atomic. However, if this code ever runs on a free-threaded Python build (PEP 703, Python 3.13+), the read could see a partially constructed object. |
| **Fix** | Document the CPython-GIL assumption. If targeting free-threaded Python, use `threading.local()` or a proper `@property` with lock. |
| **Existing test** | `test_neo4j_repository.py` — has concurrent driver creation test |

---

### I-12 — `telemetry/store.py` module-level `telemetry_store` singleton is created at import time

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `telemetry/store.py:1107` |
| **Category** | Operational fragility |
| **Description** | `telemetry_store = McpTelemetryStore()` at module level means the SQLite DB path is resolved at import time via `default_telemetry_db_path()` → `telemetry_db_path()` → `paths.py` → `os.getenv()`. If environment variables change after import (e.g., in test fixtures or in a multi-configuration deployment), the DB path is stale. Also, tests that import any telemetry-recording module will create the telemetry DB file as a side effect. |
| **Fix** | Use lazy initialization: make `telemetry_store` a module-level property or a `functools.cached_property`-style accessor. In tests, provide a fixture that patches `telemetry_db_path()` to a temp directory. |
| **Existing test** | `test_mcp_telemetry.py`, `test_telemetry_stats.py` — exist but may share the real DB |

---

### I-13 — `temporal_repository.py:create_temporal` does not set `source_confidence`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `temporal_repository.py:54-83` |
| **Category** | Data integrity |
| **Description** | The `CREATE` query for TEMPORAL nodes sets `type`, `scope`, `source`, `freshness`, `edge_count`, `sharpness`, but omits `source_confidence`. The schema bootstrap (`schema.py:96`) defaults `source_confidence` to `0.5` for Entity nodes, but since TEMPORAL nodes are created via direct Cypher (bypassing Graphiti), they'll only get the default if the bootstrap backfill runs. If bootstrap hasn't run yet, `source_confidence` is NULL, which could cause `toFloat(NULL)` issues in scoring queries. |
| **Fix** | Add `source_confidence: 1.0` to the CREATE query (TEMPORAL nodes are user-authored, so confidence should be high). Also add `user_id: 'default'` which is also missing. |
| **Existing test** | No dedicated `test_temporal_repository.py` |

---

### I-14 — `todo_repository.py` TEMPORAL reminder node creation duplicates `TemporalRepository.create_temporal` logic

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `todo_repository.py:100-134` |
| **Category** | Dead code / DRY violation |
| **Description** | When a `due_date` is provided, `create_todo` inline-creates a TEMPORAL Entity node with nearly identical fields to `TemporalRepository.create_temporal()` (same `type: 'TEMPORAL'`, `target_date`, `status: 'open'`, etc.). The inline version omits `source_confidence` and `user_id` (same issue as I-13). It also creates the `HAS_REMINDER` edge in the same query. If the TEMPORAL node schema changes (e.g., adding `source_confidence`), both places must be updated. |
| **Fix** | Refactor `create_todo` to call `TemporalRepository.create_temporal()` and then create the `HAS_REMINDER` edge in a follow-up query. Alternatively, extract a shared `_create_temporal_entity_cypher()` helper. |
| **Existing test** | `test_todo.py` — covers the create path but does not verify TEMPORAL node field parity |

---

### I-15 — `memory_graph_adapter.py` does not export `StructureGraphWriter`, `TemporalRepository`, or `TodoRepository`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `memory_graph_adapter.py`, `__init__.py` |
| **Category** | Architectural drift |
| **Description** | `MemoryGraphAdapter` is a thin facade over `EpisodeRepository`, `ConsolidationRepository`, `MemoryQueryRepository`, and `CorrelationRepository`. But `StructureGraphWriter`, `TemporalRepository`, and `TodoRepository` are separate classes that consumers instantiate directly. This means the adapter layer is incomplete — the `__init__.py` only exports `MemoryGraphAdapter` but not the structural/temporal/todo repos. Consumers must know to import them from their specific modules. |
| **Fix** | Either (a) add these to `MemoryGraphAdapter` as optional delegates (like the episode repo), or (b) add them to `__init__.py` exports for discoverability. Option (b) is lower risk. |
| **Existing test** | `test_memory_graph_adapter_methods.py` — exists |

---

### I-16 — `episode_repository.py` inherits from 3 mixins but does not override `__init__`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `episode_repository.py` |
| **Category** | Naming / architectural |
| **Description** | `EpisodeRepository` inherits from `EpisodeLifecycleRepository`, `EpisodeMaintenanceRepository`, and `EpisodeStampingRepository`. All three expect `self.neo4j` to be set, but `EpisodeRepository` is a dataclass without an explicit `__init__`. The `neo4j` field is set by the dataclass decorator. If any mixin adds an `__init__`, MRO could break. Currently works because mixins don't define `__init__`. |
| **Fix** | Document the contract: "Mixins must not define `__init__`; they rely on `self.neo4j` being set by the concrete class." Consider adding an explicit `__init__` to `EpisodeRepository` that validates `neo4j` is set. |
| **Existing test** | `test_episode_lifecycle.py` — tests use `_make_repo()` which manually sets `repo.neo4j` |

---

### I-17 — `episode_lifecycle.py:claim_pending_episode` uses hardcoded `context_retry_attempts` default of `None`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `episode_lifecycle.py` |
| **Category** | Operational fragility |
| **Description** | `claim_pending_episode()` has `context_retry_attempts: int | None = None`. When `None`, the implementation uses `max(max_attempts, context_retry_attempts)` which falls back to `max_attempts`. The test `test_episode_lifecycle.py` covers this, but the semantic intent is unclear: should `None` mean "use the default from settings" or "same as max_attempts"? The code path silently converts `None` → `max_attempts` without logging. |
| **Fix** | Add a docstring explaining the `None` semantics. Consider reading a default from `settings.py` instead of hardcoding `max_attempts` as the fallback. |
| **Existing test** | `test_episode_lifecycle.py` — covers the None/0/higher paths |

---

### I-18 — `structure_queries.py:link_episode_to_documents` uses `e.id` instead of `e.uuid` for episode match

| Field | Value |
|---|---|
| **Severity** | CRITICAL |
| **File** | `structure_queries.py:485` |
| **Category** | Cypher correctness |
| **Description** | The query `MATCH (e:Entity {id: $episode_id})` uses the `id` property, but all episode nodes use `uuid` as their identifier property (per `data_models.md` and every other query in the codebase). The `id` property in Neo4j is the internal node ID (deprecated in Neo4j 5+), not the application UUID. This query will never match any episode node, silently returning 0 links every time. |
| **Fix** | Change `{id: $episode_id}` to `{uuid: $episode_id}`. Also check `get_linked_documents` at line 505 which uses `e.id IN $uuids` — same bug. |
| **Existing test** | `test_structure_queries.py` — may not test `link_episode_to_documents` directly |

---

### I-19 — `structure_queries.py:query_blast_radius` cross-project CALLS query ignores `file_paths` parameter

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `structure_queries.py:678-688` |
| **Category** | Cypher correctness |
| **Description** | The cross-project refs query in `query_blast_radius` does `MATCH (src:Entity {structure_project: $p, structure_role: 'project'})-[r:CALLS]->(tgt:Entity)` — this always returns ALL cross-project CALLS edges for the project, regardless of which `file_paths` were changed. It should be filtered to only show cross-project refs relevant to the changed files. |
| **Fix** | This is by design (showing all cross-project refs for context), but the result key is `cross_project_refs` which in the blast-radius context implies "affected by these changes". Either rename to `all_cross_project_refs` or filter by files that expose endpoints. |
| **Existing test** | `test_structure_queries.py` — may exist |

---

### I-20 — `observability.py` Langfuse initialization at module level can fail silently

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `observability.py` |
| **Category** | Error handling |
| **Description** | Langfuse client is initialized at module level. If the `LANGFUSE_PUBLIC_KEY` env var is set but the URL is wrong or the service is down, the client creation succeeds but all subsequent `langfuse.trace()` calls will fail silently (Langfuse SDK swallows errors). There's no health check or startup validation. |
| **Fix** | Add an optional startup health check that pings the Langfuse API. Log a warning if the check fails. Consider making Langfuse initialization lazy (on first use) rather than eager. |
| **Existing test** | `test_observability.py` — exists |

---

### I-21 — `logging_config.py` uses `RotatingFileHandler` with hardcoded maxBytes/backupCount

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `logging_config.py` |
| **Category** | Env var bypass / naming residue |
| **Description** | The logging configuration hardcodes `maxBytes=10_000_000` and `backupCount=5`. These are not configurable via environment variables or settings. On a busy server, 50MB total log storage (5 × 10MB) may be insufficient. The log file path is also hardcoded relative to the workspace root rather than using the OS-specific log directory. |
| **Fix** | Move `maxBytes` and `backupCount` to `settings.py`. Use `platformdirs` or `os.environ.get('XDG_STATE_HOME')` for the log directory on Linux. |
| **Existing test** | None dedicated |

---

### I-22 — `project_scanner.py` AST-based parsers have no error recovery for syntax errors

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `project_scanner.py` |
| **Category** | Error handling |
| **Description** | The Python scanner uses `ast.parse()`, JS/TS uses regex, Java uses regex, Go uses regex. If a source file has a syntax error, `ast.parse()` will raise `SyntaxError` and the entire file's symbols are lost. The other parsers (regex-based) are more resilient but less precise. There's no try/except around `ast.parse()`. |
| **Fix** | Wrap `ast.parse()` in a try/except that logs a warning and returns an empty symbol list for that file. This matches the "best-effort" philosophy of the structural scanner. |
| **Existing test** | `test_project_scanner.py` — exists |

---

### I-23 — `pending_actions.py` SQLite work queue has no WAL mode or busy timeout

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `pending_actions.py` |
| **Category** | Operational fragility |
| **Description** | The SQLite-backed work queue uses the default journal mode (DELETE), not WAL. Under concurrent reads/writes (scheduler thread + MCP handler thread), this can cause `database is locked` errors. There is no `PRAGMA busy_timeout` set, so a lock contention will fail immediately rather than retry. |
| **Fix** | Add `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` to the connection initialization, similar to `telemetry/store.py` (which also doesn't set these but should). |
| **Existing test** | None dedicated for `pending_actions.py` |

---

### I-24 — `episode_maintenance.py` stale-episode reset uses `build_reset_or_fail_query` but the `processing_substage_started_at` field is set to `datetime()` on reset

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `episode_maintenance.py` (via `cypher.py:build_reset_or_fail_query`) |
| **Category** | Data integrity |
| **Description** | When an episode is reset from FAILED/ENRICHING back to PENDING, `build_reset_or_fail_query` sets `processing_substage_started_at = datetime()`. But the episode is being reset to PENDING/queued — there is no active substage, so `processing_substage_started_at` should be `null`, not `datetime()`. Setting it to the current time could confuse dashboards that calculate substage duration. |
| **Fix** | Change `n.processing_substage_started_at = datetime()` to `n.processing_substage_started_at = null` in the reset branch of `build_reset_or_fail_query`. |
| **Existing test** | `test_cypher.py:TestBuildResetOrFailQuery` — tests the query template but not the semantic correctness of this field |

---

### I-25 — `memory_queries.py` recall scoring uses `cosineSimilarity` without null-check on `name_embedding`

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `memory_queries.py` (recall queries) |
| **Category** | Cypher correctness |
| **Description** | The recall scoring queries use `cosineSimilarity(n.name_embedding, $query_embedding)` in ORDER BY or WHERE clauses. If `name_embedding` is NULL (e.g., for nodes created by direct Cypher that bypass Graphiti's embedding step), `cosineSimilarity` returns NULL, which sorts as 0 in DESC order. This silently degrades recall quality — nodes without embeddings appear at the bottom but are still returned. The `embedding_dimensions.py` health check detects this but is only run at startup. |
| **Fix** | Add `WHERE n.name_embedding IS NOT NULL` to recall queries, or use `coalesce(cosineSimilarity(...), 0.0)` explicitly and log a count of nodes skipped due to missing embeddings. |
| **Existing test** | `test_embedding_dimensions.py` — covers the health check |

---

### I-26 — `graphiti_client.py` `add_episode` retry loop does not distinguish retryable from non-retryable errors

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `graphiti_client.py` |
| **Category** | Error handling |
| **Description** | The `add_episode` method retries on `(httpx.HTTPError, OSError, asyncio.TimeoutError)` with a max of 3 attempts. But `httpx.HTTPStatusError` with a 4xx status (e.g., 400 Bad Request from Graphiti) is also an `httpx.HTTPError` subclass. Retrying a 400 error is pointless and wastes time. The circuit breaker's `should_trip_circuit()` correctly classifies 4xx as non-trip, but the retry loop in `add_episode` does not use this classification. |
| **Fix** | After catching `httpx.HTTPStatusError`, check `response.status_code`. If 4xx, raise immediately without retry. If 5xx/429/timeout, retry with backoff. |
| **Existing test** | `test_graphiti_client.py` — exists |

---

### I-27 — `embedding_dimensions.py` health check query assumes all nodes have `name_embedding`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `embedding_dimensions.py` |
| **Category** | Cypher correctness |
| **Description** | The health check queries `MATCH (n) WHERE n.name_embedding IS NOT NULL RETURN size(n.name_embedding) AS dim, labels(n) AS lbl`. This is correct — it only checks nodes that have embeddings. But the inference query for the embedding dimension does `MATCH (n:Entity) WHERE n.name_embedding IS NOT NULL RETURN size(n.name_embedding) AS dim LIMIT 1`. If the first Entity node has a different dimension than later nodes (e.g., due to a model change), the inferred dimension will be wrong. |
| **Fix** | Add a `GROUP BY dim` or `RETURN DISTINCT size(n.name_embedding) AS dim` and log a warning if multiple dimensions are found. |
| **Existing test** | `test_embedding_dimensions.py` — exists |

---

### I-28 — `structure_queries.py:_merge_entity` ON MATCH does not update `structure_role` consistently

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `structure_queries.py:1186` |
| **Category** | Data integrity |
| **Description** | The `_merge_entity` ON MATCH SET includes `n.structure_role = $role`, which means a directory entity that gets re-scanned as a file (unlikely but possible if a path changes type) will have its role overwritten. However, `_merge_entities_batch` (line 1235) also sets `n.structure_role = row.structure_role` on match. The `_merge_entity` single-entity method also sets `n.content = $content` on match, but `_merge_entities_batch` also does. These are consistent but the separate code paths mean any divergence would be a bug. |
| **Fix** | Consider unifying `_merge_entity` and `_merge_entities_batch` into a single code path (batch can handle single items). This reduces the risk of the two paths diverging over time. |
| **Existing test** | `test_structure_queries.py` — may exist |

---

### I-29 — No test coverage for `consolidation_queries.py`, `correlation_queries.py`, `episode_stamping.py`, `episode_maintenance.py`

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **File** | Multiple |
| **Category** | Test coverage |
| **Description** | The following infrastructure modules have no dedicated test files: `consolidation_queries.py`, `correlation_queries.py`, `episode_stamping.py`, `episode_maintenance.py`, `pending_actions.py`, `logging_config.py`, `paths.py`. While some of these are exercised indirectly through integration tests or service-level tests, there are no unit tests that validate the Cypher query strings, parameter binding, or edge cases (e.g., empty input lists, null parameters). |
| **Fix** | Create test files following the `test_cypher.py` pattern — test generated query strings for expected clauses and field names. Priority order: (1) `consolidation_queries.py` and `correlation_queries.py` (complex Cypher), (2) `episode_stamping.py` and `episode_maintenance.py` (episode lifecycle), (3) `pending_actions.py` (SQLite), (4) `logging_config.py` and `paths.py` (low risk). |

---

### I-30 — `providers.py` `ChatBackend` protocol is not imported or checked by `graphiti_client.py` or `llm.py`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `providers.py`, `graphiti_client.py`, `llm.py` |
| **Category** | Architectural drift |
| **Description** | `providers.py` defines a `ChatBackend` protocol with `chat(model, messages, ...)` method. `LLMAdapter` in `llm.py` and `GraphitiClient` in `graphiti_client.py` accept a backend but don't type-check against the protocol. If the backend interface changes (e.g., adding a required parameter), there's no static enforcement that all backends conform. |
| **Fix** | Add `backend: ChatBackend` type annotations to `LLMAdapter.__init__` and `GraphitiClient.__init__`. Run `mypy` with `--strict` in CI to catch protocol violations. |
| **Existing test** | `test_provider_backends.py` — exists |

---

### I-31 — `graphiti_helpers.py` JSON extraction regex may miss malformed JSON

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `graphiti_helpers.py` |
| **Category** | Error handling |
| **Description** | The JSON extraction helpers attempt to parse LLM output that may contain JSON wrapped in markdown code blocks or other formatting. The regex pattern may not handle all edge cases (e.g., nested JSON objects, JSON with escaped braces, multi-line JSON). The `graphiti_patches.py` also patches Graphiti's own JSON parser, creating two separate JSON extraction paths. |
| **Fix** | Consolidate JSON extraction into a single robust function. Add more test cases for edge cases: nested objects, trailing commas, BOM characters, etc. |
| **Existing test** | Tested indirectly through `test_graphiti_client.py` |

---

### I-32 — `llm.py` thinking-tag stripping does not handle nested thinking tags

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `llm.py` |
| **Category** | Error handling |
| **Description** | The thinking-tag stripping logic removes `<think>...</think>` blocks from LLM output. If the LLM produces nested thinking tags (e.g., `<think>outer <think>inner</think> outer</think>`), a simple regex or string replacement will not handle this correctly. This is unlikely in practice but could occur with chain-of-thought models. |
| **Fix** | Use a non-greedy regex like `<think>.*?</think>` with `re.DOTALL` flag. The current implementation may already do this — verify and add a test case for nested tags. |
| **Existing test** | `test_llm_compression.py` — exists |

---

### I-33 — `cypher.py` `Cypher` builder does not validate field names against Neo4j reserved words

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `cypher.py` |
| **Category** | Cypher correctness |
| **Description** | The `Cypher` builder accepts arbitrary strings for SET clauses, WHERE conditions, and RETURN fields. It does not validate against Neo4j reserved words (e.g., `MATCH`, `WHERE`, `RETURN`, `SET`). If a caller accidentally passes a reserved word as a property name, the generated Cypher will be syntactically valid but semantically wrong. |
| **Fix** | This is by design (flexible builder), but add a `validate=False` parameter that, when enabled, checks for common mistakes like missing `n.` prefix in SET clauses. |
| **Existing test** | `test_cypher.py` — extensive coverage |

---

## Summary Table

| ID | Severity | Category | File | One-line description |
|---|---|---|---|---|
| I-01 | HIGH | Thread safety | `embedding_cache.py` | Module-level EmbeddingCache singleton lacks thread-safe access |
| I-02 | HIGH | Env var bypass | `paths.py`, `llama_endpoint.py` | `os.getenv()` bypasses centralized `settings.py` |
| I-03 | MEDIUM | Schema bootstrap | `schema.py` | `PHASE_ONE_REQUIRED_INDEXES` names don't match generated index names |
| I-04 | MEDIUM | Data integrity | `schema.py` | Edge backfill runs O(N) on every boot for un-stamped edges |
| I-05 | MEDIUM | Query injection | `structure_queries.py` | f-string for relationship type lacks allowlist validation |
| I-06 | MEDIUM | Performance | `todo_repository.py` | CONCERNS query is O(N) scan of all PERSISTENT entities |
| I-07 | LOW | Operational fragility | `scheduler_trace.py` | `_registered` flag never resets, blocks re-registration |
| I-08 | MEDIUM | Test coverage | `consolidation_queries.py`, `correlation_queries.py` | No dedicated unit tests for complex Cypher queries |
| I-09 | MEDIUM | Data integrity | `schema.py` | `randomUuid()` for missing `session_id` defeats session-scoping |
| I-10 | MEDIUM | Graphiti compat | `graphiti_patches.py` | Version guard has no upper bound; patches may silently break |
| I-11 | LOW | Thread safety | `neo4j.py` | DCL pattern relies on GIL; not free-threaded-safe |
| I-12 | LOW | Operational fragility | `telemetry/store.py` | Singleton created at import; DB path stale if env changes |
| I-13 | LOW | Data integrity | `temporal_repository.py` | `create_temporal` omits `source_confidence` and `user_id` |
| I-14 | MEDIUM | DRY violation | `todo_repository.py` | TEMPORAL node creation duplicates `TemporalRepository` logic |
| I-15 | LOW | Architectural drift | `__init__.py` | Structure/Temporal/Todo repos not exported from package |
| I-16 | LOW | Architectural | `episode_repository.py` | 3-mixin inheritance relies on implicit `self.neo4j` contract |
| I-17 | LOW | Operational | `episode_lifecycle.py` | `context_retry_attempts=None` semantics undocumented |
| I-18 | CRITICAL | Cypher correctness | `structure_queries.py` | `link_episode_to_documents` uses `e.id` instead of `e.uuid` |
| I-19 | MEDIUM | Cypher correctness | `structure_queries.py` | Blast radius cross-project refs ignore changed file filter |
| I-20 | LOW | Error handling | `observability.py` | Langfuse init at module level, no health check |
| I-21 | LOW | Env var bypass | `logging_config.py` | Hardcoded rotation settings, no settings integration |
| I-22 | MEDIUM | Error handling | `project_scanner.py` | `ast.parse()` has no try/except for syntax errors |
| I-23 | MEDIUM | Operational fragility | `pending_actions.py` | SQLite no WAL mode or busy timeout |
| I-24 | LOW | Data integrity | `cypher.py` | `processing_substage_started_at = datetime()` on reset is wrong |
| I-25 | MEDIUM | Cypher correctness | `memory_queries.py` | `cosineSimilarity` on NULL embedding returns NULL not 0 |
| I-26 | MEDIUM | Error handling | `graphiti_client.py` | `add_episode` retries 4xx errors pointlessly |
| I-27 | LOW | Cypher correctness | `embedding_dimensions.py` | Dimension inference from single node may miss dimension conflicts |
| I-28 | LOW | Data integrity | `structure_queries.py` | Two separate MERGE paths risk divergence |
| I-29 | HIGH | Test coverage | Multiple | 7 infrastructure modules have no dedicated unit tests |
| I-30 | LOW | Architectural drift | `providers.py` | `ChatBackend` protocol not enforced at type level |
| I-31 | LOW | Error handling | `graphiti_helpers.py` | JSON extraction regex may miss edge cases |
| I-32 | LOW | Error handling | `llm.py` | Thinking-tag stripping may not handle nested tags |
| I-33 | LOW | Cypher correctness | `cypher.py` | Builder does not validate against reserved words |

---

## Priority Remediation Order

1. **I-18 (CRITICAL)** — Fix `e.id` → `e.uuid` in `link_episode_to_documents` and `get_linked_documents`. This is a live bug that silently breaks document linking.

2. **I-01 (HIGH)** — Add `threading.Lock` to `EmbeddingCache`. Risk of data corruption under concurrent access.

3. **I-02 (HIGH)** — Route env vars through `settings.py`. Prevents future config drift and enables proper validation.

4. **I-29 (HIGH)** — Add unit tests for the 7 untested infrastructure modules. This is the biggest risk multiplier — undetected bugs in untested code.

5. **I-03 (MEDIUM)** — Fix index name mismatch. `phase_one_schema_ready()` will never report ready if these required indexes can't be found.

6. **I-09 (MEDIUM)** — Change `randomUuid()` to sentinel for missing `session_id`. Affects session-scoped recall.

7. **I-14 (MEDIUM)** — DRY up TEMPORAL node creation. Prevents schema drift between two code paths.

8. **I-10 (MEDIUM)** — Add version upper bound to graphiti patches. Prevents silent breakage on library upgrades.

9. **I-26 (MEDIUM)** — Fix `add_episode` retry to skip 4xx. Wastes time and can mask real errors.

10. **I-22 (MEDIUM)** — Add try/except around `ast.parse()`. Prevents scanner crashes on syntax errors.

11. **I-23 (MEDIUM)** — Add WAL mode and busy timeout to `pending_actions.py` SQLite. Prevents lock errors.

Remaining MEDIUM and LOW items can be addressed in subsequent sprints.
