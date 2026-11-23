## 2026-11-22 - Conflicts needing attention appear as an operator warning, like FAILED saves

- `infrastructure/telemetry/operator_warnings_store.py`, `core/backend_runtime_admin_ops.py`,
  `mcp/formatters.py`, `mcp/tools/ops/acknowledge_warning.py`: a new `conflicts` operator warning.
  It is raised while any conflict group is confirmed and undecided, or has waited over 24 hours for
  LLM review. It shows on `add_memory`, `add_memory_and_track` and the session-start tools, and is
  listed in `get_memory_stats`. `acknowledge_warning(kind="conflicts")` silences it until a
  conflict is newly confirmed or newly stuck. Like `failed_episodes` it counts every silo and is
  hidden from namespace-pinned callers.
- `infrastructure/memory_queries.py`: `fetch_memory_overview` adds `unresolved_conflict_groups`,
  `stale_pending_conflict_groups` and `latest_conflict_attention_at`, read in the same overview
  call the FAILED warning already shares.
- `infrastructure/consolidation_queries.py`: members record `conflict_confirmed_at` when review
  confirms them or a PROMOTED pair routes them to `unresolved`, and `conflict_pending_since` when
  they enter review (including a requeue). Both are structural in `graph_text_inventory.py`.
  The conflict counts in the overview skip protected members, which no conflict tool can list or
  settle; pending age is measured from `conflict_pending_since`.
- `mcp/formatters.py`: the `add_memory` warning lines apply the caller's redaction on the client
  side, like the session-start surface.

## 2026-11-22 - Conflict lifecycle: pending conflicts are visible, review covers every member, enrichment queues conflicts (#651, #652, #654, #655)

