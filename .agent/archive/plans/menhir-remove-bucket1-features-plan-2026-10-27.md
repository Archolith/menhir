---
artifact_schema: 1
artifact_uuid: c2e20ee1-96e0-48a9-93b8-3aa6802c3f87
artifact_type: plan
artifact_status: IMPLEMENTED
---

# Menhir: remove the bucket-1 features and archive them (2026-10-27)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Disposition: IMPLEMENTED. The removal shipped on 2026-10-27; see [`../removed-features-2026-10-27.md`](../removed-features-2026-10-27.md). The plan below is kept as drafted.

Authority: owner, 2026-10-27: "keep the work somewhere but remove from menhir". Answers: "Bucket 1
only", "Archive branch + index". Evidence per feature: `.agent/reviews/menhir-168-off-flag-triage-2026-10-27.md` §1.

Base: Menhir `main` d1105b25, bench `master` 7b1d798. Line numbers below come from read-only traces
at those commits; re-grep before each edit.

## Features (11 flags, 9 removal units)

| Unit | Flags and knobs removed | Result that retired it |
|---|---|---|
| U1 oracle subset | MENHIR_FRONTIER_ORACLE_SUBSET (env-only) | 0.367 vs node-only 0.400 |
| U2 similarity scale | frontier_similarity_scale (the "normalized" mode; rrf stays as the only behavior) | 143/500 both modes, same set and order |
| U3 fact edges | frontier_fact_edges, frontier_fact_edge_mode, knob fact_edge_k | off 9/30, standalone 1/30, pointer 8/30 |
| U4 content vector | frontier_content_vector, _replace_name, _k, _weight | NO-GRADUATE 2026-07-14 |
| U5 BM25 hybrid | frontier_bm25, knob hybrid_alpha | 0.700 vs 0.710, wrong_scope 0.034 -> 0.081 |
| U6 View collapse | personal_memory_scalar_view_collapse_enabled | 10-16 A/B: collapse hurt; echo drop split out and stays |
| U7 scalar canonical self | personal_memory_scalar_canonical_self | zero cells in every configuration |
| U8 deterministic scalars | personal_memory_scalar_deterministic_shadow, _router, _classes | zero compositional identities; "not a viable replacement" |
| U9 context composition | shadow_context_composition, MENHIR_SHADOW_COMPOSITION_TIMEOUT_S | DEFERRED by owner 10-23 |

## Archive

1. Before the first removal commit, create the branch `archive/bucket1-features-2026-10-27` at the
   Menhir `main` tip, and the same branch name in the bench at the `master` tip (the bench loses
   the modules that measure U4, U5 and U8). Local only until a push is approved. Local branches
   do not survive a reclone, so push both when approved.
2. Each unit lands as one commit on `main`, so `git revert <sha>` restores a unit, and the archive
   branch keeps the whole pre-removal tree.
3. Index doc: `menhir/.agent/archive/removed-features-2026-10-27.md`. One section per unit:
   - flags and env names;
   - result and decision source (copied from the triage);
   - files deleted and files edited;
   - the archive branch, the removal commit sha, and how to restore (`git revert <sha>`, or
     `git checkout archive/bucket1-features-2026-10-27 -- <files>` plus the settings and registry
     rows);
   - bench modules removed with it, if any.
4. `menhir/.agent/default-off-features.md`: add a "Removed 2026-10-27" section linking the index,
   and drop the 11 rows from "Active default-off features".

## Removal units

Each unit also does three things:
- removes the `_SETTINGS`/`_ENV_ONLY` rows in `config/feature_flags.py`;
- adds every setting name and env name to `RETIRED` as
  `"2026-10-27: removed; archived on archive/bucket1-features-2026-10-27"`, which
  `test_feature_flag_registry.py:112-117` requires;
- removes the fields and `from_env` parses in `config/settings_model.py` and the lines in `.env.example`.

Order: U1-U3, then U4 before U5, because the content-vector block reads `enable_bm25`.

**U1 oracle subset.** Delete `services/retrieval_oracles.py:290-298`, plus the `os` import if it
becomes unused. In `feature_flags.py`, delete the `:185` row and the `:230` `recall_affecting`
special case. Tests: the one line each in `test_warden_safety_matrix.py` and
`test_warden_verifier_interactions.py`.

**U2 similarity scale.**
- `recall_pipeline.py`: delete the normalize block at 875-897 and `_min_similarity` at 2225-2231.
  Call `score_candidates(..., min_similarity=MIN_SIMILARITY_THRESHOLD)` directly.
