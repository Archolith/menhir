# Script Index (menhir + archolith-bench)

**Read this before writing a script.** Two sessions in a row re-derived results that an existing
instrument already produced, because there was no index: you grep, find nothing under the name you
guessed, and write `_probe_<today>.py`. That is how this repo accumulated 25 throwaway probes.

Scope note: menhir's measurement instruments are SPLIT across two repos. The scalar/View coverage
family lives in `archolith-bench` because it imports that repo's fixtures. Always check both tables
below before concluding something does not exist.

## The naming convention (load-bearing)

| Prefix | Meaning | Indexed? | Lifetime |
|---|---|---|---|
| `_name.py` | throwaway: answered one question, on one graph, once | no | delete once its finding is written down |
| `name.py` | durable instrument: re-runnable, re-usable | yes, below | maintained |

A `_`-prefixed script is **deletable on sight** and does not need a deprecation path. If you find
yourself wanting to keep one, that is the signal to drop the underscore and add a row here.

Do not add a `_` script to answer a question one of the durable instruments already answers.

## Where to look first, by question

| Question | Instrument | Repo |
|---|---|---|
| did the scalar/View path work? (4-stage coverage) | `workflows/scalar_state_measurement.md` -> `scalar_state_coverage.py` | bench |
| does deterministic extraction agree with the frozen LLM gate, and what conservative scalar call savings would it imply? | `archolith-bench/scripts/measure_deterministic_scalar_shadow.py` (offline; paired with Menhir `scripts/freeze_scalar_samples.py` and the pre-registered static held-out smoke fixture) | bench |
| does deterministic compositional identity match independent source-authored semantics without treating an LLM as gold? | `archolith-bench/scripts/measure_compositional_scalar_panel.py` (offline; paired with the versioned non-LME generic panel) | bench |
| does an offline research scalar candidate survive Menhir grounding and structural identity without entering runtime or persistence? | `archolith-bench/scripts/measure_scalar_identity_acceptance_panel.py` (offline; paired with `fixtures/scalar_identity_acceptance_v1.json`) | bench |
| do clean and noisy conversational scalar forms preserve identity without adding false-current state? | `archolith-bench/scripts/measure_scalar_identity_noisy_panel.py` (offline; paired with `fixtures/scalar_identity_noisy_v1.json`) | bench |
| does mapped clause normalization improve scalar identity versus the unchanged canonical research adapter? | `archolith-bench/scripts/measure_scalar_identity_isolated_comparison.py` (offline; baseline/isolated comparison over `fixtures/scalar_identity_noisy_v1.json` and the cumulative-completion `fixtures/scalar_identity_cumulative_v1.json`) | bench |
| does parser-neutral dependency evidence survive Menhir's source-bound validation and conservative Phase-A scalar rules? | `archolith-bench/scripts/measure_dependency_scalar_bridge.py` (offline; paired with `fixtures/dependency_evidence_bridge_ops_v1.json`) | bench |
| what scalar calls/artifacts and recall evidence are present in a historical run, and what answer-token cost was persisted? | `archolith-bench/scripts/measure_scalar_spend_attribution.py` (offline, read-only) | bench |
| why did THIS namespace bind nothing? | `inspect_scalar_state_graph.py --ns-prefix X --all` | bench |
| is this LME question a real retrieval miss or a scoring artifact? | `longmemeval/analysis/lib/recall_lab_investigate.py` (LLM judge) | bench |
| gold@k / support@k across many LME questions, no LLM spend | `longmemeval/analysis/lib/retrieval_quality.py` | bench |
| did an ingest change alter end-to-end answer accuracy? | `longmemeval/analysis/answer_ab.sh` | bench |
| does a full or query-filtered typed Recall Lab packet change KU78 answer accuracy without reingest? | `archolith-bench/scripts/longmemeval/analysis/run_typed_recall_packet_rescore.py` (noncanonical, recall-only, paid answer/judge calls) | bench |
| which compact typed packet shape performs better on the preregistered held-out panel? | `archolith-bench/scripts/longmemeval/analysis/run_packet_shape_panel.py` (noncanonical, recall-only, paid answer/judge calls) | bench |
| what did enrichment do to ONE episode? | `get_episode_trace` MCP tool (not a script) | menhir |
| queue stalled / episodes stuck | `workflows/troubleshoot_enrichment_stalls.md` | menhir |
| a batch of episodes is stuck FAILED, fix has landed | `retry_failed_episodes.py` | menhir |
| does typed-scalar extraction ever propose value V? | `probe_scalar_extraction.py` | menhir |
| would a different gate/fold flag change the committed Views? | `replay_gate_flags.py`, `replay_fold_flags.py` | menhir |

