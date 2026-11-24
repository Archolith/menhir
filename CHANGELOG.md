## 2026-11-24 - Remove dead dev-only scripts (#789, #792, #793)

- #789: removed the dead doc-token benchmark script under `.agent/tools/` (whose scenario file and `.agent/user/` tree no longer exist) along with its `.agent/README.md` section, `user/` fallback paragraph, and `.agent/file-index.md` entry.
- #792: removed the superseded M2 functional audit probe under `.agent/audit/` (which printed unconditional `CONFIRMED` lines for fixed security findings).
- #793: removed the orphaned QuantState prototype script under `.agent/plans/backlog/` (whose companion plan is archived and fold logic is superseded by `domain/fold_algebra.py` and `domain/scalar_state_fold.py`).

## 2026-11-23 - v0.3.14: Agent Smith OAuth clients come from the client policy; conflict lifecycle and recall fixes

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.14. No dependency changes in this commit.
- Upgrade note (#761): Agent Smith OAuth identities (`/oauth/client-metadata/agent-smith.json?client=<key>`) are served only from client-policy entries with a `registration`; the built-in table is gone. A deployment that uses Agent Smith must add those registrations to its policy in the same upgrade.

## 2026-11-22 - A memory whose enrichment fails stays recallable; extraction and lab fixes (#662, #483, #265)

- #662: the source-memory lane (`search_episode_embeddings`) now returns FAILED episodes; only
  erase tombstones and episodes with a sealed erase marker are excluded. The embedding repair
  job lists READY and FAILED episodes (`settled_only`), an oversized episode is embedded before
  it is parked, and exhausted-lease parking writes no raw-capture copy. Legacy raw captures are
  hidden from ranked recall and from recent memories. Source-search readiness
  (`source_search_eligible`) uses the same predicate as recall, and a FAILED receipt no longer
  uses up a timeline-anchor slot. The FAILED warnings on `add_memory`, the operator warning and
  `get_memory_stats` now say the text is recallable but has no entities or facts.
- #483: a combined-extraction response that is not a JSON object raises
  `CombinedExtractionShapeError`, classified as an output-parse error for manual review, instead
  of a bare `TypeError`.
- #265: the extraction lab passes the edge expiry, invalidation and preserved-attribute hooks
  that ingest uses.

## 2026-11-22 - A scalar View from a not-yet-admitted turn is recallable (#311)

- `infrastructure/typed_assertion_write_repository.py`, `projection_coverage_repository.py`,
  `scalar_view_repository.py`: when a turn-anchored assertion's TurnEvidence has no admitting
  Episodic, its `episode_uuid` is the turn id instead of `''`. The folded View's receipt is then
  that turn, it gets a TurnEvidence `MENTIONS` edge, and recall's live-provenance check passes;
  before, the View had an empty receipt and stayed hidden. The turn is used only when it is
  usable View evidence (finalized, not quarantined): a turn captured before finalization existed
  keeps the empty receipt, because the write gate would refuse it and abort the entity's rebuild.
  A finalized admitting episode becomes the receipt; the write path resolves a non-finalized one
  back to its turn. `source_episode_uuid` still reports admitted episodes only.
- Live tests cover the turn-only View, finalized and non-finalized admitters, a View rebuilt
  after admission, and a legacy turn that must not abort the rebuild.

## 2026-11-22 - Retry a timed-out source-memory embedding (#409)

- Source-memory ingestion retries one embedding timeout before finishing enrichment. A
  subsequent failure leaves the receipt available for the source-embedding repair job.
- Regression tests cover a successful second attempt and a repeated timeout.
- The episode status response reports source-search readiness separately from graph
  enrichment (`source_search_status`: READY, PENDING, UNKNOWN or UNAVAILABLE). The stored
  vector is compared with the size the configured embedder produces, so an embedder smaller
  than 1024 dimensions reads READY; when that size is not known the status is UNKNOWN.
- A PENDING status carries `source_search_reason`, and the guidance names what changes it:
  `missing` (the repair job), `missing_in_flight` (the ingest step), `size_mismatch` or
  `model_mismatch` (a re-embed with `scripts/backfill_episode_embeddings.py`).

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