- `domain/retrieval_tuning.py`: delete the validation at 362-366 and fix the comments at 88, 105 and 133.
- `settings_model.py`: delete 414 and the mappings at 733 and 964.
- Tests: delete `test_tuning_config_rejects_unknown_similarity_scale`.
- Keep `GRAPHITI_RRF_DUAL_METHOD_MAX` and `RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED`, which the
  facet lane uses.

**U3 fact edges.**
- `recall_pipeline.py`: delete the pointer block (943-1013 and on) and the standalone injection (2035-2090).
- `infrastructure/graphiti_client.py`: delete `search_edges_scored` (858). Its only caller is the
  pointer block.
- Delete the `fact_edge_search` phase timing.
- `RetrievalTuningConfig`: delete the knobs.
- Keep `_admitted_fact_rows` (verb facets uses it), `frontier_fact_relevance` and `frontier_verb_facets`.
- Tests: edit the `search_edges_scored` stubs in `test_recall_service.py` (687-730).

**U4 content vector.**
- `recall_pipeline.py`: delete the `if tuning.enable_content_vector:` block (648-728) and
  `content_cosine_map` (637, 702, 1073). Drop the `content_query_vector` parameter of
  `_build_source_memories`. It is already `None` by default, so the lane embeds the query itself.
- Delete `memory_queries.search_content_embeddings` (384-406) and its adapter wrapper
  (`memory_graph_adapter.py:977-984`).
- Delete `tests/test_content_vector_retrieval.py`.
- Keep the episode `content_embedding` writers (`episode_lifecycle.py:253`,
  `enrichment_steps.py:861`), `source_embedding_repair.py`, and `search_episode_embeddings`. The
  source-memory lane shares the data with this lane, not the query.

**U5 BM25 hybrid.**
- `recall_pipeline.py`: delete the `elif tuning.enable_bm25` branch (729-741).
- `services/hybrid_retrieval.py`: delete `hybrid_search`, `weighted_rrf` and `_METHOD_SOURCE`.
  Drop `hybrid_search` from the re-exports in `recall_policies.py:47`, `recall_service.py:44` and
  `recall_support.py:46`.
- Keep `weighted_rrf_multi`, `FusionLane`, `HybridCandidate` and `_admission_source` (the facet
  lane at `recall_pipeline.py:817` uses them).
- Keep `graphiti_client.search_ranked_by_method`, which serves the fused-search fallback (750) and
  the default trace rank shadow (756).
- Tests: in `test_hybrid_retrieval.py`, delete the `weighted_rrf`/`hybrid_search` tests and keep
  the `weighted_rrf_multi` tests.

**U2-U5 shared surfaces.**
- `RetrievalTuningConfig` loses the knobs.
- `explorer/recall_lab.py`:
  - `RecallLabTuning` loses the fields;
  - `DEFAULT_ARMS` drops arms a, b and c, which exist only to set them;
  - `explorer/static/recall-lab.js:10-23` drops the controls.
- `CandidateSource.BM25`, `CONTENT_VECTOR`, `FACT_EDGE` and `RetrievalScoreKind.FACT_EDGE_RRF`
  stay as vocabulary unless a grep for value parsing (`CandidateSource(`, trace readers in
  `retrieval_trace_models.py` and `scoring_service.py`) shows no reader of stored traces. Their
  priors and precedence rows stay with them.
- Tests to edit:
  - `test_settings.py`;
  - `test_recall_lab.py`;
  - `test_recall_service.py`;
  - `test_release_config_upgrade.py` (`CAMPAIGN_FLAGS`);
  - `tests/e2e/_harness/features.py` (142-153, 291-293);
  - the comment in `tests/conftest.py`.

**U6 View collapse.**
- `recall_pipeline.py`: delete the collapse branch at 1713-1722 and keep its `else` body
  unconditionally.
- Reduce the condition at 1927-1929 to `scalar_view_echo_drop_enabled`, and reword the comments
  and log text to "echo drop".
- Delete the field at `recall_service.py:97`, the kwargs at `core/bootstrap.py:243` and
  `cli/bootstrap.py:73`, and the `requires` and note rows at `feature_flags.py:199` and 207.
- Keep the echo-drop plumbing.
- Tests: `test_scalar_view_collapse_drilldown.py` is a fixture hub for nine files, so edit it
  rather than delete it:
  - convert the observation-drop tests (98-136) to `scalar_view_echo_drop_enabled=True`, so echo
    drop does not lose coverage;
  - delete the counted-contributors-hidden tests (141-167);
  - update the docstring.