- `domain/warden.py`, `domain/truth/labels.py`, `services/assertion_pipeline.py`: recall flags a
  `pending_llm_review` member as `possible_conflict` and an `auto-resolved` one as
  `unsettled_conflict`. Both are FLAG only, never refused (#651, #652). The 14-day auto-resolve job is
  kept; a confirmed contradiction it ages out stays visibly marked.
- `infrastructure/memory_queries.py`, `mcp/tools/ops/get_memory_stats.py`,
  `services/scheduler_tasks.py`: stats show the pending-conflict backlog and its oldest entry; the
  queue-health check warns when the oldest is over 24 hours old (#651).
- `infrastructure/consolidation_queries.py`: `set_conflict` no longer overwrites the status of
  members already in a group. A confirmed member stays `unresolved`, and members absorbed in a group
  merge keep their own status (#654).
- `services/lifecycle_conflicts.py`: LLM review compares each member in the reviewed status with the
  rest of its group, instead of members 0 and 1 standing in for the whole group. Confirmed members
  become `unresolved`; a member whose every pair is rejected leaves as `false_positive`, with those
  pairs suppressed, and a group left with one member dissolves. Pair verdicts are cached per run, and
  `limit` also caps LLM calls. New repository ops `set_conflict_member_status` and
  `remove_conflict_member` (compare-and-set; the removal write-locks the group with a transient
  `_conflict_group_lock` token) and `list_conflict_groups(include_all_members=True)` (#654).
- `services/enrichment_steps.py`, `services/lifecycle_consolidation.py`: conflict pairs found by
  correlation at enrichment are queued through the same path as consolidation
  (`record_conflict_pair`: cooldown, PROMOTED routing, `set_conflict`), one per node, instead of
  being counted and dropped (#655).
- The 14-day auto-resolve now skips a group that still holds a pending member; it goes through
  `auto_resolve_settled_conflict_group`, which re-checks under a group lock. Removal after review
  is refused if a member joined since the verdict. A node joining a group fresh gets a new
  `conflict_created_at`. Review drains the oldest groups first.

## 2026-11-22 - Startup requires every phase-one uniqueness constraint; L4 supersession refuses historical endpoints (#741, #742)

- `infrastructure/schema.py`, `infrastructure/memory_graph_adapter.py`, `core/bootstrap.py`,
  `core/runtime.py`: startup now requires all ten uniqueness constraints phase one creates, up from
  four: added `work_artifact_uuid_unique`, `artifact_source_uuid_unique`,
  `artifact_source_locator_unique`, `artifact_reconcile_cursor_repository_unique`,
  `metric_uuid_unique` and `turn_evidence_key_unique`. If one cannot be created, writer startup
  (and candidate-readonly startup) is refused, and the message counts the duplicated values that
  block each constraint. **Upgrade note:** an install whose graph holds duplicates for any of these
  refuses to start until they are repaired; installs that already have the constraints are
  unaffected.
- `infrastructure/artifact_repository.py`, `services/artifact_service.py`: L4
  `supersede_artifact` write-locks every node with either id, then refuses when any copy of either
  id is historical (no b->a after a->b, no overwrite of `superseded_by`). The service reports
  `refused` with a reason, separate from `not_found`, and treats a retry of an applied
  supersession as success. `graph_text_inventory.py`: the `_artifact_supersede_lock` token is
  structural.

## 2026-11-22 - Client tool allowlist on resource listings, named REST routes and the explorer (#657, #658)

- `api/mcp_remote.py`, `mcp/contracts.py`: `resources/list` and `resources/templates/list` show a
  restricted client only what it could read (governing tool, tier, pin). A query-auth caller is
  listed nothing, since every resource read refuses query auth. Resource reads keep their checks
  and messages; one helper, `tool_allowed_for_request`, now backs reads, listing and REST.
- `api/route_tools.py`, `api/routes.py`: each named `/api` route maps to the MCP tool it
  corresponds to. A client with a tool allowlist (`MENHIR_CLIENT_TOOLS` or a client policy) gets
  403 on a route whose tool it lacks, and on routes with no tool: the hook capture routes
  (turn-evidence, episode-admission, tool-events), phase3 run/status/reset and views. Health and
  ready stay open. An integration that sends a client name listed in `MENHIR_CLIENT_TOOLS` needs
  that list to include the tools behind the routes it calls (for example `build_context` for
  `/api/context`).
- `explorer/app.py`: the explorer refuses any client with a tool allowlist.

## 2026-11-22 - v0.3.13: Graphiti fork save fixes, breaker gaps closed, ranked source memories in build_context

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.13. No dependency changes in this commit.

## 2026-11-22 - Graphiti fork 0.31.2.post4: entity-edge save fixes (Archolith/graphiti #28-#30)

- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post4`, fork commit `d94da8d7`. Pins, docs and release checks updated.

## 2026-11-22 - Phase 3 reports a billing refusal or open breaker as unavailable (#743)

- `POST /api/phase3/run` answers 503 (`phase 3 consolidation unavailable: <reason>`) when the
  pass stops on a billing refusal or an open breaker, instead of a 500 with a traceback. The
  namespace stays dirty, as before. Any other error is unchanged.
- The scheduler's job wrapper logs the same cases as one WARNING without a traceback and records
  the failure as `provider_unavailable` (retryable) rather than `scheduler_error`.
- Both use `provider_unavailable_reason` (circuit_breaker.py); `circuit_open_cause` moved there
  from graphiti_client.py, which keeps the old private name as an alias.

## 2026-11-22 - Breaker gaps after #638: sync clients, probe wait, reranker, degraded drill-downs (#639, #640, #641, #642)

- #639: the sync view embedder and the personal-memory sync chat take the Graphiti breaker for the
  same endpoint (`sync_client_breakers`; none when the base URLs differ). While it refuses, the
  embedder returns `None` and the chat raises `CircuitOpenError`, with no provider call. A billing
  refusal either one sees hard-opens that breaker: `report_quota_refusal_threadsafe` runs the
  hard-open on the breaker's loop under its lock, so a worker thread never writes breaker state.
- #640: `CircuitBreaker.call(wait_for_probe_s=...)` lets a call that arrives during a half-open
  probe wait for the probe's outcome (bounded) instead of failing at once. Every path that ends a
  probe, cancellation included, wakes the waiters. Only `create_batch` (ingest's concurrent
  batches) waits, up to the request timeout; recall and search embed through `create`, which
  still refuses at once so they fall back to keyword search.
- #641: Graphiti's cross-encoder is wrapped (`BreakerGuardedCrossEncoder`), so the `reranker`
  snapshot reports real calls. Menhir's searches use RRF today, so it stays closed in practice;
  snapshot keys are unchanged.
- #642: with the embed breaker open, `recall_history` returns no memories and a note ("semantic
  search off: ... ; recall_history needs embeddings ..."), and a timeline `query` returns the
  same note instead of raising. An ordinary embed error still raises. The source-memory and
  observation lanes log a breaker refusal at INFO without a traceback.

## 2026-11-21 - supersede_artifact refuses a terminal replacement (#678)

- `supersession_cypher` now also requires the replacing artifact to be non-terminal. Before, after
  A superseded B, B (SUPERSEDED) could supersede A and form a SUPERSEDES cycle; a DEFERRED artifact
  could also be named as a replacement.
- No cycle can form: every node in one is the old side of an earlier edge, so already SUPERSEDED,
  and no writer moves an artifact out of a terminal state. Online tests cover A->B then B->A, a
  three-node cycle, a DEFERRED replacement, a rejected `supersedes:` declaration, and B->A blocking
  on an uncommitted A->B that holds both locks, then refusing once it commits.
- A refusal caused by the replacement names it: reason `replacement_terminal`. Other refusals keep
  `type_mismatch_already_terminal_or_namespace_incompatible`.
- Chains must be recorded oldest first: once v3 supersedes v2, v2 can no longer supersede v1;
  supersede v1 with v3. Documented in `.agent/endpoints.md` and `artifact_authoring.md`.
- Not covered: an artifact can still be moved to DEFERRED after it replaced another (no cycle).

## 2026-11-21 - Sarcasm and unaffirmed third-party claims stay out of facts and scalar values (#283)

- Extraction (both self-endpoint instruction blocks in `graphiti_extraction_policy.py`): sarcasm and
  irony are not affirmative facts; store the meant sense or only the literal events. Negation,
  questions and hypotheticals were already covered by "Preserve negation".
- Typed scalars (`TYPED_SCALAR_SYSTEM_PROMPT`): emit only values the episode presents as true. No
  value from sarcasm, and none from another person's claim that the speaker does not affirm.
- Measured with `scripts/probe_nonveridical_extraction.py` (84 single-turn cases), 3 runs per arm on
  a 50-case subset, gpt-6-luna Flex. Fact leaks from sarcasm: 15 of 24 on main, 2 of 24 with the
  rules; no other category leaked on either arm. Missed true facts: 18 of 96 on main, 23 of 96 with
  the rules (mostly q-06 and rep-04). Scalars, one run: the sarcastic values and the contradicted
  third-party value are gone, and the sincere controls keep theirs.
- The prompts' examples are new, and `tests/test_nonveridical_extraction_guards.py` fails if any
  prompt quotes a 4-word run of the probe corpus.
