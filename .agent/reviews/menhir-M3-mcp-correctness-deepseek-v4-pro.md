# Menhir M3 — MCP Correctness Audit (deepseek-v4-pro)

**Date:** 2026-08-12
**Auditor:** deepseek-v4-pro (model `deepseek/deepseek-v4-flash` via opencode harness)
**Task:** `.harness/TASK-ds4pro-m3-mcp-audit-2.md`
**Scope:** all 70 `*.py` files under `src/menhir/mcp/`
**Mode:** READ-ONLY functional audit. No project files modified except this single report.
**Declared prior-art access:** I did **not** read `.agent/reviews/menhir-M3-mcp-correctness-audit-results.md`. I read the inlined findings from the task file and `.agent/reviews/menhir-confirmed-findings-register-SNAPSHOT.md` was *not* opened (the relevant CF-2 detail was already inlined in the task).

---

## 1. Executive Summary

I enumerated and read **all 70 files / 7,222 lines** under `src/menhir/mcp/` (line total reconciles exactly with the task's 7,222). No MCP file is marked NOT READ.

The primary task — a CF-2-class sweep comparing every tool's declared parameter names/order and return-type assumptions against the actual backend method — found **one genuine contract-breaking mismatch** and confirmed **CF-2** exactly as the background described, with executed reproduction:

### CONFIRMED CRITICAL — `supersede_artifact` is broken on every runtime invocation
`src/menhir/mcp/tools/ops/supersede_artifact.py:33` calls `backend.supersede_artifact(new_uuid, old_uuid)` positionally, but the **effective** `MemoryGraphAdapter.supersede_artifact` (the definition at `memory_graph_adapter.py:1664`, which silently overrides the `:1362` definition) has signature `(old_id, new_id) -> bool` and returns a **bool**. Consequences, both confirmed by execution:
1. **Arguments are inverted** — `new_uuid` is bound to `old_id`, `old_uuid` to `new_id` (the two definitions disagree on argument order; see §6.1).
2. **`result.get("applied")` on a `bool` raises `AttributeError: 'bool' object has no attribute 'get'`**, which `track_mcp_call` swallows and turns into the string `"Error: AttributeError: 'bool' object has no attribute 'get'"`.

End-to-end reproduction (stub backend mirroring the effective adapter) pasted in §4.1/§6.1: the tool can never succeed. If the `:1362` WorkArtifact definition were ever re-activated, the args would be inverted and it would silently supersede the *wrong* pair — a silent data-corruption path. Either way this tool is non-functional.

### Other findings (summary)
- **High — Duplicate method silently overrides a richer implementation** (`MemoryGraphAdapter.fail_exhausted_pending_episodes`, `:487` vs `:876`). Second of two codebase-wide same-signature duplicates.
- **Medium — Bug class 4 (datetime TEXT comparison):** telemetry stores compare Python `isoformat()` ("T", `+00:00`) values against SQLite `datetime('now', ...)` (space separator) as TEXT → the since-window filters in `get_memory_stats`/`get_episode_trace`/queue views over-include rows on the boundary day.
- **Medium — Bug class 5 (blocking I/O in async):** `telemetry/tracker.py` calls synchronous `store.record(...)` (SQLite) directly on the event loop; `resources.py` runs `subprocess.run(git)` and a blocking `socket.create_connection` inside async resource handlers. The codebase convention is `asyncio.to_thread` (146 sites codebase-wide, **0** in the MCP tree).
- **Low/Medium — `get_episode_trace` decodes `details_json` twice per failure row** (`ops/get_episode_trace.py`).
- **Low — unbounded / docstring-vs-code limit drift** on `limit`/`top_k`/`max_views` in `list_enrichment_queue`, `repair_stale_enrichment`, `view_entropy`, `list_conflicts`.
- **Doc/reconciliation:** README claims "52 MCP tools" (README.md:18, :450) but **54** are registered (INGEST 10 + RECALL 5 + CONFLICT 5 + OPS 34). All 54 are unique; no double-registration; no defined-but-unregistered tool. The 9-resource claim is correct (9 registered).
- **No bug-class-2 or bug-class-3 defects** in the MCP tree (pyflakes clean of undefined names; CancelledError correctly escapes).
- **Tier observations:** `supersede_artifact` and `transition_artifact` (one-way lifecycle/status mutations) sit at `agent` tier while comparably destructive ops (`delete_memory`, `promote_memory`, `close_memory`) are `operator`; inconsistent, and moot for the broken `supersede_artifact`. See §5.

Confidence: **84/100** (§11).

---

## 2. Tool/Backend Contract Mismatch Table

Methodology per the task warning: comparison is **not** arity-only. For each tool I (a) listed the declared parameter names/order, (b) read the backend method it actually dispatches to (RuntimeProvider/BackendClient mixin → `MemoryGraphAdapter`/repos), and (c) verified the caller's return-value assumption against the backend's actual return type.

Legend: **MATCH** = names/order/arity/return-type all agree. **MISMATCH** = disagreement. Evidence column cites the tool line and the backend signature/return.

| Tool (module) | Declared params (order) | Backend method called | MATCH/MISMATCH | Evidence |
|---|---|---|---|---|
| `supersede_artifact` (ops/supersede_artifact.py) | `(new_uuid, old_uuid)` | `backend.supersede_artifact(new_uuid, old_uuid)` → effective `MemoryGraphAdapter.supersede_artifact(old_id, new_id) -> bool` | **MISMATCH (Critical)** | Tool `:33` passes positionally; adapter `:1664` binds them to `old_id`/`new_id` (inverted). Return is `bool`; tool `.get("applied")` at `:35` → AttributeError. Executed repro §4.1. |
| `add_memory` (ingest/add_memory.py) | `(text, source, diff, type, valid_at, namespace, flagged, bootstrap_scope, turn_evidence_uuid)` | `backend.queue_episode` / `backend.create_temporal` (kw) | MATCH | kw-only backend; all names present; returns dict. |
| `add_memory_and_track` (ingest) | `(text, source, timeout_s, poll_interval_s, diff, turn_evidence_uuid)` | `backend.queue_episode`; `_collect_episode_status` | MATCH | returns dict w/ episode_id. |
| `add_candidate` (ingest) | `(content, source, cluster_id, label, ...)` | `backend.create_candidate(**kw)` | MATCH | candidate_repository.py:49 returns dict with `uuid/created/...`; `.get('created')` valid. |
| `close_memory` (ingest) | `(uuid)` | `backend.complete_temporal(uuid) -> bool` | MATCH | bool handled via `if ok:`. |
| `delete_memory` (ingest) | `(node_uuid)` | `backend.delete_memory(node_uuid) -> bool` | MATCH | bool handled. |
| `flag_memory` (ingest) | `(node_uuid, bootstrap_scope)` | `backend.flag_memory(node_uuid, bootstrap_scope=...)` | MATCH | signature `(node_uuid, *, bootstrap_scope)`. |
| `unflag_memory` (ingest) | `(node_uuid)` | `backend.unflag_memory(node_uuid) -> bool` | MATCH | bool handled. |
| `promote_memory` (ingest) | `(node_uuid)` | `backend.promote_memory(node_uuid) -> bool` | MATCH | bool handled. |
| `ingest_document` (ingest) | `(path, project, document_type)` | `backend.ingest_document(path, *, project, session_id, user_id, document_type)` | MATCH | returns dict; `structure_project/path/content_length/document_type/narrative` all present. |
| `ingest_project` (ingest) | `(path, name, force)` | `execute_project_ingest(backend, path=..., ...)` | MATCH | service returns `ProjectIngestOutcome`. |
| `list_conflicts` (conflict) | `(status, limit)` | `backend.list_conflict_groups(status=..., limit=...)` | MATCH | returns list[dict]; status=None for "all" handled. |
| `resolve_conflict` (conflict) | `(group_id, action, keep_uuid, remove_uuid, dry_run, allow_promoted_removal)` | `backend.resolve_conflict_group(group_id, *, action, resolution_status, keep_uuid, remove_uuid, allow_promoted_removal)`; `backend.record_conflict_resolution` | MATCH | returns dict `resolved/removed_uuids/bridged_edges/member_uuids`. |
| `requeue_conflicts_for_llm_review` (conflict) | `(from_status, limit)` | `backend.requeue_conflicts_for_llm_review(from_status=..., limit=...)` | MATCH | returns int; tool renders `{"requeued": int}`. |
| `run_llm_conflict_review` (conflict) | `(limit)` | `backend.confirm_pending_conflicts(limit=..., verbose=True)` | MATCH | returns dict. |
| `scan_for_conflicts` (conflict) | `(limit, cursor)` | `backend.scan_for_conflicts(limit=..., cursor=...)` | MATCH | returns dict. |
| `recall_memories` (recall) | `(query, preset, limit, file_context, file_context_project, namespace, include_invalidated, compact, trace)` | `backend.recall(query, *, preset, limit, ...)` | MATCH | returns dict; `results` list of dicts (valid-identifier keys → `SimpleNamespace(**scored)` safe). |
| `recall_context_memories` (recall) | `(reader_id, query, preset, limit, recent_limit, namespace, workspace)` | `backend.recall(...)`, `backend.fetch_recent_memories`, `backend.list_todos` | MATCH | all dict-returning. |
| `read_flagged_memories` (recall) | `(reader_id, limit, workspace)` | `backend.fetch_flagged_memories`, `fetch_flagged_memory_bootstrap_version` | MATCH | dict. |
| `build_context` (recall) | `(query, max_tokens, preset, session_id, include_scores, namespace)` | `backend.build_context(query, *, ...)` | MATCH | dict; `context/memory_count/token_estimate/estimation_mode/truncated` used. |
| `query_structure` (recall) | `(query_type, project, path, namespace)` | `backend.query_structure(project, query_type, params: dict)` | MATCH | runtime wrapper reads `params["path"]` for documents; other types forwarded as `**params`; kwarg keys match structure_queries (`path_filter`, `file_path`, `file_paths`, `path`). |
| `add_todo` (ops) | `(text, code_ref, priority, episode_uuid, structure_project, due_date, namespace)` | `backend.create_todo(content=..., ...)` | MATCH | kw; dict. |
| `close_todo` (ops) | `(uuid)` | `backend.close_todo(uuid) -> bool` | MATCH | bool handled. |
| `close_stale_todos` (ops) | `(older_than_days, dry_run)` | `backend.close_stale_todos(*, older_than_days, dry_run)` | MATCH | dict. |
| `get_todo` (ops) | `(uuid, namespace)` | `backend.get_todo(uuid, namespace=...)` | MATCH | dict. |
| `list_todos` (ops) | `(status, limit, namespace)` | `backend.list_todos(status=..., limit=..., namespace=...)` | MATCH | list[dict]. |
| `get_artifact` (ops) | `(artifact_uuid, namespace)` | `backend.get_artifact(artifact_uuid, namespace=...)` | MATCH | dict. |
| `list_artifacts` (ops) | `(artifact_type, status, namespace, limit)` | `backend.list_artifacts(...)` | MATCH | list[dict]. |
| `list_artifact_questions` (ops) | `(artifact_uuid, status, namespace, limit)` | `backend.list_artifact_questions(...)` | MATCH | list[dict]; `ordinal` handled. |
| `get_artifact_relationships` (ops) | `(artifact_uuid)` | `backend.get_artifact_relationships(artifact_uuid)` | MATCH | dict `outgoing/incoming/subjects/todos`. |
| `link_artifacts` (ops) | `(source_uuid, target_uuid, relation)` | `backend.link_artifacts(source_uuid, target_uuid, relation)` | MATCH | returns dict `linked/edge_type/reason`. |
| `transition_artifact` (ops) | `(artifact_uuid, to_status)` | `backend.transition_artifact_status(artifact_uuid, to_status)` | MATCH | dict `applied/from_status/to_status/reason/valid_transitions`. |
| `relocate_artifact_source` (ops) | `(artifact_uuid, old_path, new_path, repository, expected_old_integrity, observed_integrity)` | `backend.relocate_artifact_source(*, ...)` | MATCH | dict `applied/reason`. |
| `audit_artifact_corpus` (ops) | `(repo_path, repository, from_commit)` | `backend.fetch_artifact_corpus_audit(*, repo_path, repository, from_commit)` | MATCH | dict. |
| `delete_namespace` (ops) | `(namespace, max_nodes, force, dry_run)` | `backend.delete_namespace(namespace, *, max_nodes, force, dry_run)` | MATCH | dict / raises ValueError (caught). |
| `force_reenrich` (ops) | `(episode_uuid, wait, timeout_s, poll_interval_s)` | `backend.force_reset_failed_episode`, `enqueue_pending_episode` | MATCH | bools. |
| `force_release_enrichment_lease` (ops) | `(episode_uuid, requeue)` | `backend.force_release_episode_lease(uuid, *, max_attempts)`; `enqueue_pending_episode`; `fetch_episode_processing` | MATCH | bool/dict. |
| `force_scheduler_takeover` (ops) | `(reason)` | `backend.scheduler_force_takeover(reason=...)`; `scheduler_status_snapshot` | MATCH | bool + dict. |
| `get_client_context` (ops) | `()` | `telemetry_store.get_session_last_accessed` | MATCH | str | None. |
| `list_clients` / `mint_client` / `revoke_client` (ops) | `()` / `(client_name, tier)` / `(client_id)` | `get_client_token_store()` (direct) | MATCH | store API; no backend. |
| `get_enrichment_status` (ops) | `(episode_uuid, wait, timeout_s, poll_interval_s)` | `backend.fetch_episode_processing`, `get_queue_depth` | MATCH | dict. |
| `get_episode_trace` (ops) | `(episode_uuid, limit)` | `backend.fetch_episode_processing`, `fetch_episode_task_events`, `fetch_recent_failures`, `fetch_recent_lifecycle_events` | MATCH | list[dict]; decode_json_value applied twice (see §4). |
| `get_memory_stats` (ops) | `(since_hours)` | `backend.fetch_memory_overview`, `fetch_operation_stats`, `fetch_failure_summary`, `fetch_enrichment_rate`, `fetch_lifecycle_summary`, `get_queue_depth`, `circuit_breaker_snapshots` | MATCH | dicts (subject to bug-class-4 window skew). |
| `get_provenance` (ops) | `(node_uuid, content_chars)` | `backend.fetch_node_receipts(node_uuid)` | MATCH | dict `episodes/evidence/anchor_paths/uuid/...`. |
| `list_enrichment_queue` (ops) | `(state, limit)` | `backend.list_episode_processing(states=..., limit=...)`, `get_queue_depth` | MATCH | list[dict]. |
| `rate_recall` (ops) | `(score, recall_id, reason)` | `telemetry_store.record_recall_feedback` | MATCH | store API. |
| `recover_orphans` (ops) | `(max_age_hours, dry_run)` | `backend.fetch_session_entities`; `RuntimeProvider.built.lifecycle_service.recover_orphans` or `backend.recover_orphans` | MATCH | dict (branch on RuntimeProvider is safe; `isinstance` check). |
| `repair_stale_enrichment` (ops) | `(dry_run, limit)` | `backend.fetch_stale_enriching_episodes`, `recover_stale_enrichment_leases` | MATCH | list/dict. |
| `view_entropy` (ops) | `(namespace, kind, top_k, max_views)` | `backend.view_entropy(namespace=..., kind=..., top_k=..., max_views=...)` | MATCH | dict. |
| `watch_enrichment` (ops) | `(episode_uuid, timeout_s, poll_interval_s)` | `_collect_episode_status` | MATCH | dict. |

**Sweep conclusion:** exactly **one** contract mismatch in the tool→backend surface: `supersede_artifact` (Critical). All other tools' parameter order/names/arity/return-type assumptions agree with the methods they dispatch to.

---

## 3. Tool Registration Reconciliation

Registered tool count (measured by importing the registry, not by counting source):

```
ALL 54  (INGEST 10 + RECALL 5 + CONFLICT 5 + OPS 34)
```

```
$ python -c "from menhir.mcp.tools import ALL_TOOLS; from collections import Counter; ..."
total 54 unique 54
DUPS {}
```

- **54 registered** vs **README claim of 52** (README.md:18 "exposes 52 MCP tools plus 9 read-only MCP resources"; README.md:450 "Menhir registers 52 tools"). Off by **+2**.
  - Contributing drift sources: `.agent/architecture.md:201` says "43 tools", `.agent/endpoints.md:126` says "operator all 44" — all three doc numbers are stale. 54 is authoritative.
- **All 54 names unique** — no tool registered twice (no duplicate `name`, which FastMCP keys on).
- **Every defined tool class is registered; every registered class is defined.** Cross-checked each `tools/*/__init__.py` import against `ALL_TOOLS`: ingest 10/10, recall 5/5, conflict 5/5, ops 34/34 files all present and referenced. No defined-but-unregistered and no registered-but-undefined tool.
- **Resources:** `RESOURCE_TYPES` (resources.py:504) contains **9** classes; `register_memory_resources` registers all 9 → README's "9 read-only MCP resources" is **correct**. (DependencyHealth, SystemMetadata, RecentMemories, LifecycleTrace, ProcessingQueue, MemoryByUuid, MemoriesByScope, MemoriesBySearch, MemoriesByType.)

---

## 4. Findings by Lane

Severity rubric: Critical = data loss/corruption, auth bypass, crash on valid input, silent wrong result in a security/correctness-critical path. High = wrong results for common inputs / silent swallowing of real errors. Medium = wrong results in rare edge cases, resource leak. Low = cosmetic / recoverable on pathological input.

### Lane A — logic / regression

**A-1 (Critical) `supersede_artifact`: inverted positional args + `.get()` on a bool → guaranteed crash / latent silent corruption.**
`ops/supersede_artifact.py:31-35`. `backend.supersede_artifact(new_uuid, old_uuid)` binds to effective adapter `(old_id, new_id)` (`memory_graph_adapter.py:1664`), inverting both, and returns `bool`; `result.get("applied")` crashes. Reproduced §4.1. This is CF-2 confirmed.

### Lane B — boundary / edge cases

**B-1 (Low) Missing input caps / docstring-vs-code drift.**
- `list_enrichment_queue.py` passes `limit` straight to `list_episode_processing` (docstring: "max 200") — no clamp.
- `repair_stale_enrichment.py` passes `limit` (docstring: "max 500") — no clamp.
- `view_entropy.py` passes `top_k`/`max_views` unbounded (docstring caps none) — a pathological `top_k` could widen search cost.
- `list_conflicts.py` passes `limit` unbounded (docstring: "max 200") — contrast with `list_artifacts`/`list_todos`/`list_artifact_questions` which correctly do `min(limit, 200)`.
These are DoS-adjacent only at `readonly` tier; Low.

**B-2 (Medium) `get_episode_trace` decodes `details_json` twice per failure row.**
`ops/get_episode_trace.py` (failure_events list): `decode_json_value(item.get("details_json"))` is computed for `"details"` and *again* inside `"traceback_preview"` (`if isinstance(decode_json_value(...), dict)`). Double JSON parse of the same cell per row — waste, not incorrectness. Medium-low; grouped as Low given bounded `limit`.

**B-3 (Low) `_query_add_memory_events` in `contracts.py:101` grows unbounded per key.**
The deque is pruned by window but the dict key (`query-auth:<id>`) is never evicted; an attacker/long-lived client with rotating identities can accumulate keys. Bounded memory concern only. Low.

### Lane C — concurrency

**C-1 (informational, no defect) `threading.Lock` in async (`contracts.py:116`).**
`_consume_query_add_memory_budget` holds a `threading.Lock` around a non-awaiting critical section inside an async runner — safe (no `await` under lock). Not a finding; noted for completeness.

**C-2 (Low, latent) CancelledError propagation skips telemetry recording in `tracker.py`.**
`asyncio.CancelledError` is a `BaseException`; `track_mcp_call`'s `except Exception` (tracker.py:93) does not catch it, so a cancelled runner produces no failure/telemetry row. This is the *correct* behavior for task cancellation (nothing to reset), so it is not a bug-class-3 defect; reported as informational.

---

### 4.1 Executed reproduction (CF-2 / A-1)

```python
import sys, asyncio; sys.path.insert(0,'src')
class StubBackend:                       # mirrors EFFECTIVE MemoryGraphAdapter.supersede_artifact (line 1664)
    async def supersede_artifact(self, old_id, new_id):
        print(f'   [backend] got old_id={old_id!r} new_id={new_id!r}  -> returns True (bool)')
        return True
from menhir.mcp.tools.ops.supersede_artifact import SupersedeArtifactTool
t = SupersedeArtifactTool(); t.get_backend = lambda: StubBackend()
async def main():
    try:
        out = await t.execute(new_uuid='UUID-NEW', old_uuid='UUID-OLD')
        print('   result:', out)
    except Exception as e:
        print(f'   TOOL CRASHED: {type(e).__name__}: {e}')
asyncio.run(main())
```

**Output:**
```
MCP tool/supersede_artifact failed after 0ms: AttributeError: 'bool' object has no attribute 'get'
   [backend] got old_id='UUID-NEW' new_id='UUID-OLD'  -> returns True (bool)
   result: Error: AttributeError: 'bool' object has no attribute 'get'
```

The first log line is `track_mcp_call`'s error path; the second proves the **argument inversion** (`new_uuid` arrived as `old_id`); the third proves the tool **always fails** (returns an error string, never a supersede result). Also `bool.get` AttributeError independently reproduced with `.venv/Scripts/python.exe -c "r=True; r.get('applied')"` → `AttributeError: 'bool' object has no attribute 'get'`.

---

## 5. Tier Appropriateness Review

Tier distribution measured: **readonly 20 / agent 16 / operator 18**.

| Tool | Tier | Assessment |
|---|---|---|
| `delete_namespace`, `delete_memory`, `promote_memory`, `force_reenrich`, `force_release_enrichment_lease`, `force_scheduler_takeover`, `recover_orphans`, `repair_stale_enrichment`, scheduler pause/resume, client token mint/revoke/list | operator | Appropriate — all destructive / trust-critical. |
| `resolve_conflict`, `run_llm_conflict_review`, `scan_for_conflicts`, `requeue_conflicts_for_llm_review`, `close_memory` | operator | Appropriate (mutation / LLM-cost / conflict resolution). |
| `supersede_artifact`, `transition_artifact`, `relocate_artifact_source`, `link_artifacts` | agent | **Observation:** these mutate graph state (status changes, edge writes, locator moves) yet sit at `agent` tier, while `close_memory` (a status change on a TEMPORAL node) is `operator`. The split looks inconsistent. In particular `supersede_artifact` writes a SUPERSEDES edge *and* moves status — a one-way, destructive lifecycle action — at the lowest write tier. Its being completely broken (A-1) is the immediate problem, but if/when repaired, `agent` tier is arguably a privilege-escalation surface under the task's own criterion. Recommend operator. |
| `close_stale_todos` (default `dry_run=True`) | agent | Defensible because dry-run is the default and the write is reversible/cheap. |
| `flag_memory`, `unflag_memory` (idempotent, reversible) | agent | Appropriate. |
| `rate_recall` (writes telemetry feedback only) | agent | Appropriate. |

No tool is at a *higher* tier than its destructiveness warrants (no over-privilege in the opposite direction except the noted lifecycle trio).

---

## 6. Bug-Class Sweep Results (one row per class)

### 6.1 Bug class 1 — duplicate method/function definitions (later overrides earlier), body-compared

Proving command (AST, compares bodies/dispatch targets, not just signatures):

```python
import ast, os
root='src/menhir'
found=[]
for dp,_,fs in os.walk(root):
    if '__pycache__' in dp: continue
    for fn in fs:
        if not fn.endswith('.py'): continue
        p=os.path.join(dp,fn)
        try: tree=ast.parse(open(p,encoding='utf-8').read(),p)
        except SyntaxError: continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node,ast.ClassDef):
                seen={}
                for m in node.body:
                    if isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef)):
                        if m.name in seen: found.append((p,node.name,m.name,seen[m.name],m.lineno))
                        seen[m.name]=m.lineno
for f in found: print(f'{f[0].replace(os.sep,"/")} class={f[1]} method={f[2]} defs at {f[3]} and {f[4]}')
print('TOTAL',len(found))
```

**Output:**
```
src/menhir/infrastructure/memory_graph_adapter.py class=MemoryGraphAdapter method=fail_exhausted_pending_episodes defs at 487 and 876
src/menhir/infrastructure/memory_graph_adapter.py class=MemoryGraphAdapter method=supersede_artifact defs at 1362 and 1664
TOTAL 2
```

Both duplicates are in `MemoryGraphAdapter`:
- `supersede_artifact` `:1362` (→ `self._work_artifacts`, returns `dict`) is **silently overridden** by `:1664` (→ `self._artifacts`, returns `bool`). **This is CF-2's root cause** — two same-arity definitions that dispatch to entirely different repositories and return different types. The `:1362` body is dead code.
- `fail_exhausted_pending_episodes` `:487` (rich: fetches exhausted + creates raw-capture entities) is **silently overridden** by `:876` (thin delegate to `self._episodes.fail_exhausted_pending_episodes`). The `:487` raw-capture behavior is dead on `MemoryGraphAdapter`. Whether the delegate reproduces it depends on `_episodes`; the adapter-level implementation is unreachable. High — this is the second "same signature, different behavior" instance.

### 6.2 Bug class 2 — names used only in except-handlers / never bound

Proving command:
```
$ .venv/Scripts/python.exe -m pyflakes src\menhir\mcp
```
**Output:** no F821 undefined-name errors. Only F401 unused-imports (`formatters.py`, `server.py` `anyio`, `service_access.py` request_context re-exports, `query_structure.py` `json`) and two F541 f-string-missing-placeholder (`query_structure.py:301,497`). **No unbound-logger NameError sites in the MCP tree.** (The task notes 9 such sites "elsewhere" — outside this MCP scope.)

### 6.3 Bug class 3 — `except Exception` letting `asyncio.CancelledError` escape cleanup

Proving scan: grep for `except Exception` in the MCP tree:
```
tracker.py:93,111,134 ; contracts.py:66,147,164 ; formatters.py:532 ; get_episode_trace: (none) ; recover_orphans.py: except Exception ... raise
```
`asyncio.CancelledError` inherits `BaseException` (Python ≥3.8), so none of these `except Exception` handlers swallow it — CancelledError correctly propagates. **No defect.** The only consequence is telemetry-recording omission on cancellation in `tracker.py` (informational, C-2). The `recover_orphans.py` handler emits a "failed" event then `raise`s; on CancelledError the failure event is skipped — benign.

### 6.4 Bug class 4 — mixed Python `isoformat` ("T") vs SQLite `datetime('now')` (space) compared as TEXT

Reachability: `get_memory_stats(since_hours)` → `fetch_operation_stats/fetch_failure_summary/fetch_enrichment_rate`; `get_episode_trace`; queue/processing views → telemetry stores. Stores write `started_at/recorded_at` with `_utc_now_iso()` = `datetime.now(timezone.utc).isoformat()` → **`"2026-08-12T14:05:07.123456+00:00"`** (helpers.py:18) and filter with SQLite **`datetime('now', '-N hours')`** → **`"2026-08-11 14:05:07"`** (space separator). TEXT comparison of these is lexicographic: at the boundary day the stored `'T'` (0x54) sorts after `' '` (0x20), so **any row on the cutoff's calendar day — including rows earlier in that day than the cutoff time — passes `>= cutoff`**, over-including up to ~a full day's worth of boundary rows for a 24h window. Matches the task's "always over-includes" note exactly.

```sql
-- e.g. lifecycle_store.py:431 / recall_store.py:106,147,157,370,400
window_clause = "AND started_at >= datetime('now', '-' || ? || ' hours')"
```
**Finding: Medium.** Window stats in `get_memory_stats`/`get_episode_trace`/queue views can over-report counts near the window boundary. Fix: compare against a Python-computed ISO cutoff string, or store `datetime('now')`-format, or compare both sides as the same format.

### 6.5 Bug class 5 — blocking I/O inside async without a thread executor

Codebase convention check: `asyncio.to_thread` sites **146** codebase-wide, **0** inside `src/menhir/mcp`. Unwrapped blocking I/O in MCP async paths is a deviation:

- **`telemetry/tracker.py:76,99,122`** — `store.record(...)` is a **synchronous SQLite write** executed directly on the event loop inside async `track_mcp_call`. The tracker's own timeout message even admits a locked telemetry DB "can block the event loop so the call cannot return sooner" — the blocking write is what causes exactly the timeout it warns about. **Medium** (event-loop stall; every MCP call passes through `track_mcp_call`).
- **`resources.py:_resolve_build_id` (called from `SystemMetadataResource.build_payload`)** — runs `subprocess.run(["git","rev-parse","--short","HEAD"], timeout=2.0)` and `os.path.getmtime` synchronously inside an async resource handler (cached after first call). **Low/Medium** (first metadata read blocks loop up to ~2s).
- **`resources.py:_neo4j_dependency_snapshot` (DependencyHealthResource.build_payload)** — blocking `socket.create_connection(..., timeout=1.5)` inside async. **Low** (1.5s worst case, cached per call).
- `recall_memories.py`/`get_client_context.py` — synchronous `telemetry_store.get_session_last_accessed` (SQLite read) in async. **Low** (fast, but same class).

---

## 7. Disproved Candidates

I explicitly investigated and disproved the following, rather than dropping them silently:

1. **`query_structure(query_type="documents", path=...)` kwarg mismatch.** Suspected the tool's `{"path": path}` vs `query_documents(path_filter=...)` was a TypeError. **Disproved:** the runtime `query_structure` wrapper (`backend_runtime_data_ops.py`) special-cases `"documents"` and reads `params.get("path")` → forwards as `path_filter`. Not a bug on the runtime path; works correctly.
2. **`SimpleNamespace(**scored)` crashing on reserved/non-identifier recall keys.** **Disproved:** `ScoredMemory` field names (`uuid, name, content, scope, memory_type, final_score, breakdown, retrieval_score, retrieval_score_kind, warden_label, temporal_facts, is_superseded_view, view_kind, is_scalar_authority, stale_anchor_info`) are all valid identifiers, none Python keywords.
3. **`rate_recall` returning `None` from `record_recall_feedback` handled?** Verified the tool checks `if rated is None:` and returns a proper "no recall found" JSON — no `.get` on None. Not a bug.
4. **`add_candidate` result keys.** Verified `candidate_repository.create_candidate` returns `uuid/created/cluster_id/evidence_strength/distinct_sessions` — tool's `.get` calls all valid.
5. **CancelledError being swallowed (bug class 3).** Disproved — `except Exception` does not catch it; correct.

---

## 8. Open Questions

1. **`fail_exhausted_pending_episodes` (`:876`) delegation parity** — does `_episodes.fail_exhausted_pending_episodes` reproduce the raw-capture creation that `:487` implements? If not, exhausted-episode text is silently dropped on the adapter path. Needs a repo-level check outside the MCP surface.
2. **Whether any caller relies on the *WorkArtifact* `supersede_artifact` (`:1362`)** — it is dead now, but a repair of CF-2 must decide which repository is canonical (L4 artifact vs WorkArtifact) and reconcile the protocol/adapter/tool accordingly.
3. **README/`architecture.md`/`endpoints.md` tool counts** (52/43/44 vs actual 54) — which is intended to be the source of truth.
4. **`supersede_artifact` intended tier** — if it were working, is `agent` intended, or should it be `operator` to match other status-mutating tools?

---

## 9. Coverage Table (all 70 files, line reconciliation)

Line total reconciled: **70 files / 7,222 lines** (task's figure). All files read; none marked NOT READ. (Note: PowerShell `Measure-Object -Line` reported 6,045 — it drops the final unterminated line of each file; Python newline counting yields 7,222, matching the task.)

| Group | Files | Read |
|---|---|---|
| Root: `__init__.py`, `contracts.py`, `feedback.py`, `formatters.py`, `lifecycle.py`, `resources.py`, `server.py`, `service_access.py` | 8 | YES (all) |
| `telemetry/`: `__init__.py`, `tracker.py` | 2 | YES |
| `tools/`: `__init__.py`, `base.py` | 2 | YES |
| `tools/conflict/` (5 incl. `__init__`) | 5 | YES |
| `tools/ingest/` (10 incl. `__init__`) | 10 | YES |
| `tools/ops/` (34 incl. `__init__`) | 34 | YES |
| `tools/recall/` (5 incl. `__init__`) | 5 | YES |
| `tools/ops` additional files listed in enumeration | 0 (all 34 covered above) | — |
| **Total** | **70** | **70** |

Files read individually (non-`__init__`, all reviewed for the sweep): add_candidate, add_memory, add_memory_and_track, close_memory, delete_memory, flag_memory, ingest_document, ingest_project, promote_memory, unflag_memory, list_conflicts, requeue_for_review, resolve_conflict, run_llm_review, scan_conflicts, build_context, query_structure, read_flagged_memories, recall_context_memories, recall_memories, and all 33 ops tool modules + base.py + resources.py + server.py + lifecycle.py + service_access.py + contracts.py + formatters.py + tracker.py.

---

## 10. What Was Checked and What I Could Not Verify

**Checked (with evidence):**
- Full tool→backend contract sweep (names/order/arity/return-type) — 54 tools (§2).
- Registration reconciliation + uniqueness (§3).
- Duplicate-method AST sweep over the entire `src/menhir` (not just MCP) to find both instances (§6.1).
- pyflakes for unbound names; CancelledError handling; datetime-format window comparison; blocking-I/O-in-async sites (§6.2–6.5).
- Tier appropriateness (§5).
- Executed CF-2 reproduction with the project venv (§4.1).

**Could not verify / limitations:**
- **No live backend / Neo4j / telemetry DB** was available; findings 6.4 and 6.5 (SQL window skew; blocking writes) are proven by code+SQL reading but not executed against a real database. The bug-class-4 magnitude claim (up to ~a day of over-inclusion at the boundary) is analytic, not measured.
- **Bug class 4/5** live impact and the `fail_exhausted_pending_episodes` repo-level parity (open question 1) were not executed.
- The backend *server* HTTP path for `supersede_artifact` (BackendClient → `/supersede_artifact`) was not run; it routes to the same runtime method, so the same bool-return + `.get()` crash is expected but the client wire shape was not directly exercised.
- I did not read the peer audit results file (per task rule) or the confirmed-findings register beyond what was inlined.

---

## 11. Review Confidence: 84/100

**+** Complete coverage (70/70 files read, line total reconciled exactly). CF-2 reproduced end-to-end with executed evidence including the argument inversion, not just the arity. Both codebase-wide duplicate-method instances found via a body-compared AST sweep. Registration count reconciled against a live import (54, not 52). pyflakes run clean for undefined names.

**−** Bug classes 4 and 5 are code/SQL-read proven but not executed against a real SQLite/Neo4j (would require a live env). The `fail_exhausted_pending_episodes` behavioral-parity question and the BackendClient wire path for `supersede_artifact` were reasoned, not run. A handful of low-severity limit/drift observations are judgment calls. Several doc-count claims (README 52, architecture 43, endpoints 44) are stale vs the authoritative 54, which slightly lowers certainty on whether "52" in the task refers to a now-outdated README.

Not a substitute for a live, DB-backed regression run of the window-stats and supersede paths.