- Tests: in `test_feature_flag_registry.py:124`, drop the key.

**U7 scalar canonical self.**
- Delete the setting path in these places:
  - `core/runtime.py:466-467`;
  - `api/routes_handlers.py:88-89`;
  - `maintenance_scheduler.py:130`, 134 and 936;
  - `scheduler_tasks.py:558`, 776 and 834;
  - the table row in `docs/agreement-settings.md:18`.
- Keep the `canonical_self=False` fields on `ScalarConsolidationConfig` and
  `CountingConsolidationConfig`. They stay inert at that default, and the research and
  structural composer paths pass `True`.
- Do not touch `canonical_self_binding_mode` or `bind_canonical_self`.
- Tests to edit:
  - `test_gate_relaxations.py:438-439`: make it a `not hasattr` retirement assertion;
  - `test_shipped_agreement_profile.py:44`, 69 and 81;
  - `test_settings_scalar_deterministic_shadow.py:259` (deleted in U8 anyway).

**U8 deterministic scalars.**
- Delete these files:
  - `services/deterministic_scalar_extractor.py`;
  - `_registry.py`;
  - `_text.py`;
  - `_receipts.py`;
  - `deterministic_scalar_router.py`.
- `typed_scalar_service.py`:
  - delete the router block (760-863), `_merge_router_decisions` (1051-1068), the shadow call
    (919-936) and `_run_deterministic_shadow` (987-1049);
  - delete the shadow-only helpers (about 55-61 and 84-567);
  - delete both extractor/router import blocks (16-28);
  - replace `llm_episodes` with `episodes`;
  - reduce line 881 to `[_extract_once() for _ in range(k)]` and lines 914-916 to
    `decisions = llm_decisions`;
  - delete `_unique_episode_source_map` and the composer-version import if they are unused after
    that.
- Keep `_proposal_audit_summary` and the default `extract` audit event.
- Delete the threading:
  - `core/runtime.py:470-475`;
  - `routes_handlers.py:94-99`;
  - `maintenance_scheduler.py:141-144` and 940-942;
  - `scheduler_tasks.py:565-567`, 595-600 and 770-772;
  - `scalar_consolidation.py:119-123` and 201-203;
  - the constructor at `typed_scalar_service.py:644-661`;
  - `feature_flags.py:216` and 223-224.
- Read `deterministic_scalar_router._to_decisions` first, to confirm it writes no persisted marker.
- Tests to delete:
  - `test_deterministic_scalar_router.py`;
  - `_routing_service.py`;
  - `_shadow.py`;
  - `_extractor.py`;
  - `test_settings_scalar_deterministic_shadow.py`;
  - `_router.py`.
- Tests to edit:
  - `test_cf92_clock_value_contracts.py`: keep the composer `_clock_value_or_none` half;
  - `test_compositional_scalar_structural.py:184`: replace the extractor input generator with
    fixture inputs;
  - `test_freeze_scalar_samples.py:11` and 123;
  - `test_feature_flag_registry.py:127`;
  - `test_release_config_upgrade.py:29`;
  - `test_shipped_agreement_profile.py:56`;
  - `e2e/test_e2e_09_scalar_state.py:173-180` and 243.
- Check the constructor kwargs in `test_counting_reversion_safety.py`.

**U9 context composition.**
- Delete `services/shadow_context_composition.py` and the two `scripts/smoke/shadow_context_composition*_smoke.py`.
- In `enrichment_steps.py`, delete the import (68-73), the fields (119-124), the snapshot (556-579)
  and the dispatch (730-804).
- In `llm.py`, delete the two shadow prompts, their builders, `classify_shadow_context` and
  `break_shadow_tie` (435-478).
- Delete the shadow parts of these files:
  - `ingest_queue.py:81-82`, 122-134 and 427-438;
  - `ingest_service.py:122-134`;
  - `ingest_worker.py:180-182`;
  - `core/bootstrap.py:227-228`.
- Keep `shadow_semantic_similarity_lab.py`, its script and its tests. They are independent, and
  mention the feature only in docstrings, which are reworded.