## menhir/scripts

### Production hooks (wired into live agents -- do not delete)
`hooks/menhir_turn_evidence.py` (Claude), `hooks/menhir_codex_turn_evidence.py`,
`hooks/menhir_opencode_turn_evidence.py`, `hooks/menhir_turn_evidence_common.py` (shared core),
`hooks/menhir_file_event.py`, `hooks/menhir_policy_guard.py`,
`opencode-plugin/menhir-turn-evidence.js`. ADR 0001 producers.

### Operations and one-time migrations
| Script | Purpose |
|---|---|
| `deploy/release_flow.py` | Resumable, digest-bound `prepare` / `finalize` / `status` / `deploy` coordinator; deploy is preview-only without exact confirmation plus `--execute` |
| `deploy/release_spec.py` | Strictly validates release inputs and generates the maintained four-repository release-author specification |
| `deploy/release_notes.py` | Validates committed change fragments and deterministically renders release Markdown or JSON |
| `deploy/build_install_bundle.py` | Builds and revalidates the exact reviewed host installation bundle from committed blobs and rendered artifacts |
| `deploy/release-install.sh` | Root-only transactional bundle installer with an independent destination allowlist and rollback of replaced files; does not start cutover |
| `run_mcp_gateway.py` | stdio launcher for the MCP server |
| `export_graph_backup.py` / `_tolerant.py` | logical graph backup; the tolerant variant survives corruption |
| `migrate_schema_v_property.py` | dry-run-first `_yawn_schema_v` removal and replacement-index migration. Remote writes require `--allow-remote`; configured production targets require `--allow-production` after a verified backup |
| `unmerge.py` | operator CLI to reverse a merge |
| `migrate_namespace_default.py` | one-shot: normalize legacy nodes into the default namespace |
| `migrate_flagged_bootstrap_scope.py` | one-time recall-hygiene classification cutover |
| `repair_embedding_dimensions.py` | fix mismatched embedding dims |
| `backfill_merge_audit.py` | copy graph merge_audit into the telemetry sidecar |
| `metric_recapture.py` | recapture instrumentation :Entity view nodes as :Metric |
| `repair_lme_valid_at.py` | restore conversation-time valid_at on the LME corpus |
| `repair_lme_scalar_source_times.py` | dry-run-first repair of TurnEvidence/assertion world time from a frozen LME fixture, followed by deterministic ScalarStateView rebuild; requires verified graph snapshot to apply |
| `backfill_admitted_on.py` | backfill `(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)` on a pre-existing graph. REQUIRED before typed-scalar binding can resolve a turn_id on any corpus built before that edge existed. Dry-run by default; remote writes require an explicit acknowledgement |
| `retry_failed_episodes.py` | bulk re-enrich FAILED episodes in paced waves after a root-cause fix lands. Dry-run by default; `--apply` required, `--error-contains` filters by cause. Operator-tier (needs `MENHIR_OPERATOR_KEY`). Verify the fix on ONE episode first — a bulk pass against an unfixed cause burns every retry counter for nothing |
| `setup_remote_test_neo4j.sh` | stand up the dedicated TEST Neo4j (port 7688) |
| `maintenance/*.py` | dirty-file, stale-anchor, and stale-verification reporting |

