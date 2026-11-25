## 2026-11-25 - An entity whose every source memory is retired is retired too

- `domain/recall_visibility.py`: `memory_retired_cypher` is the single definition of retired: the
  stored `memory_retired` marker, or, for an ordinary `:Entity`, at least one `:Episodic` mentions it
  and every one that does is retired. It is derived at read time, so restoring a source or a new
  memory mentioning the entity brings it back with no extra write. Views, flagged and
  retention-protected entities, and entities no episode mentions are never retired this way, so
  retiring sources still never hides a scalar View.
- `cypher.py` (ranked admission), `consolidation_queries.py` (decay, compression, TTL),
  `merge_eligibility.py` and `correlation_queries.py` (merge veto), `tool_event_repository.py`
  (stale anchors) and `flag_memory` read retired state only through the helper.
  `tests/test_memory_retired_reads.py` fails on any other raw read.
- `tests/test_memory_retire_live.py`: single-source and shared entities, a new mention un-hiding an
  entity, flagged and retention-protected entities staying visible, a scalar View staying visible,
  and decay, merge and flag refusing a derived-retired entity until a source is restored.

## 2026-11-25 - Keep corrupt projection work from blocking other targets (#403)

- Projection workers read only their own definition's dirty work. They quarantine corrupt generations while continuing to scan, and report unreadable work keys.
- Re-dirtying a quarantined target can restore its canonical target JSON and schedule a fresh generation.

## 2026-11-25 - Memories can be retired and restored; retired memories leave default recall but stay in history

- `mcp/tools/ingest/retire_memory.py`, `restore_memory.py` (operator tier): retiring is a visibility change
  only. It sets `memory_retired` (with `_at`, `_reason`, `_by`) on an `:Entity` or `:Episodic`, and on both
  halves of an episode pair; it never changes edges, typed assertions, scalar Views, counts or history.
  Restore clears it. Both are idempotent. Retire refuses Views, CANDIDATE memories, episodes still being
  processed, structural nodes, and flagged or retention-protected memories; `flag_memory` now refuses a
  retired memory, so a memory is never both flagged and retired.
- `domain/recall_visibility.py`: `default_recall_visibility_cypher(include_retired=False)`. Default recall,
  pins, listings, the source-memory lane, ranked admission (`_memory_admitted`), pending fallback, facts
  touching a retired memory and stale-anchor labels hide retired memories. `recall_history`,
  `recall_timeline` and the embedding backfill include them; history and timeline results carry
  `retired: true`. `tests/test_memory_retire_census.py` fails on any unclassified call site.
- `consolidation_queries.py`, `merge_eligibility.py`: decay, compression, TTL deletion and merging skip a
  retired memory (`MEMORY_RETIRED` veto). Operator erasure and delete still reach it.
- `tests/test_memory_retire_live.py`: round trip, refusals, every hidden and historical surface, the
  lifecycle freeze, erasure, and retiring a scalar View's sources through the tool.

## 2026-11-25 - Recall ranking strategies and live oracle inputs (#659, #579, #566)

- The cross-encoder rerank now runs as `CrossEncoderRerankOracle` (`services/rerank_oracle.py`)
  behind a new batch oracle contract (`BatchRetrievalOracle`); results carry the raw logit in
  `OracleResult.log_odds`. It runs once per recall, where the rerank ran before. Batch oracle
  timeouts/exceptions mark `failed=True` so `#571` coverage-gap reporting covers batch oracles too.
- New `MENHIR_RECALL_RANKING_STRATEGY` (and Recall Lab knob): `reranker_only` (default, unchanged
  behaviour), `oracle_only` (no rerank: rank floor + oracle residual blend) and `oracle_combined`
  (the oracle combiner orders the reranked head, with the logit as an uncapped `rerank` role;
  candidates past the rerank top N follow the head). Wardens gate every strategy. Without a rerank
  model every strategy runs as `oracle_only`.
- Combiner role weights (`oracle_weight_<role>`, Recall Lab only, default 1.0 = previous scores).
- A traced recall reports `trace.ranking`: per result the strategy, rerank logit, role logits,
  weights, contributions, combiner score and warden decision; Recall Lab shows them per row.
