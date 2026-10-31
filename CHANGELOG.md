## 2026-10-30 - Delta-audit remediation (CF-259..CF-306)

Fixes from the 2026-10-29 delta audit (`ca3f762f..0e078983`). The root findings register ("Delta audit 2026-10-29") carries each CF's status and commit, and lists the residuals. CF-277 is held for an owner decision, CF-285 is closed by design (dates stay UTC), and a new High finding, CF-307, is fixed (see LLM retry below).

Operator-visible changes:

- **Telemetry:** Graphiti fork PostHog telemetry is off by default. Menhir sets `GRAPHITI_TELEMETRY_ENABLED=false` at client construction unless the operator set it, and `GRAPHITI_TELEMETRY_ENABLED=true` turns it back on (CF-272).
- **Install:** the documented Neo4j floor is 5.26 (README, `docs/post-install.md`). `python-multipart` is declared. A missing or incomplete `graphiti_core` now fails with a message naming `archolith-graphiti-core==0.31.2.post1` (CF-297, CF-303..306).
- **Embedding model:** the episode embedding stamp now records the configured embedding model instead of `unknown`, so the source-memory model filter applies. Legacy `''`/`unknown` stamps still match. Changing the embedding model on an existing graph is not supported: recall skips vectors made by another model, and the scheduled repair job and the `--dry-run --require-complete` check do not see them. A model change needs a full re-embed or a fresh graph (CF-260).
- **Settings:** the flex request timeout is validated at settings load. A non-numeric, non-finite or non-positive value fails startup and names the env var, even when the flex tier is unused (CF-286). A retired flag explicitly set to false logs a startup warning (CF-301).
- **Redaction:** JWKS failure logs redact URLs fail-closed (CF-300). Recall Lab redaction is an allowlist on every route (CF-269).
- **LLM retry:** the outer LLM retry no longer retries provider 429s; the rate-limit layer owns them (CF-262). One bad anchored-time item drops only itself, not the episode (CF-263). A provider 429 that Graphiti rewraps as its own `RateLimitError` ("Rate limit exceeded", no "429" in the text) now requeues the episode like a raw 429, instead of parking it in `manual_review` for good. An `insufficient_quota` 429 still parks (CF-307). Errors that cannot succeed on retry now get one attempt: a 4xx other than 408/409 (bad request, auth, 402) and an unimplemented backend. Billing 429s (`billing_hard_limit_reached`, `credit_balance_*`, "exceeded your current quota") are treated as quota like `insufficient_quota`. Conflict review stops under a standing chat quota and leaves the remaining groups pending. 5xx, timeouts and throttle 429s still retry (CF-262).
- **Extraction on gpt-6:** the contained-edge prune is off for the gpt-6 profile, the only one that enabled it, so no shipped profile prunes. Token containment cannot tell whose fact an edge states, so the prune could delete a user's own fact and its node. gpt-6 extractions now keep restated sub-facts as redundant edges, and `contained_edges_pruned` stays 0. The code is kept; re-enabling it needs a meaning-based check (#402, CF-259).

Behaviour fixes:

- **Extraction:**
  - The contained-edge prune keeps a fact when the keeper cannot see its endpoint (CF-259, before it was switched off; see above).
  - Status reporters follow the verbatim-skip projection everywhere, including `list_enrichment_queue`, the explorer and READY counts (CF-261).