### Scalar / typed-assertion instruments
| Script | Purpose |
|---|---|
| `probe_scalar_extraction.py` | READ-ONLY: does extraction ever propose a given value? Accepts full `lme-*` namespaces and `--only-episode` isolation. |
| `probe_scalar_proposer_reviewer.py` | live panel across proposer/reviewer arms A-D |
| `score_proposer_reviewer_gates.py` | score a capture against pre-registered go/no-go gates |
| `freeze_scalar_samples.py` | freeze k extraction samples per exact graph namespace or pre-registered static episode JSON (measure only; fail-closed) |
| `replay_gate_flags.py` | re-gate a frozen capture through the REAL parser and gate |
| `replay_fold_flags.py` | score gate configs on the FINAL View, not on what the gate committed |
| `gate_threshold_ab.py` | 3/3 vs 2/3 shadow A/B, offline, same-samples |
| `measure_absence_band.py` | absence-band anatomy (measure only) |
| `span_consistency_probe.py` / `eval_span_alignment.py` | span grounding + alignment scoring |
| `lme_ground_truth.py` | LME knowledge-update ground truth + value matching |

### Extraction lab (all tied to `menhir-extraction-context-ablation-handoff.md`)
`run_extraction_lab_phase1.py`, `_phase1_variance`, `_phase2_context_ablation`,
`_phase3_schema_limit`, `_phase4_selector`, `_phase4b_eligibility`, `_phase5_metadata`,
`_phase5_ranked`, `_phase5_genuine_ties`, plus `analyze_phase5_abstentions.py`.
Read the handoff doc before running any of them; they are phases of one experiment, not standalone.

### Shadow labs (real LLM/embedding spend)
`run_shadow_semantic_similarity_lab.py`, `run_shadow_llm_judge_lab.py`,
`run_shadow_contrastive_judge_lab.py`. These make real API calls -- check cost before running.

### Smoke tests
`smoke/run_all.py` runs every self-serving smoke. Auth/OAuth: `auth_shapes_smoke.py`,
`oauth_local_smoke.py`, `auth0_live_smoke.py`. Hook Center: `hook_center_live_smoke.py`,
`hook_center_stale_lane_smoke.py`. Extraction regressions: `suburbs_extraction_live_smoke.py`,
`trace_suburbs_grounding.py`, `replay_trial10_dedup.py`, `replay_edge_invalidation.py`,
`test_dedup_prompt_patch.py`, `test_truncation_fix_live.py`. Shadow composition:
`shadow_context_composition_smoke.py` (+ `_broad_`). Helper: `smoke/_server.py`.

### Auth / dev tooling
`dev/auth0_provision.py`, `dev/auth0_diagnose.py`, `dev/auth0_token_probe.py`,
`dev/test_server.py` (throwaway server in a selectable auth shape).