- A non-finite rerank logit now counts as unscored (the item takes the rank floor path).
- `infrastructure/memory_queries.py`, `domain/artifact_role.py`, `services/recall_support.py`,
  `services/recall_pipeline.py` (#579, #566): `fetch_candidate_provenance` projects tenant-scoped
  `:WorkArtifact` `ABOUT` lifecycle and type signals onto recallable `:Entity` candidates (marking
  `artifact_status="historical"` + `superseded_by` only when every linked WorkArtifact is
  `SUPERSEDED`, so `TemporalOracle`, `OracleAdmissionWarden`, and `memory_lifecycle_note` surface
  superseded plans across `recall_memories`, `recall_context_memories`, `build_context`, and REST).
  `_attach_frontier_metadata` now populates `files` and `anchors` from `anchor_paths` and
  `QueryContext.files` from `file_context`, activating `StructureOracle` and anchor-based
  `IntentOracle` role derivation.
- `api/routes_support.py`, `api/routes.py` (#566 CF-320): REST `RecallRequest` now accepts and
  forwards `file_context` and `file_context_project` to `backend.recall`.
- `domain/temporal.py`, `infrastructure/memory_queries.py`, `services/recall_support.py`,
  `services/retrieval_oracles.py` (#579 Items 2-3, #350-safe): `fetch_candidate_provenance`
  projects tenant-scoped `RELATES_TO` fact-edge temporal summaries (`fact_temporal`) dynamically
  onto recallable `:Entity` candidates via `derive_edge_temporal_projection`. Entities with mixed
  active and expired edges stay current (`#350` Cause 1); entities whose same-tenant fact edges
  are all belief-expired carry `all_facts_expired=True` (flagged `historical` under current intent
  and boosted under historical intent, never silently refused); active-edge world-validity bounds
  (`valid_at`/`invalid_at`) project only when every active edge carries them. `QueryContext.as_of_time`
  is now populated from `classify_temporal_intent(query).as_of` in both active and shadow frontier
  passes.

## 2026-11-25 - v0.3.17: separate off/propose/on switches for automatic merging and deletion (#978)

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.17. No dependency changes in this commit.
- Upgrade note: by default automatic merges become proposals and automatic deletion stops. Stop the old
  process before starting this version; 0.3.16 ignores the new switches.

## 2026-11-25 - Automatic merges and deletions each have an off switch (`MENHIR_AUTO_MERGE_MODE`, `MENHIR_AUTO_DELETE_MODE`)

- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: two modes, `off | propose | on`.
  `MENHIR_AUTO_MERGE_MODE` defaults to `propose` and `MENHIR_AUTO_DELETE_MODE` defaults to `off`, so
  an upgraded deployment no longer merges or deletes on its own until the owner sets `on`. An invalid
  or empty value stops startup; a setting that cannot be read later counts as `off`. The switches are
  read from the process environment: set them for every process and restart to change them.
- Deletion `on` has no LLM approval yet: expiry alone authorizes the delete.
- `services/auto_mutation_policy.py` (new): reads the mode at call time and fails closed. In
  `propose` the would-be action is recorded as a `merge_proposal` or `delete_proposal` row in
  `pending_actions` (uuids, numbers and a reason code only) and nothing is changed.
- `services/merge_coordinator.py`, `services/correlation_service.py`: merge is refused unless the mode is
  `on`, and re-checked inside the saga before the mutation. Replay of an interrupted merge that never
  reached the graph then closes it FAILED and releases the pair; one already applied still commits.
  The judge still runs, so contradictions are still queued; a confirmed duplicate is not.
- `services/delete_coordinator.py`, `services/lifecycle_decay.py`, `services/lifecycle_consolidation.py`,
  `infrastructure/episode_maintenance.py`: the TTL sweep, decay deletion and orphan-episode cleanup
  need delete mode `on`.
- Not affected: operator-requested erasure, `delete_entity`, candidate rejection, namespace delete,
  unmerge and rebuilds of derived data. A manual `recover_orphans` obeys the switch.
- `tests/test_auto_mutation_census.py` fails on any new ungated merge or delete path.
- Upgrade note: set `MENHIR_AUTO_MERGE_MODE=on` and `MENHIR_AUTO_DELETE_MODE=on` to keep the old behavior.

## 2026-11-25 - v0.3.16: merge retention, telemetry, hook ordering and release branch fixes (#939 part 2, #959, #964, #965, #956)

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.16. No dependency changes in this commit.
- Upgrade note: stop the old process before starting this version. A 0.3.15 process that is still
  running merges by the old rules and can lower retention (#939).

## 2026-11-24 - Remove dead dev scripts, unused recall imports, unreachable governance code, and deploy script duplication (#789, #792, #793, #794, #644, #660, #802, #803)

- #789: removed the dead doc-token benchmark script under `.agent/tools/` (whose scenario file and `.agent/user/` tree no longer exist) along with its `.agent/README.md` section, `user/` fallback paragraph, and `.agent/file-index.md` entry.
- #792: removed the superseded M2 functional audit probe under `.agent/audit/` (which printed unconditional `CONFIRMED` lines for fixed security findings).
- #793: removed the orphaned QuantState prototype script under `.agent/plans/backlog/` (whose companion plan is archived and fold logic is superseded by `domain/fold_algebra.py` and `domain/scalar_state_fold.py`).
- #794: updated `.agent/audit/` docs and prompt templates to document installing `cth.auditprobe` and running the `auditprobe` entry point with its supported `--type` values (`correctness`, `security`, `architecture`).
- #644: removed 19 unused imports from `services/recall_pipeline.py` while keeping the test-pinned `GRAPHITI_RRF_DUAL_METHOD_MAX` re-export.
- #660 (partial, zero-bench-impact slice): removed unreachable `services/memory_oracle_service.py`, `services/windowed_recall.py`, `WeightedOracleCombiner`, and unused `WardenLabel.UNCERTAIN`/`SUSPICIOUS_CHAIN`; documented `#566`/`#579`/`#659` on kept oracle/warden branches and corrected stale default-off comments in `recall_pipeline.py`, `config/settings_model.py`, and `recall_support.py`. Modules still imported by `archolith-bench` (`structure_temporal`, `exhaustion`/`ExhaustionWarden`, `BeliefScorer`) were intentionally kept.
- #802: removed the duplicate `remote_manifest_digest` call in `deploy/build_release_image.py`, validated `image_id`/`config_sha256` and hoisted `archive_config_for_manifest` before the evidence loop, reused `canonical_sha256`, and unified `EVIDENCE_SPECS`.
- #803: removed unused `ARCHIVE_SHA256`/`ARCHIVE_SIZE` computation and moved symlink checks (covering `$STATUS_DIR`, `${STATUS_DIR}/staging`, `$ARCHIVE_ROOT`, and `$RECEIPT_ROOT`) ahead of `mkdir -p` in `deploy/menhir-backup-local.sh`; collapsed the duplicated required-secret helpers (including OpenAI/Gemini provider branches) and replaced legacy `WORKSPACE_ROOT` with `MENHIR_STATE_DIR` in `deploy/Dockerfile`.
- Updated the release workflow test to allow a single pre-push registry lookup. Behavioral tests cover candidate verification and registry races.

## 2026-11-24 - A merge or unmerge no longer makes a memory easier to delete (#939, part 2)

- `infrastructure/correlation_queries.py`: a merge gives the survivor the higher live scope and clears its
  `ttl_expires`, after locking both nodes so a concurrent promotion is not overwritten. A restored node
  comes back without its old `ttl_expires`; the survivor's scope is not lowered by an unmerge.
- `domain/merge_eligibility.py`: only SESSION and PERSISTENT memories merge. CANDIDATE, missing and
  unknown scopes are refused (`UNMERGEABLE_SCOPE`).
- `infrastructure/consolidation_queries.py`, `infrastructure/memory_graph_adapter.py`,
  `services/delete_coordinator.py`: the TTL delete re-checks expiry in the delete statement.
- `infrastructure/graph_text_inventory.py`: registers the transient lock key.
- Not retroactive: survivors that already carry a `ttl_expires` keep it.

## 2026-11-24 - File events stay in their project; stale receipts are ordered by instant (#964, #965)

- `infrastructure/tool_event_repository.py`: an event from an indexed project no longer marks another
  project's files. An event from an unknown project marks files only when every match belongs to one
  project. Verification receipts are compared as instants and count no later than their server
  `created_at`; the receipt list is ordered by `created_at`. Files already marked are not cleared.
- `scripts/maintenance/report_stale_verifications.py`: same receipt rule.
- `docs/hook-center-tool-events.md`: describes both rules.
