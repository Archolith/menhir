# Removed features (2026-10-27)

Eleven default-off experiments were removed from Menhir `main` after the #168 freeze. Each had
been measured and lost, tied, or been deferred. The code is kept on branch
`archive/bucket1-features-2026-10-27` (Menhir and archolith-bench, both pushed), cut at Menhir
`d1105b25` and bench `7b1d798`, before the first removal.

Evidence: workspace `.agent/reviews/menhir-168-off-flag-triage-2026-10-27.md` (section 1).
Plan: workspace `.agent/plans/menhir-remove-bucket1-features-plan-2026-10-27.md`.

Default behavior did not change. Each removed flag was off by default, and each commit was
checked against the pre-removal tree: the resolved settings, `retrieval_tuning()`, the component
defaults and the bootstrap-wired service attributes all match, except for the removed keys.
Every removed setting and env name is listed in `RETIRED` (`config/feature_flags.py`), so Menhir
ignores a stale value and the bench launch guard refuses it.

## Restoring a feature

One commit per feature. To bring a feature back on top of `main`, revert its commit:

```
git revert <commit>
```

Or copy files from the archive branch:

```
git checkout archive/bucket1-features-2026-10-27 -- <files>
```

Then re-add the settings field, the `from_env` parse and the `_SETTINGS` row, and drop the
`RETIRED` keys. A revert conflicts only where later commits touched the same lines, which is
mostly `feature_flags.py`, `settings_model.py` and `.env.example`.

## Features

| Commit | Feature | Flags / env | Result | Decided in |
|---|---|---|---|---|
| `be6e3658` | Oracle subset | `MENHIR_FRONTIER_ORACLE_SUBSET` (env only) | 0.367 vs node-only 0.400 (07-02) | bench `lme-score-campaign.md` |
| `20774682` | Similarity scale "normalized" | `frontier_similarity_scale` | 143/500 for both rrf and normalized, the same set (LME oracle, 07-04); rrf is now the only scale | ledger: "A/B before flip"; content-vector plan section 13 |
| `4f94e6c0` | Fact-edge lane | `frontier_fact_edges`, `frontier_fact_edge_mode`; knobs `enable_fact_edges`, `fact_edge_mode`, `fact_edge_k` | off 9/30, standalone 1/30, pointer 8/30 (07-02) | `reference/anecdotal-recall-oracle-ladder.md` |
| `2f0e0afc` | Content-vector lane | `frontier_content_vector`, `_replace_name`, `_k`, `_weight` | tied the human-anchor primaries | `archive/plans/menhir-content-embedding-recall.md`: CLOSED, NO-GRADUATE (07-14) |
| `46e52607` | BM25 hybrid candidate generation | `frontier_bm25`; knobs `enable_bm25`, `hybrid_alpha` | 0.700 vs 0.710; `wrong_scope` 0.034 to 0.081 | ledger: "neutral-to-negative on LME" |
| `db0da530` | Scalar View collapse | `personal_memory_scalar_view_collapse_enabled` | 10-16 A/B (n=5): off 4/5, collapse alone 4/5, counting on 3/5, counting with collapse 3/5 | collapse drill-down plan: hiding counted quotes hurt; echo drop was split out |
| `32cf81c7` | Scalar canonical self | `personal_memory_scalar_canonical_self` | zero cells in every configuration (bench `3a036d4` replay, 07-27) | bench `config.sh` note |
| `1ca48fee` | Deterministic typed-scalar shadow and router | `personal_memory_scalar_deterministic_shadow` (`MENHIR_SCALAR_DETERMINISTIC_SHADOW`), `_router`, `_classes` | 78-item offline eval: zero compositional identities; failed the zero-router-miss gate (08-06) | bench `ASSESSMENT.md`: "should remain shadow-only. It is not a viable replacement" |
| `155d34be` | Shadow context composition | `shadow_context_composition`, `MENHIR_SHADOW_COMPOSITION_TIMEOUT_S` | real-traffic trace (07-16, 10 rows): exit criteria only partly met | context-composition plan: DEFERRED by owner 10-23 |

## What each commit removed