- Keep `register_background_task` only if the namespace-boundary tests still need it.
- Tests:
  - delete `test_shadow_context_composition.py`;
  - delete `test_settings.py:158-175`;
  - delete `test_verdict_budget_203.py:63-65`;
  - delete the shadow tests in `test_services_pipeline.py` (3443-3527);
  - rename `_build_shadow_test_ctx` and `_FakeGraphitiClientForShadow` (still used at 3565-3615);
  - rework `test_self_endpoint_collision.py:148-160` without the shadow assertions;
  - `test_cf127_tenancy_scope_predicate.py:98`;
  - `tests/e2e/_harness/features.py:191-195`.
- Old lifecycle rows (`ingest_shadow`/`extraction_composition`) and the LLM usage operation names
  simply stop appearing.

## Bench and workspace consumers (separate commits, after the Menhir units)

Bench (`archolith-bench`):
- U8: delete these files:
  - `archolith_bench/deterministic_scalar_shadow.py`;
  - `compositional_scalar_panel.py`;
  - `scripts/measure_deterministic_scalar_shadow.py`;
  - `tests/test_deterministic_scalar_shadow.py`.
  They import the deleted extractor.
- U8: move `resolve_menhir_root` and any other helper that `scalar_identity_acceptance_panel.py:22`,
  `archolith_bench/event_history_acceptance.py:27` and
  `scripts/longmemeval/analysis/event_history_acceptance.py:44` import, into a small shared module.
- U8: delete the deterministic lines in `run_knowledge_update_buildout.sh` (71-72, 257-259, 333-334)
  and in `tests/test_longmemeval_ingest_fixture.py:223-224`.
- U4/U5: delete `scripts/run_content_vector_benchmark.py` and `tests/test_content_vector_benchmark.py`.
  Delete the E_hybrid sweep in `scripts/run_r1_live.py:129-131` and `run_r1_dummy.py:128-130`,
  which build `RetrievalTuningConfig(enable_bm25=...)`. Keep the R1 `bm25_rank` trace fields,
  which the default trace still emits.
- U3: in `scripts/longmemeval/analysis/ablation_sweep.sh:44`, drop `MENHIR_FRONTIER_FACT_EDGES`.
- U7:
  - delete the `LME_SCALAR_CANONICAL_SELF` lines in `config.sh:193`, `build_graph.sh:191` and
    340, and `retry.sh:103`;
  - delete the entry in `test_lme_retry_parity.py:234` (`INGEST_ENV`);
  - fix `README.md:110`.
  Check `run_provenance.py` first: if `scalar_canonical_self` is a recorded or immutable key,
  drop it the way the freeze dropped attach and expiry, and keep old provenance resumable.
- Results and benchmark notes stay as history.

Bench launcher scripts that exported the removed names were updated too, because the
retired-flag guard refuses them.

## No regression

The owner's rule: no change to approved behavior. Every flag here is off by default, so removing
it must leave the default path unchanged.

1. **Equivalence.** Run an effective-config script on the base tree and on each unit's tree,
   with no env set. They must resolve to the same:
   - `MemorySettings` fields, except the removed ones;
   - `retrieval_tuning()` output, except the removed knobs;
   - component constructor defaults;
   - recall service attributes from both bootstraps.
2. **Default-path code.** The diff review confirms that each deleted branch was guarded by a
   removed flag being on. The kept `else` bodies (U2, U6) and the reduced expressions (U8: lines
   881 and 914-916) must be the exact code the flag-off path ran.
3. **Echo drop.** The converted U6 tests pass with `scalar_view_echo_drop_enabled=True` and fail
   if the drop block is removed.
4. **Deployment config.** A deployment that still sets a removed name has it silently
   ignored, which is a behavior change only if it was on. Check deployment config before the
   removal ships.

Visible changes, by design:
- Recall Lab requests that send a removed knob get a 422 (`extra="forbid"`).
- Bench launches that export a removed name are refused by the guard.

## Verification

- Per unit, run the focused Menhir tests for the touched files, plus `test_feature_flag_registry.py`
  and `test_settings.py`. Also run a `src` grep that shows no removed name outside `RETIRED`.
- Once at the end, run the full Menhir suite.
- Bench: run the focused tests (`test_lme_retry_parity.py`, `test_lme_retired_flags.py`, and the
  tests of the moved helper), and run the guard against the updated arms.
- No paid runs. Unit removals change no default behavior, so none are needed.

## Out of scope

- Buckets 2-5.
- The deferred launchers from the freeze plan (except the lines named above).
- Pushing anything without approval.

## Open decisions

1. Bench: delete the extractor-dependent measurement modules with U8 (proposed). The other choice
   is to vendor the extractor into the bench.
2. Push the two archive branches once created (proposed, because local branches are lost on a reclone).