- **Counting and scalar state:**
  - The related pool is fetched per candidate (CF-264). Known issue: past 10 related rows a candidate is never counted, so recurring counts plateau at about 11 (Archolith/menhir#405, pre-freeze).
  - An entity merge re-arms moved counted rows for re-decision (CF-265).
  - Rebuild failures stay owed, and rebuilds and coverage audits fold with the writer's parameters (CF-266, CF-267, CF-270, CF-290).
  - The LME repair script uses the runtime counting and history flags (CF-289).
  - A zero-row counted View clears stale `CONTRIBUTED_TO` edges (CF-291).
  - `purge_scalar_state_nodes` also removes history Views and `scalar:` read markers (CF-292).
  - Phase3 dirty signals read the live scalar cursor (CF-273).
- **Recall:**
  - `recall_timeline` query mode anchors past unanchorable hits (CF-268).
  - CLI hook recall honours scalar history like the backend (CF-278).
  - `fetch_temporal_facts` drops its unused `namespace` parameter (CF-279).
  - A failed recall cancels its source-memory task (CF-281).
  - Receipts name scalar Views by `view_name` (CF-282).
  - Typed-history `prev_offset` is clamped at 0 (CF-283).
  - `agent_quoted` matching ignores case, surrounding whitespace and `-`/`_` in Python and in the two scalar foundation queries (CF-287).
- **Graph-op journal:**
  - Transitions are owner-checked compare-and-set (CF-271).
  - A refused `mark_committed` now logs at ERROR and writes a failure event (`failure_stage="commit_refused_after_graph_work"`). Every caller commits only after its own graph work, so the graph may hold the after-state while the journal says FAILED. The journal state is not changed; escalating to NEEDS_REVIEW is #404 (CF-271 F2). That event now keeps the op id, kind and journal state; they had been redacted as unscoped telemetry. A writer whose op another owner already committed now gets `StaleTransitionError` instead of an idempotent success, which is tracked by a new nullable `resolved_by_token` column added on startup. A repeat commit by the same owner is still idempotent (CF-271 review F-1, F-2). Upgrade every process that shares a journal file: a commit made by an older process leaves the column empty, so it gets no different-owner check.
  - The committed-pairs scan pages by keyset on `(created_at, rowid)` (CF-275).
  - Telemetry SQLite connections close when their `with` block exits (CF-276).
- **Eval script:** `scripts/eval_anchored_time.py` routes the resolver like production, reserves budget before each call, and stops before scoring a set the cap cut short (CF-288).
- **By design, documented:** CF-274, CF-280, CF-294, CF-299. CF-284's premise was false: nothing calls `_belief_commit_context`.

Tests: each unit added counterexample tests; online tests ran against a throwaway Neo4j 5.26. Full offline suite: 12,234 passed, 914 skipped, 1 failed, 14 errors in 10m48s. The failure was `test_large_module_boundaries` (`telemetry/store.py` at 426 lines against its 425 cap, from the CF-269 comment), fixed in ee0e88d0 and rerun green. The 14 errors are `test_beacon_generation`, which requires `MENHIR_TEST_BEACON_PYTHON` and fails loudly when it is unset (an environment gate).

`CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-29 - Bucket-2 flags removed (belief gate, evidence anchor, pools); local-model routing settings

The three remaining default-off recall flags from #399 bucket 2 are removed; the code is kept on `archive/bucket2-flags-2026-10-29`. All six names are in `feature_flags.RETIRED`, and `tests/test_feature_flag_registry.py` checks each retirement. Default recall is unchanged.

- `FRONTIER_BELIEF_GATE` (quality delta 0, p95 +12.6%): deleted `domain/belief_evidence.py` and `services/change_log_provider.py`; removed CurrentnessWarden, `WardenContext.belief_score`, the ContradictionWarden conflict-set branch, and the frontier metadata's `anchor_paths`/`anchor_project` writes (no reader). `domain/git_staleness.py` keeps only `GitChange` and `_parse`.
- `FRONTIER_EVIDENCE_ANCHOR` (3 of 33,861 entities pass): removed EvidenceAnchorWarden, `WardenContext.support_profile`, the assertion pipeline's `evidence_anchor` kwarg, and SupportProfile / ReinforcementVerdict / assess_reinforcement. Its True fallbacks were unreachable in production. Recall Lab drops arm D.
- `FRONTIER_SOURCE_MEMORY_POOLS` (indistinguishable from no pools on AMA): deleted `domain/source_memory_pools.py`; removed `SourceMemory.pool_id`/`pool_anchor`, the pool branches in `recall_pipeline` and `context_builder`, and the `pools` parameter on MCP `recall_history`, `POST /recall/history` and the backend layers. Null `pool_id`/`pool_anchor` keys no longer appear; a REST request that still sends `pools` is accepted and ignored.
- `.env.example`, `.agent/default-off-features.md`, `.agent/endpoints.md` and `.agent/tasks-mcp.md` drop the removed names.
- Tests: the pools, belief-gate, git-staleness and evidence-anchor tests are deleted with their code; the warden chain pin is now `("scope", "oracle_admission", "contradiction")`.
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

New settings let the cost lanes run on a local OpenAI-compatible server (#399 item 3). With them unset, clients, base URLs, request bodies and the `make_sync_chat` call are unchanged.

- `MENHIR_ANCHORED_TIME_BASE_URL` / `MENHIR_ANCHORED_TIME_API_KEY`: a separate endpoint for the anchored-time resolver and attach check. It requires `MENHIR_ANCHORED_TIME_MODEL` while the resolver is on, so the ingest model name is never sent to another server.
- `MENHIR_ANCHORED_TIME_EXTRA_BODY` and `MENHIR_PERSONAL_MEMORY_CHAT_EXTRA_BODY`: JSON objects merged into those requests' `extra_body` (e.g. `{"chat_template_kwargs":{"enable_thinking":false}}`). The setting wins over model-profile extras and `disable_reasoning`. A value that is not a JSON object fails at settings load and names the env var.
- `MENHIR_PERSONAL_MEMORY_CHAT_TIMEOUT_S`: per-request timeout for the personal-memory chat (0, the default, keeps the 30 s `DEFAULT_REQUEST_TIMEOUT_S`). A local model at ~20 tok/s needs minutes per lane call.
- Tests: `tests/test_local_model_routing_settings.py`.

`.agent/workflows/run_and_test.md` gains the owner's standing benchmark rule: run as parallel as the system can handle within reason (every namespace of a staged fixture at once), and paid runs under $0.25 need no go-ahead when a cost monitor with a manual ceiling covers them.

`uv.lock` upgrades five dependencies with security fixes. The no-publish release image for 9abaa3d1 failed its vulnerability policy on authlib GHSA-6v38-4h7w-rj9h (Critical, published 2026-10-27) and on CPython 3.12.14 in the pinned base (CVE-2026-19445, Critical).

- authlib 1.7.2 to 1.9.1 (also GHSA-8m4g, GHSA-7vr2 High); fastmcp and fastmcp-slim 3.4.4 to 3.4.11 (GHSA-7xgj-8392-5q4r); opentelemetry-api 1.43.0 to 1.45.2 (CVE-2026-114603); dnspython 2.8.0 to 2.9.1 (GHSA-45c3, High). `pyproject.toml` ranges are unchanged.
- Release image base: `python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159` (Debian 13.7, Python 3.12.16, no Critical findings). Its High findings are 14 Debian OS-package CVEs that Debian marks won't-fix.

Enrichment status of a verbatim-skipped episode follows its evidence projection. A client episode whose text its turn's projection extracts is set READY (`verbatim_skipped`) without extraction while that projection may still be PENDING or ENRICHING, so `get_enrichment_status(wait=True)` could return READY before anything was extracted (E2E `test_e2e_09_scalar_binding_repair` failed on Windows at 9abaa3d1).

- `domain/verbatim_skip_status.py`: `effective_processing_row` reports such a client as the projection's progress while the projection is unfinished, READY with the projection's enriched episode and touch counts once it is READY, and PENDING (`verbatim_skip_projection_lost`) while a FAILED or deleted projection waits for `reenable_verbatim_skips`.
- `fetch_episode_processing` also returns `extraction_skipped_for` and `skip_projection`; its raw state is unchanged, so lease fences, enqueue/requeue checks and `force_release_lease` still see the client's own row.
- Status and wait readers use the effective row: `get_enrichment_status`, `watch_enrichment` and `add_memory_and_track` (MCP wait loop), `get_episode_trace`, `wait_for_episode_processing` (REST `POST /memory?wait=true`, `ingest_episode`) and recall's pending-episode wait. `list_enrichment_queue`, the explorer and READY counts still list the client row as READY.
- Tests: `tests/test_verbatim_skip_status.py`; `tests/test_verbatim_skip_live.py` gains the status read against Neo4j.

`deploy/build_release_image.py --mode verify` works on a Docker host with the containerd image store (Docker 29 default). There `docker image inspect` reports the manifest digest as the image ID and renders the config without the classic store's empty legacy fields, so a correct bundle failed with "loaded image ID does not match validation identity" (E2E-6 container lane, 7030e78f). A loaded ID that is not the recorded config digest is now accepted only when it is the digest of a manifest in the archive's OCI index whose config is the recorded image ID, with the manifest blob hash-checked. Its loaded config must then equal the archive's hash-checked config blob. The classic path still requires the recorded config hash. Tests: `tests/test_build_release_image.py`.

## 2026-10-29 - Anchored-time render removed

`MENHIR_ANCHORED_TIME_RENDER` (default off) showed each fact's persisted anchored-time contract as its event time in recall, context and timeline. Paired A/B on a resolver-on graph of 211 LongMemEval items (78 knowledge-update, 133 temporal-reasoning): off 198/211, on 198/211, flips +4/-4, with render text in 169 contexts (#400). It is removed; the code is kept on `archive/anchored-time-render-2026-10-29`. The anchored-time resolver and its `time_*` edge properties are unchanged.

- Deleted `domain/event_time.py`. `TemporalFact.event_time`, `TimelineFact.event_time` and the matching REST response fields are gone; with the flag off they were always unset and never emitted.
- `infrastructure/cypher.py`: `FACT_TEMPORAL_FIELDS` no longer projects the nine `time_*` contract columns, which only the renderer read.
- `infrastructure/memory_queries.py`, `memory_graph_adapter.py`, `services/timeline_service.py`, `core/backend_runtime_data_ops.py`: the timeline `event_time` parameter is gone; the query is the former flag-off text.
- `services/recall_policies.py`, `recall_pipeline.py`, `context_builder.py`, `mcp/formatters.py`, `explorer/recall_lab.py`: the render branches are gone; flag-off output is unchanged.
- Settings: `anchored_time_render_enabled` and `RetrievalTuning.enable_anchored_time_render` removed; both names are in `feature_flags.RETIRED`. `.env.example` and `.agent/default-off-features.md` drop the flag.
- Tests: deleted `tests/test_anchored_time_render.py`, `test_anchored_time_render_review.py`, `test_anchored_time_render_review_r2.py`, and the render cases in `test_anchored_time_state_start.py` and `test_anchored_time_attach_hook.py`. `tests/test_feature_flag_registry.py` checks the retirement.
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-28 - Context todos are scoped to the caller's namespace and appear again

`build_context` adds up to 3 keyword-matched open todos ("Related open TODOs"). That search never ran: it passed a Cypher parameter named `query`, which collides with `session.run(query, **params)` in `Neo4jClient.execute`, and the raised TypeError was swallowed, so the section was always empty. It also had no namespace filter, so once fixed it would have shown other namespaces' todos (#401).

- `infrastructure/todo_repository.py`: `search_by_query` takes `namespace` and keeps only the caller's namespace plus the default bucket (`None` stays unrestricted), the rule `list_todos` and `get_todo` use. The filter runs in Python like `todo_supersession`'s, so there is no new hand-written tenancy predicate (CF-127 ratchet). Scoped searches read up to 200 matches before filtering. The search parameter is renamed `query_text`.
- `infrastructure/memory_graph_adapter.py`: `list_todos_matching_query` forwards `namespace`.
- `services/context_builder.py`: `build_context` passes its `namespace` to the todo search. REST `/api/context`, the MCP `build_context` tool and the hook now show matching todos.
- `core/backend_runtime_admin_ops.py`: `close_todo` and `delete_todo` call `_require_own_todo`, as supersede/resolve/reopen/link already did. Through `/api/internal/backend/{op}` a client pinned to one namespace could close or delete another namespace's todo by uuid (the MCP `close_todo` tool already checked; the internal ops did not). Unpinned callers are unchanged. Test: new `tests/core/test_todo_close_delete_ownership.py`.
- Tests: new `tests/test_context_todo_namespace_live.py` (online: namespaces A, B and default; scoped to A shows A and default, never B). `tests/test_todo.py`: namespace scoping tests, and the existing parameter test asserts `query_text` and no `query` key. `tests/test_context_builder.py`: the namespace reaches the todo search.
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-26 - Episodes over ~8 KB can be written again

Neo4j cannot index a property value over about 8 KB, and `episodic_content_idx` (a RANGE index on `:Episodic(content)`, added 2026-08-18) made every longer episode fail to write; an LME build died on a 9,838-byte episode. Nothing seeks that index any more (its content prefix scan is gone, and text search uses Graphiti's FULLTEXT `episode_content`).

- `infrastructure/schema.py`: the index is no longer created; `RETIRED_INDEXES` lists it and `get_retired_index_queries()` drops it (`DROP INDEX ... IF EXISTS`).
- `infrastructure/memory_graph_adapter.py`: `retire_obsolete_indexes()` runs those drops and returns failures.
- `core/bootstrap.py`: `prepare_memory_runtime` runs the drops on every writer start, before the readiness gate, because phase-one bootstrap is skipped on any install whose required indexes are online (every existing graph). A failed drop is logged and reported as `retired_index_failures`; startup continues. Candidate-readonly startup does not drop.
- Tests: new `tests/test_retired_content_index.py` (unit, plus an online test that reproduces the failed write with the old index and passes after the drop). `tests/test_degraded_startup.py`: its fake adapter gains the new method.
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-25 - Fact said time drops the Neo4j zone id; freshness A/B numbers corrected

- `services/recall_policies.py`: `said_at` drops a trailing zone id (`2023-11-30T20:25:00Z[UTC]` -> `2023-11-30T20:25:00Z`), as `domain/event_time.py` does for event times. Test: `tests/test_fact_said_time.py`.
- `CHANGELOG.md`: the freshness gate entry quoted 0.746 for both fixes, measured with the first, broader gate; as shipped it is 0.738 (re-measured).
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-25 - View freshness gate and fact statement time, both on by default

o500 (#168): a View kept leading with a stale value when the newer statement was a same-slot occurrence the fold ignores, and fact lines carried only event time, so a later statement about an earlier event read as older. Recall-only o500 A/B (one run per arm): both off 0.712, said time 0.738 (+0.026, paired 95% CI +0.002 to +0.048). A first gate version also demoted on newer statements in other slots of the same subject; E2E-17 showed this is wrong (a newer book count demoted the coin count), and on o500 only 1 of its 15 demotions changed an answer (a win, from a same-slot occurrence), so the shipped gate keeps only the same-slot rule. Re-measured as shipped: gate alone 0.714, gate and said time 0.738; the gate fired once in 500 questions (that win, no losses).

- `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_FRESHNESS_GATE` (new, on; requires scalar view authority): `domain/scalar_view_freshness.py` (new) finds occurrences in the verdict's exact slot with a later `valid_at`. `services/recall_pipeline.py`: a current (not counted) View or expiry verdict with one is advisory instead of leading; `has_foundation` is unchanged and the audit records `stale_by`. Hits kept out of the ranked list still count; occurrences reach recall only with the counting lane on.
- `MENHIR_FRONTIER_FACT_SAID_TIME` (new, on): fact queries (`infrastructure/cypher.py`, `infrastructure/memory_queries.py`) read the edge `reference_time` as `said_at`; with the flag on, `TemporalFact.said_at`, the API item (`api/routes_support.py`, `mcp/formatters.py`) and the context line (`services/context_builder.py`: `<happened> | said <date> | <fact>`) carry it.
- `domain/retrieval_tuning.py`, `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: the two settings; the shipped settings default on, the `RetrievalTuningConfig` engine defaults stay off.
- `docs/agreement-settings.md`, `tests/test_shipped_agreement_profile.py`: the freshness gate in the shipped profile.
- Tests: new `tests/test_scalar_view_freshness.py`, `tests/test_fact_said_time.py`.
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-25 - Scalar verdict gate on by default; observation recall gated by default

On o500 the scalar lane lost to lanes-off (0.618 vs 0.686): off-topic verdicts and raw observation quotes took the slots of answer-bearing memories (#168). With both changes, counted-rw scores 0.694 (lanes-off 0.686; parity, not yet a significant gain).

- `config/settings_model.py`: `MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY` defaults to 0.675 (raw cosine 0.35, #300's bench value), was 0 (off).
- `MENHIR_PERSONAL_MEMORY_SCALAR_OBSERVATION_RECALL` (new; `gated` default, `all`, `off`): which raw observations enter the ranked list. `gated` keeps those at or above the verdict gate; `all` is the old behavior; `off` keeps none. Slots still feed the authority and history lanes, and a skipped observation still resolves the query subject (G17).
- `domain/retrieval_tuning.py`, `services/recall_pipeline.py`: the mode, validated to the three values.
- `.env.example`, `docs/agreement-settings.md`, `tests/test_shipped_agreement_profile.py`: the new default and setting. `tests/test_scalar_verdict_min_similarity.py`: default assertion. `tests/test_scalar_observation_recall.py` (new).
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-24 - Agent episodes cannot supersede user-stated facts; Graphiti fork 0.31.2.post1

For agent-written memories (#168, plan `menhir-agent-memory-writer-plan-2026-10-23` P1).

- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post1` (upstream Graphiti 0.31.2 plus the fork's hooks, including the new `EdgeInvalidationHook`); pins, docs and release checks updated.
- `infrastructure/graphiti_edge_invalidation.py` (new), `infrastructure/graphiti_client.py`: `MenhirEdgeInvalidationHook` is always installed. Information whose source is not in `GATE_APPROVED_HUMAN_SOURCES` never expires or truncates a stored edge whose stored `r.source` is human; the authority is the cause edge's source (its stored source, else this call's `source_description`, bound per `add_episode` call in a ContextVar). A human cause may still supersede anything. Lookup errors keep the edge (fail closed). Startup refuses a fork without the seam.
- `r.source` now survives duplicate resolution: `source` is always in `preserved_edge_attributes`. Before, a paraphrased restatement cleared the stored edge's attributes and the full-replace save dropped `r.source`, so stamping gave the edge to the restating source.
- A kept contradiction is written after a successful `add_episode` as an `:EdgeInvalidationConflict` record (`status: 'unresolved'`, one per kept/cause edge pair; a repeat bumps `seen_count` and never reopens a resolved record). Entity nodes and the entity conflict groups are untouched. These records are not yet listed by `list_conflicts`.
- Known gap (fork): an edge whose own window is inverted or empty is expired by its own end without asking the hook.
- Tests (new): `tests/test_edge_invalidation_guard.py`, `tests/test_edge_invalidation_guard_online.py`, `tests/test_edge_source_preserved_live.py`. Changed for 0.31.2: test doubles that now receive `query_vector` / `identity_properties` / `max_tokens`, the preserved-attributes expectation in `tests/test_anchored_time_restatement.py`, the restated-attributes assertion in `tests/test_anchored_time_expiry_live.py`, the timeout allow-list, and the `ScoredMemory` field set (adds `provenance`, missed in #375).
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).

## 2026-10-23 - Recall results say who said them; scalar view authority defaults on

For agent-written memories (#168, plan `menhir-agent-memory-writer-plan-2026-10-23` P1b and P2).

- `domain/recall_provenance.py` (new), `domain/recall.py`, `infrastructure/memory_queries.py`, `infrastructure/memory_graph_adapter.py`, `services/recall_pipeline.py`: each Entity, fact-edge and pending-episode result carries `provenance` (`said_by` user / agent / mixed / unknown, `sources`, `first_seen_at`, up to 5 `turn_evidence_ids`). One batched, namespace-scoped query per recall (`fetch_result_provenance`) classifies every contributing episode by its source against `GATE_APPROVED_HUMAN_SOURCES`; best-effort, so a failure leaves it unset. View, history and observation items, nodes with no contributing episode, and verb-facet facts appended later carry none.
- `mcp/formatters.py`, `services/context_builder.py`, `api/routes.py`, `api/routes_support.py`: non-user items get a label (`agent-recorded`, `user + agent`, `provenance unknown`) and the MCP `provenance` key; output for user-stated items and items without provenance is unchanged. The same rule applies to REST `/recall`, `recall_context_memories` and `memory://search`. The receipt source wins over Graphiti's placeholder `message` source; hub-entity turn ids are bounded to the earliest 5 episodes with a 10s query timeout.
- Tests (new): `tests/test_recall_provenance.py`, `tests/test_recall_provenance_pipeline.py`, `tests/test_recall_provenance_rendering.py`, `tests/test_result_provenance_online.py` (online).

- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: `personal_memory_scalar_view_authority_enabled` defaults to `true` (owner decision 2026-10-23, #168). `config/authority_gates.py` still applies it only while `personal_memory_scalar_state_enabled` is on, and the lane stays off by default, so a default install behaves as before; a deployment that turns the lane on now gets View authority without a second flag. Collapse, echo-drop and observation-dedup stay off. Opt out with `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED=false`.
- `docs/agreement-settings.md`, `docs/personal-memory-readiness.md`, `src/menhir/domain/retrieval_tuning.py`, `.agent/default-off-features.md`, `.agent/plans/menhir-scalar-default-enablement-plan.md`: defaults and notes updated. `RecallService`'s own dataclass default stays `False`; bootstrap passes the effective gate.
- Tests: new upgrade test in `tests/test_release_config_upgrade.py` (unset versus pinned) and a default-wiring test in `tests/test_authority_requires_lane.py`. Changed: `DEFAULT_ON` in `tests/test_release_config_upgrade.py` and the shipped value in `tests/test_shipped_agreement_profile.py`.
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).