- **Oracle subset.** Removed the subset filter in `retrieval_oracles.default_oracles()`.
- **Similarity scale.**
  - Removed the normalize block and `_min_similarity` in `recall_pipeline.py`. The floor is
    `MIN_SIMILARITY_THRESHOLD`, as it was under rrf.
  - Removed the `RetrievalTuningConfig.similarity_scale` field and its validation.
  - `GRAPHITI_RRF_DUAL_METHOD_MAX` and `WEIGHTED_RRF_NORMALIZED` stay, because the facet lane uses them.
- **Fact edges.**
  - Removed the pointer hydration and the standalone injection in `recall_pipeline.py`.
  - Removed `GraphitiClient.search_edges_scored`.
  - `_admitted_fact_rows` (shared with verb facets) and the `CandidateSource.FACT_EDGE` and
    `FACT_EDGE_RRF` trace vocabulary stay.
- **Content vector.**
  - Removed the content-vector block, `content_cosine_map` and `content_query_vector` in
    `recall_pipeline.py`.
  - Removed `memory_queries.search_content_embeddings` and its adapter wrapper.
  - Deleted `tests/test_content_vector_retrieval.py`.
  - The episode `content_embedding` writers and `search_episode_embeddings` stay, because the
    source-memory lane uses them.
  - Recall Lab dropped arms B and C.
- **BM25 hybrid.**
  - Removed the `enable_bm25` branch, plus `hybrid_search`, `weighted_rrf` and `_METHOD_SOURCE`
    in `hybrid_retrieval.py`.
  - `weighted_rrf_multi`, `FusionLane` and `HybridCandidate` stay for the facet lane.
  - `search_ranked_by_method` stays for the fused fallback and the trace rank shadow.
  - Recall Lab dropped arm A.
- **View collapse.**
  - Removed the counted-View collapse branch in `recall_pipeline.py`.
  - The observation-drop block is now gated by echo drop alone.
  - `tests/test_scalar_view_collapse_drilldown.py` stays as a fixture hub, and its drop tests run
    under echo drop.
- **Scalar canonical self.**
  - Removed the setting and its threading through runtime, routes, `MaintenanceScheduler` and
    `consolidate_personal_memory`.
  - `ScalarConsolidationConfig.canonical_self` and `CountingConsolidationConfig.canonical_self`
    stay at False, the value the setting always passed.
  - `canonical_self_binding_mode` is a different feature and is untouched.
- **Deterministic scalars.**
  - Deleted `deterministic_scalar_extractor.py` (with `_registry`, `_text` and `_receipts`) and
    `deterministic_scalar_router.py`.
  - In `typed_scalar_service.py`, removed the router block, the shadow pass and its helpers.
  - Removed the scheduler and config threading.
  - Deleted six feature-only test files.
  - `tests/e2e/test_e2e_09_scalar_state.py` lost its router-and-shadow lane.
- **Context composition.**
  - Deleted `services/shadow_context_composition.py` and the two smoke scripts.
  - Removed the dispatch in `enrichment_steps.py`, the shadow LLM methods in `llm.py`, and the
    shadow-task plumbing in the ingest queue, service and worker.
  - The semantic-similarity lab is independent and stays.

## Visible changes

- A Recall Lab request that sends a removed knob gets a 422 (`extra="forbid"`).
- A bench launch that exports a removed name is refused by the retired-flag guard.
- Audit events `deterministic_shadow` and `deterministic_router`, lifecycle rows
  `ingest_shadow`/`extraction_composition` and LLM usage operations `shadow_context_composition*`
  are no longer written. Existing rows stay.

## Bench consumers

Bench launchers no longer export the removed names. The commits are workspace `2df6e70d` and
bench `97adc2b`.

These bench measurement modules are kept. They import the deterministic extractor, so they run
only against a checkout of the archive branch (`MENHIR_ROOT=<archive checkout>`):

- `deterministic_scalar_shadow`
- `compositional_scalar_panel`
- the `scalar_identity_*` panels

Without that checkout, their tests skip (bench `cffdddd`).

The `analysis/*.sh` sweeps (ablation, answer matrix, MSC) still name some removed flags. They are
left as historical launchers.