### Recall and audit analysis
`profile_recall.py` (latency phases), `analyze_recall_lab_scores.py`,
`inspect_audit_trail.py` / `inspect_consolidation_audit.py` (replay a run's telemetry DB),
`validate_phase3_realdata.py` (hook triage -> TurnEvidence -> Views -> recall),
`probe/probe_sharpness_cosine_floor.py`, `run_suburbs_extraction_gate.py`.

### Surviving `_` scripts (cited by a doc or another script -- do not delete blindly)
`_calibrate_combiner.py`, `_clone_to_dummy.py`, `_dump_residual_losses.py` (used by
`tests/test_gate_relaxations.py`), `_integ_reference_time.py`, `_measure_anchor_quality.py`,
`_measure_facet_generator.py`, `_replay_entities.py` (used by `replay_fold_flags.py`),
`_verify_valid_at_repair.py` (used by `repair_lme_valid_at.py`).

## archolith-bench/scripts

### menhir ScalarStateView instruments
Full detail in [`workflows/scalar_state_measurement.md`](workflows/scalar_state_measurement.md).
`scalar_state_coverage.py`, `run_scalar_state_e2e.sh`, `inspect_scalar_state_graph.py`,
`scalar_view_authority_live.py`, `scalar_leads_authority_live.py`, `scalar_phase_d.py`.
Plus `inspect_owned_provenance.py` (why the `owned` slot returns row_count=0) and
`diagnose_mentions_provenance.py` (entity->episode MENTIONS provenance after each ingest).

`measure_deterministic_scalar_shadow.py` is the **Bench-owned, offline** deterministic-shadow
instrument. It consumes only captures from Menhir `scripts/freeze_scalar_samples.py`, reruns the
real Menhir proposal/gate/extractor/comparator logic, and is separate from the live graph tools.

`measure_compositional_scalar_panel.py` is the **Bench-owned, offline** independent semantic
instrument. It binds source-authored positive/negative labels to exact source hashes and locators,
runs the real deterministic extractor plus structural composer, and never uses an LLM answer or
proposal as gold. Its bounded panel is regression evidence, not a promotion gate.

`measure_scalar_spend_attribution.py` is the **Bench-owned, offline** historical scalar-spend
attribution instrument. It consumes a run directory plus a recall checkpoint, opens telemetry SQLite
with `mode=ro`, validates manifest/provenance/checkpoint integrity, and writes only explicitly
requested JSON and Markdown outputs. It does not claim scalar-caused corrections or scalar spend.

`measure_scalar_identity_acceptance_panel.py` is the **Bench-owned, offline** research-candidate
panel. It evaluates source-bound raw candidates through Menhir's pure research adapter and
structural composer, reporting parser admission separately from composition status. The bounded
non-LME fixture is regression evidence only, never a production gate.

`measure_scalar_identity_noisy_panel.py` is the **Bench-owned, offline** noisy-language comparison.
It runs clean and informal cases as separately reported slices through Menhir's research adapter,
reporting paired invariance, perturbation coverage, and false-current-state errors as regression
evidence without LLM truth.

`measure_scalar_identity_isolated_comparison.py` is the **Bench-owned, offline** isolation comparison
runner. It runs the same fixture through both canonical and mapped-isolated research adapters,
reporting identity differences and composition gains without claiming a production gate.

`measure_dependency_scalar_bridge.py` is the **Bench-owned, offline** evidence-bridge validator.
It strictly verifies a 48-case fixture, builds candidates from supplied subject/operation/value/claim,
and loads Menhir's real adapter and Phase-A dependency rules—purely offline with no LLM, network,
or runtime calls.

### LongMemEval framework
One dispatcher: `longmemeval/lme.sh` (`-h` for all verbs), one config: `longmemeval/config.sh`,
one runbook: `longmemeval/README.md`. Start there, not at the individual scripts.

| Script | Purpose |
|---|---|
| `longmemeval/build_graph.sh` | build the persistent pre-ingested graph |
| `longmemeval/lib/ingest.py` | resumable haystack ingest (imports `claim_segmenter.py`) |
| `longmemeval/lib/run_provenance.py` | append-only build/resume attempt and phase provenance; canonical mode refuses commit drift, `--noncanonical` relaxes |
| `longmemeval/lib/validate_run.py` | final acceptance report: manifest cardinality, failed episodes, projection counts, namespace isolation, commit immutability, telemetry presence; wired as `lme.sh validate` |
| `longmemeval/lib/backfill_dates.py` | backfill world-time valid_at from real session dates |
| `longmemeval/lib/expected.py` | precompute expected turn counts for progress |
| `longmemeval/lib/retry.py` / `retry.sh` | re-enrich FAILED episodes |
| `longmemeval/status.sh` | read-only ingest progress |
| `longmemeval/snapshot_graph.sh` / `backup_graph.sh` | volume snapshot / dump+load |
| `longmemeval/promote_persistent.sh` | SESSION -> PERSISTENT scope promotion |
| `longmemeval/recall_ab.sh` | recall-only A/B against the pre-built graph |
| `longmemeval/buildout_ab.sh` | ingest the same slice on main vs frontier into two fresh graphs |
| `longmemeval/run_date_smoke.sh` | one-item proof that ingest backdating works |
| `longmemeval/run_suburbs_fixture.sh` | build+verify the Rachel/Chicago/suburbs regression fixture |

**Knowledge-update buildout arms** (five variants of one experiment -- pick by segmentation mode):
`run_knowledge_update_buildout.sh` (78-item base), `run_ku_nosplit.sh` / `run_ku_nosplit_full.sh`
(no sentence splitting), `run_ku_split15.sh` (sentence splitting, 15 items),
`run_ku_adaptive.sh` / `run_ku_adaptive_full.sh` (adaptive claim segmentation).
Update `results/lme-ku-buildout/LEDGER.md` after every buildout run.

**Analysis** (`longmemeval/analysis/`):
| Script | Purpose |
|---|---|
| `lib/retrieval_quality.py` | deterministic gold_rank / support_rank, no LLM spend, scales to n=500 |
| `lib/recall_lab_investigate.py` | gold-aware LLM judge on a sample -- tells a real miss from a token-overlap artifact |
| `answer_ab.sh` / `.py` | do committed Views improve END-TO-END answer accuracy? |
| `answer_matrix.sh` | stratified accuracy matrix, retrieval vs generation |
| `brief_ab.sh` / `.py` | does the BriefBuilder brief beat the flat brief? |
| `entropy.sh` / `.py` | D0 retrieval-entropy instrument, deterministic, GPT-free |
| `perception_write.py` / `perception_delta.py` / `perception_tune.py` | Arm C gated-perception write + delta + threshold tuning |
| `capstone.sh` | Arm C end-to-end Event->Fold->View measurement |
| `msc_sweep.sh` / `ablation_sweep.sh` | minimal-sufficient-context and per-oracle ablation sweeps |
| `acquisition_window.py` | windowed acquisition counts over the D0 counting slice |
| `reflection_rescue_survey.py` | survey assistant turns in the KU fixture |
| `oracle_entity_grouping_probe.py` | oracle entity-grouping sidecar experiment |
| `run_typed_recall_packet_rescore.py` | noncanonical recall-only typed packet vs canonical checkpoint comparison; refuses incomplete model evidence and records task/token/provenance deltas |
| `run_packet_shape_panel.py` | preregistered ten-item held-out comparison of compact typed packet shapes over shared production retrieval |

**Stratification warning:** a bare `--limit N` samples only `temporal-reasoning`. A fair run must
sweep all 6 question types via `--subset`. See the runbook's Stratification section.

### Research ladders (R1-R7, facet, oracle)
`run_r1_bench.py`, `run_r1_live.py`, `run_r1_dummy.py`, `run_r3_bench.py` (+ `_temporal`,
`_structural`, `_exhaustion`, `_warden`), `run_r5_bench.py`, `run_facet_bench.py`,
`run_intent_bench.py`, `run_l4_bench.py`, `run_oracle_bench.py`, `run_content_vector_benchmark.py`,
`run_mteb_local.py`, `mine_r1_gold.py`, `probe_l4_walk.py`, `probe_rrf_scale.py`,
`probe_phase3_sum_rate.py`, `dump_prod_fixture.py`.

### Launch gates
`check_evidence_policy.py` (offline evidence-policy validator),
`check_public_claims.py` (offline public-claim scanner). Both gate launch copy.

## Maintenance

When you add a durable script, add a row here in the same commit. When you finish with a `_` script,
delete it in the same commit that records its finding. If this index disagrees with the filesystem,
the filesystem is right and this file is a bug.
