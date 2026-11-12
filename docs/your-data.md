# Your data in Menhir

Menhir stores memories in Neo4j and keeps a local SQLite telemetry sidecar next to it. This page
lists every store, how long it is kept, and how to remove it. Erasure is incomplete today: see
[What deletion does not remove yet](#what-deletion-does-not-remove-yet).

## Where things live

| Item | Location |
|---|---|
| State directory | `MENHIR_STATE_DIR`, else `WORKSPACE_ROOT/.agent`, else `~/.menhir` |
| Telemetry sidecar (SQLite) | `MENHIR_MCP_TELEMETRY_DB`, else `<state dir>/mcp_telemetry.db` |
| Neo4j | `NEO4J_URI`, default `bolt://localhost:7687`. The bundled compose file keeps data in the `neo4j_data` volume. |
| Log files | `MENHIR_LOG_DIR`, default `<cwd>/logs` |
| OAuth state | the `oauth_as_dir` setting, else `MENHIR_OAUTH_AS_DIR`, else the state directory |
| Secrets (`.env`) | `ENV_FILE`, else `./.env`, else `<state dir>/.env` |
| Hook files (only if you install the hooks) | `~/.claude/menhir-turns/*.json` (`MENHIR_TURN_STASH_DIR`), `~/.claude/menhir-turn-hook.log` (`MENHIR_TURN_HOOK_LOG`), `~/.claude/hooks/.turn_counter.json`, `~/.claude/hooks/project-index.json` |

## What Menhir stores

| Store | What it holds | Where | Kept for | How to remove |
|---|---|---|---|---|
| Memories | Raw memory text (up to 48,000 chars), optional `diff` (up to 50,000 chars), source, session and user ids, namespace, embedding. The Graphiti twin episode carries the same body and diff. | Neo4j `:Episodic` | Indefinitely. A daily job can replace a body with a summary; demoted SESSION memories are deleted after 14 days. | `delete_memory` |
| Entities and facts | LLM-extracted entity names and summaries, fact text, embeddings | Neo4j `:Entity`, `RELATES_TO` | Indefinitely | `delete_memory` on the entity uuid removes one entity and its edges. Nothing removes a single fact edge. |
| Views | Scalar, history, timeline and counter values built from memories | Neo4j `:Entity` with `view_kind` | Indefinitely | `delete_memory` on the view uuid |
| Typed assertions | Typed claims extracted from turns and memories | Neo4j `:TypedAssertion` and related heads | Indefinitely | Removed with their memory by `delete_memory`, and by `delete_namespace` |
| Hook-captured prompts (TurnEvidence) | Accepted raw user prompts with `cwd`, `transcript_path`, `prompt_id`, session id, git branch and commit | Neo4j `:TurnEvidence` | No pruning found | `delete_memory` (also removes turns admitted by that memory), `delete_namespace` |
| TODOs | Content, code ref, priority, status, due date | Neo4j `:Todo` | Indefinitely | No MCP tool. `close_todo` and similar only change status. |
| WorkArtifacts | Title, type, status, source path and hash, open questions | Neo4j `:WorkArtifact` | Indefinitely | No tool |
| Candidates and flags | Low-trust candidates; `user_flagged` markers | Neo4j nodes and properties | Candidates until reviewed; flagged memories are exempt from decay | `delete_memory`; `unflag_memory` |
| Code-structure graph | File and symbol names, paths, signatures, first-line docstrings, README first paragraph | Neo4j, shared group (empty `group_id`) | Refreshed every 30 minutes by default; rescans prune stale entries | No tool |
| File-event metadata | Dirty flags, file hash and mtime on existing file entities | Neo4j file entities | Overwritten per event | No tool |
| Telemetry (observability) | `mcp_events` (500-char payload preview, sizes, client, session), `lifecycle_events`, `episode_task_events`, `lifecycle_actions` | Sidecar SQLite | 30 days (`MENHIR_TELEMETRY_OBSERVABILITY_RETENTION_DAYS`) | Erasure clears addressable rows; `menhir erasure-backfill` for legacy rows |
| Telemetry (diagnostic) | `failure_events` (may hold prompt previews and tracebacks), `recall_receipts`, `conflict_resolutions`, Recall Lab and Extraction Lab runs | Sidecar SQLite | 90 days (`MENHIR_TELEMETRY_DIAGNOSTIC_RETENTION_DAYS`) | Erasure by episode or namespace |
| Revisions | Full old and new field values, including pre-compression original content (the rehydration archive) | Sidecar `memory_revisions` | 14 days (`MENHIR_REVISION_RETENTION_DAYS`) | Erasure by node uuid |
| Merge and graph-operation journals | `merge_audit` (full content of absorbed nodes); `graph_operations` (full snapshots of merged and TTL-deleted nodes) | Sidecar SQLite | Never pruned | `merge_audit` is marked erased by erasure; `graph_operations` is not reached |
| Usage, registries, leases, queues | `llm_usage_events` (tokens, model, may hold prompt text in error fields), client and session registries, scheduler leases, pending actions, `erasure_subjects` | Sidecar SQLite | Never pruned | `llm_usage_events` by episode via erasure; the rest has no removal path |
| OAuth and client state | `client_tokens.db` (SHA-256 token hashes), `menhir_oauth_as.db` (OAuth clients, auth codes, refresh tokens), signing key | OAuth directory | Auth codes 120 s; refresh tokens 30 days; stale unused clients 24 h; revoked clients stay | `revoke_client` (soft) |
| `.env` | API keys and secrets | See above | Until you remove it | Delete the file |
| Logs | `server.log`, `server.err.log`, `server.access.log`, which may include memory text | Log directory | Rotated: 5 MB x 3 backups each | Delete the files |
| Hook client files | Turn-id stash, failure log (never the prompt text), turn counter, project index | `~/.claude` | Stash files are never deleted; hook log has no rotation; counter entries 24 hours | Delete the files, set `MENHIR_TURN_EVIDENCE_ENABLED=0`, or remove the hooks |
| Backups | Optional graph export (`scripts/export_graph_backup.py`, gzipped JSONL, embeddings excluded by default); hosted encrypted backups | `MENHIR_BACKUP_DIR`, default `<repo>/backups/` | Export: no retention | Delete the files |
| Snapshot staging (off by default) | Uploaded project-snapshot bytes | `<state dir>/snapshot-staging` | Until the staging cancel tool is used | Staging cancel tool |

Setting a retention variable to `0` disables pruning for that tier. Pruning runs as scheduled jobs
inside a running server.

## Removing data

- **`delete_memory(node_uuid)`** is a hard delete and needs the operator tier. REST equivalent:
  `DELETE /api/memory/{uuid}`. It removes the memory node, its Graphiti twin, admitted TurnEvidence
  with their projections, typed assertions and consolidation watermarks, and clears addressable
  sidecar content columns. It reports `nothing_to_erase`, `graph_already_absent`,
  `residual_content_after_purge` (quarantined), and a note when some sidecar rows could not be
  addressed. A memory still PENDING or ENRICHING is kept as FAILED with its content removed. It
  accepts memory, entity, TurnEvidence and typed-assertion ids.
  Entities and facts derived from the memory are handled by provenance (#423): one only the erased
  memory supports is deleted; one other memories also support keeps them, but its summary and fact
  text are blanked and it is flagged for re-derivation from the remaining sources. Content that
  conflict resolution absorbed from the memory is cut out of the surviving entity.
- **`delete_namespace(namespace, max_nodes=200, force=False, dry_run=False)`** is hard and
  irreversible, operator tier. REST equivalent: `DELETE /api/namespace/{ns}`. It removes the
  namespace's memories, TurnEvidence, typed assertions, TODOs and WorkArtifacts. It refuses
  `default` and the shared structure group, and `max_nodes` is a blast-radius guard, not a backup.
  Use `dry_run=true` first; it lists the counts per label.
- **Soft tools change status and remove nothing:** `close_memory`, `close_todo`,
  `close_stale_todos`, `resolve_todo`, `supersede_todo`, `reopen_todo`, `unflag_memory`,
  `transition_artifact`, `supersede_artifact`, `revoke_client`. `resolve_conflict` marks the removed
  node GONE and absorbs its content into the survivor.
- **`menhir erasure-backfill [--apply] [--redact-unaddressable]`** redacts legacy telemetry rows
  that erasure could not address.

There is no `erase_memory` MCP tool and no data export command.

## What deletion does not remove yet

- **Derived items with unknown provenance.** When an entity's facts have no recorded source
  episodes (older data), the erase cannot tell whether the erased memory wrote them, so it leaves
  them and replies `erased_incomplete` with the reason `unknown_provenance`.
- **Re-derived text.** A shared entity or fact is blanked at erase time and re-derived from its
  other sources. Nothing yet verifies that a model-written paraphrase of the erased memory does not
  survive in another memory's derived text.
- Views built from an erased memory are marked retired but stay as nodes.
- `delete_namespace` cannot remove the `default` namespace (#476,
  https://github.com/Archolith/menhir/issues/476). Its data shares a graph group with the
  code-structure graph, which `delete_namespace` never touches.
- `graph_operations` journal snapshots: erasure purges the ones that name an erased item (#475).
  Snapshots of completed deletes and TTL deletes are cleared after the diagnostic retention window.
  Merge snapshots are kept until the merged item is erased, because unmerge needs them. Journal
  rows themselves are kept; after these purges they hold ids and request metadata, not memory text.
- `merge_audit`, `llm_usage_events`, the registries and `erasure_subjects` are never pruned.
  Erased ids stay in `erasure_subjects` permanently.
- Log files may contain memory text and are never rewritten.
- Backups are not touched by erasure.
- No tool deletes a single fact edge, a TODO, or a WorkArtifact.
- Whether Neo4j store files keep deleted bytes after a delete is unknown.

The sidecar is compacted hourly (`VACUUM`, then a WAL checkpoint), so content that a purge blanked
does not survive in free pages after the next compaction.

Deleting the sidecar file is not a supported way to erase data: it holds the only unmerge copy, the
erasure journal and the erased-id suppression record.

## Things that leave your machine

- The memory body, its diff, and captured turns are sent to the LLM and embedder you configure
  (`LOCAL_LLM_BASE_URL` for a local model, otherwise OpenAI). That provider's policy applies.
- Graphiti's PostHog telemetry is off by default.
- Recall rerank model download: unless `MENHIR_RECALL_RERANK_MODEL_DIR` is set or
  `MENHIR_RECALL_RERANK_AUTO_DOWNLOAD=false`, a long-running server downloads about 92 MB from
  Hugging Face (`HF_ENDPOINT`, default huggingface.co) the first time it starts, then reuses the
  cached copy (`MENHIR_RECALL_RERANK_CACHE_DIR`, else `HF_HOME`; the container image keeps it in
  the `/srv/menhir/cache/huggingface` volume). No memory, query or token is sent. The request says
  `menhir-recall-rerank` as its User-Agent. huggingface_hub may also fetch its agent registry from
  the same host about once a day; `HF_HUB_DISABLE_TELEMETRY=1` stops that, and the image sets it.
  Whatever the endpoint serves must match the pinned digests or it is not loaded. Hooks never
  download. ONNX Runtime's telemetry events are turned off when the model loads.

## Local vs hosted

- **Local stdio:** the MCP server binds the operator tier, so `delete_memory` and `delete_namespace`
  are available. It needs a running `menhir serve` and `MENHIR_BACKEND_URL`.
- **Hosted:** operators are denied `delete_namespace`, `mint_client` and `revoke_client`. Agent
  clients cannot call `delete_memory`.
- Hosted backups live in the private deploy repo and are outside this page. Retention of the
  encrypted backup archives is unknown from this repository.

## Privacy settings

- `MENHIR_SECRET_POLICY` defaults to `redact`: recognised credentials in memory text are replaced with
  `[REDACTED:<kind>]` on write and on recall. `refuse` rejects such writes; `off` stores and returns
  text as given. See [security posture](security-posture.md).
- `MENHIR_PRIVACY_REDACT` defaults to off. Recall Lab and Extraction Lab runs therefore keep raw
  text in the sidecar for 90 days.
- `MENHIR_TELEMETRY_RECORD_RECALL_QUERIES` defaults to off. When on, each recall (MCP recall tools and
  `POST /api/recall`) adds a `recall_query_log` row to the sidecar with the query text, a SHA-256 of
  the query, the ordered result ids and scores, and the recall receipt token. Rows are kept for 90
  days. With `MENHIR_PRIVACY_REDACT=true` the query text is not stored; only the hash, ids and scores
  are. Namespace erasure scrubs the query text of rows recorded with that namespace; the ids, scores
  and hash stay. Recalls made without a namespace are recorded with a null namespace, which
  namespace erasure does not reach, so for those only the 90-day retention removes the query text.
- To stop prompt capture, set `MENHIR_TURN_EVIDENCE_ENABLED=0` or remove the hooks.

## Source references

Paths are under `src/menhir/` unless they start with `scripts/` or `deploy/`, or are
`docker-compose.yml` at the repository root.

- Locations: `infrastructure/paths.py`, `env_file.py`, `infrastructure/logging_config.py`,
  `config/settings_model.py`, `docker-compose.yml`
- Memories and lifecycle: `infrastructure/episode_lifecycle.py`, `infrastructure/memory_queries.py`,
  `services/ingest_limits.py`, `services/lifecycle_decay.py`, `services/lifecycle_consolidation.py`
- Erasure: `services/erasure_coordinator.py`, `core/backend_runtime_data_ops.py`,
  `infrastructure/telemetry/erasure_purge.py`, `infrastructure/telemetry/erasure_inventory.py`,
  `mcp/tools/ingest/delete_memory.py`, `mcp/tools/ops/delete_namespace.py`, `cli/__init__.py`
- TurnEvidence and hooks: `infrastructure/turn_evidence_repository.py`,
  `scripts/hooks/menhir_turn_evidence_common.py`, `scripts/hooks/menhir_file_event.py`
- TODOs, WorkArtifacts, candidates, structure: `infrastructure/todo_repository.py`,
  `infrastructure/work_artifact_repository.py`, `infrastructure/candidate_repository.py`,
  `infrastructure/structure_queries.py`
- Telemetry sidecar and retention: `infrastructure/telemetry/store.py`,
  `infrastructure/telemetry/lifecycle_store_stats.py`, `services/scheduler_tasks.py`,
  `services/maintenance_scheduler.py`, `infrastructure/graph_operations_prepare.py`
- OAuth: `api/client_token_store.py`, `api/oauth_refresh_store.py`, `api/auth_code_store.py`
- Access tiers and tool profiles: `tiers.py`, `access_contract.py`, `mcp/tool_profiles.py`,
  `api/client_policy.py`
- Backups: `deploy/menhir-backup-local.sh`, `scripts/export_graph_backup.py`, `deploy/README.md`
- Privacy: `config/feature_flags.py`, `explorer/recall_lab.py`
