---
artifact_schema: 1
artifact_uuid: f57095ed-c3de-49e6-bc53-ab1721792cdb
artifact_type: review
artifact_status: COMPLETE
---

# Menhir: triage of the flags left off after the #168 freeze (2026-10-27)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Scope: every behavior flag in `feature_flags.py` at Menhir main `d1105b25` that is off by default,
plus two left to deployment config. None ran in a bench run since 2026-10-13
(`menhir-168-freeze-feature-flag-review-2026-10-27.md`). This file records the evidence from any date.

Sources: bench run records, `archolith-bench/results` and bench notes, the KU78
buildouts, Menhir `.agent` (ledger
`default-off-features.md`, plans, archive, CHANGELOG), workspace `.agent` plans and reviews, and
`git log -S`. Gate points are files in `src` (outside config) that read the setting, env var or mapped
knob. Modules are files named for the feature.

Decision quotes were checked against the files. The run results are the reports' extractions and
were not recomputed.

## 1. Measured and lost, or superseded: candidates to delete

| Flag (MENHIR_) | Last on | Result | Decided in | Code |
|---|---|---|---|---|
| FRONTIER_BM25 | 2026-07-15 (content-vector arms); R1 live 07-05 | hybrid lost 0.700 vs 0.710 and `wrong_scope` rose 0.034 -> 0.081 | ledger: "neutral-to-negative on LME"; bench CHANGELOG "does not graduate" | 3 gate files |
| FRONTIER_CONTENT_VECTOR, _REPLACE_NAME | 2026-07-15 | tied the human-anchor primaries; decision NO-GRADUATE | `menhir/.agent/archive/plans/menhir-content-embedding-recall.md`: "CLOSED — NO-GRADUATE (2026-07-14)" | 3 + 3 gate files |
| FRONTIER_ORACLE_SUBSET (env-only) | 2026-07-02 | 0.367 vs node-only 0.400 | bench `lme-score-campaign.md`: "Node-only relevance ranking remains the champion." | 1 file |
| FRONTIER_FACT_EDGES (+ FACT_EDGE_MODE) | 2026-07-02 | off 9/30, standalone 1/30, pointer 8/30 | `menhir/.agent/reference/anecdotal-recall-oracle-ladder.md`: keep default off | 3 gate files |
| FRONTIER_SIMILARITY_SCALE=normalized (mode) | 2026-07-04, N=500 | 143/500 for both modes, identical set and order | ledger: "A/B before flip"; content-vector plan §13 "No-op" | mode value only |
| PERSONAL_MEMORY_SCALAR_CANONICAL_SELF | replay only (bench 3a036d4, 07-27) | "ZERO cells in every configuration" | bench `config.sh`: "flip on only to re-measure" | 2 gate files |
| PERSONAL_MEMORY_SCALAR_VIEW_COLLAPSE_ENABLED | 2026-10-16 paid A/B, n=5 | off 4/5, off+collapse 4/5, on 3/5, on+collapse 3/5 | archived collapse drill-down plan: "hiding counted quotes hurts"; split into echo drop | 2 gate files |
| SCALAR_DETERMINISTIC_SHADOW, _ROUTER | offline 78-item eval, 08-06 | "Zero compositional scalar identities were produced" | bench `ASSESSMENT.md`: "should remain shadow-only. It is not a viable replacement"; plan: fails the zero-router-miss gate | 5 modules, ~1,190 lines |
| SHADOW_CONTEXT_COMPOSITION | real-traffic trace, 07-16 (10 rows) | "exit criteria only partially met" | archived context-composition plan: DEFERRED by owner 10-23; Stages 2-4 overtaken by the TurnEvidence pipeline | `services/shadow_context_composition.py` + 6 gate files |

## 2. Measured neutral, or broken, with a named follow-up

| Flag | Result | Follow-up | Code |
|---|---|---|---|
| FRONTIER_BELIEF_GATE | LME 500 q x 3: quality delta 0, p95 +12.6%, 40% of candidates flagged | #350 (any expired edge marks a hub superseded) | 2 modules, ~490 lines |
| FRONTIER_EVIDENCE_ANCHOR | 3 of 33,861 entities pass; strict profile refused all 205 AMA questions | reimplementation #349 | 4 gate files |
| FRONTIER_SOURCE_MEMORY_POOLS | AMA A/B 09-30: indistinguishable from no pools (15 vs 14 head to head) | "value to be tested with an explore tool" | 1 module, 106 lines |

## 3. Never measured

| Flag | Status | Pending | Code |
|---|---|---|---|
| FRONTIER_DIVERSITY_GATE | only in the 07-02 "aggressive" stack, confounded by the evidence anchor | #344 | 1 module, 115 lines |
| FRONTIER_FACT_RELEVANCE | no run | #360 (re-score arm never run) | 2 gate files |
| FRONTIER_VERB_FACETS | offline simulation only (11 -> 13 of 16 turns) | review NEEDS CHANGES: fix F1 admission bypass; one paid arm | 1 module, 260 lines |
| PERSONAL_MEMORY_SCALAR_VIEW_ECHO_DROP_ENABLED | split out of collapse; its own A/B never ran | echo-drop-alone A/B | 2 gate files |
| PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED | offline repros; review defects addressed | none named | 2 gate files |
| PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED | on in every KU78 build 07-30..08-31 (v9 0.885), no on/off | slices 4-5 incomplete | 1 module, 313 lines |
| PERSONAL_MEMORY_EVENT_HISTORY_ENABLED, _AUTHORITY_ENABLED | KU78 v6 71/78 and v9 0.885, no clean on/off (v4 differs in commit) | planned full-500 with event history never ran; `arms/events.env` unused | 5 modules, ~2,270 lines |
| ANCHORED_TIME_RENDER | offline render tests; no live readback | needs anchored-time contracts, which the resolver now writes by default | 9 gate files |

## 4. Diagnostics and compatibility switches: keep off, not experiments

| Flag | Why |
|---|---|
| FRONTIER_SHADOW | observe-only trace |
| PERSONAL_MEMORY_CONSOLIDATION_DISABLE_REASONING | provider compatibility: "api.openai.com rejects, so defaulting it on would 400 every native-OpenAI deployment" (2ffae1bd) |

## 5. Left to deployment config

| Flag | Evidence | Note |
|---|---|---|
| PERSONAL_MEMORY_CONSOLIDATION_ENABLED (counter state) | no bench run has it on; the lanes ran with `counter_state=false` | findings register: a default `POST /api/phase3/run` no longer runs it |
| VERIFIER_SYNC_ENABLED | offline interaction tests only (#173) | deployed qualification pending (ledger) |

## Open decisions (owner)

1. Bucket 1: delete the feature code and flag, or retire the flag and keep the code as a seam (the
   freeze pattern)?
2. Bucket 2: keep until the named follow-up, or delete and reopen if the follow-up is done?
3. Bucket 3: name a run for each, or delete what has no planned run (fact relevance, dedup, echo drop)?
4. Bucket 5: make default or leave as deployment config?

Not covered: `FRONTIER_FUSION_ADMISSION_POLICY` ran with `production_fused` only inside the
content-vector arms (07-15); there is no separate result for it.
