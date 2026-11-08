---
artifact_schema: 1
artifact_uuid: 00d5f44e-e522-4544-9d37-d75d18d4e4a5
artifact_type: review
artifact_status: COMPLETE
---

# Menhir #168 freeze: feature flag defaults (2026-10-27)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical. This is the review as of 2026-10-27; `default-off-features.md` records what shipped, including the flags later removed.

Owner rules (2026-10-27):
- The freeze makes the tested behavior Menhir's default behavior.
- A feature that was on in a bench run in the last two weeks (since 2026-10-13) defaults to on.
- Nothing is removed. Features and flags stay; only defaults change.

## Boundary and method

- Flags: every entry of `FEATURES` in `src/menhir/config/feature_flags.py` at menhir-168-rebase
  `466a8d39`: 122 entries (auth 32, runtime 31, personal_memory 27, frontier 22, telemetry 7, saga 3).
  Tunables the freeze sets outside `FEATURES` are covered too.
- Run evidence:
  - Records: local bench run records (99 run dirs, 10-21 to 10-27) and the luna pilot results
    (10-15/16).
  - Files read: `arm.env` (scoring arms), `lineage/*.json` (lane `arm_settings` and pins), and
    `graph-provenance*.json` (build settings).
  - Runs from other places are not counted: ad hoc replays, Menhir eval scripts, unit and live
    tests.
  - A flag no record sets ran at the default of that run's Menhir commit. Defaults were read per
    commit: `f30aed3b`, `af85c3e7`, `7a9d615c`, `953ca6e7`, `14f86d29`, `503991cf`, `b7bea48c`, plus
    `7f25f632` for the 10-27 scores.
- Current freeze envs (the fz5 aso snapshots and the 10-27 scores), resolved with
  `MemorySettings.from_env()`:
  - ingest: the bench build scripts, with the values in shard 5's graph provenance.
  - lanes: the bench serve script and the counted lineage record.
  - score: the bench serve script and the freeze score arm.
- Live deployment config was also checked.

## A. Default changes

| Flag (MENHIR_) | Default now | New default | Last run on | Evidence | In the current freeze? |
|---|---|---|---|---|---|
| PERSONAL_MEMORY_SCALAR_STATE_ENABLED | off | on | 10-27 | On/off arms; #170 scalar decisions | Yes (lanes, score) |
| PERSONAL_MEMORY_COUNTING_ENABLED | off | on | 10-27 | `base`/`off` vs counted arms | Yes (lanes, score) |
| PERSONAL_MEMORY_COUNTING_RECALL_AUTHORITY_ONLY | off | on | 10-27 | Off in 9 runs (last 10-24), on in 62 | Yes (score) |
| PERSONAL_MEMORY_COUNTING_WORDING_ENABLED | off | on | 10-27 | Same; wording A/B kept "at least N" | Yes (score) |
| PERSONAL_MEMORY_COUNTING_EXTRACT_V2 | off | on | 10-23 | `counted-4r`, o500 S4 (`953ca6e7`): 337/500 vs 328/500 for v1 | **No**, lanes ran v1 |
| PERSONAL_MEMORY_COUNTING_MATCH_ENABLED | off | on | 10-23 | Same arm (both on) | **No** |
| ANCHORED_TIME_RESOLVER | off | on | 10-15/16 | luna pilot build (`f30aed3b`, 78 items); no score recorded | **No**, built off |
| ANCHORED_TIME_ATTACH | off | on | 10-15/16 | Same build | **No** |
| ANCHORED_TIME_EXPIRY | off | on | 10-15/16 | Same build | **No** |
| FRONTIER_SOURCE_MEMORY_MAX_CHARS (tunable) | 600 | 2000 | 10-27 | src2000 vs src600 arms, 10-26 | Yes (score) |

Dependencies:
- Counting builds on scalar state, and both become default on.
- The luna pilot ran the three anchored-time flags together, without ANCHORED_TIME_RENDER, which stays
  off.

## B. Defaults that stay as they are

On now and on in the window, so no change. Every one was on in the 10-26/27 freeze builds and scores:
- PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED
- PERSONAL_MEMORY_SCALAR_VIEW_FRESHNESS_GATE
- PERSONAL_MEMORY_SCALAR_RECONCILE_ATTRIBUTE / _SCOPE / _SUBJECT
- PERSONAL_MEMORY_SUM_GROUNDING
- COUNT_ANCHOR_HINT_ENABLED
- STALE_LEAD_DEMOTION_ENABLED
- ASSISTANT_TURNS_STORE_ONLY_ENABLED
- EDGE_INVALIDATION_STATEMENT_ORDER
- FRONTIER_WARDEN_GATE
- FRONTIER_CONTRADICTION_INTERRUPT
- FRONTIER_FACT_SAID_TIME
- FRONTIER_SOURCE_MEMORIES
- FRONTIER_SOURCE_MEMORY_SPREAD
- Modes: CANONICAL_SELF_BINDING_MODE=enforce, INFERRED_SELF_MODE=enforce,
  FRONTIER_FUSION_ADMISSION_POLICY=attributed, FRONTIER_FACT_EDGE_MODE=standalone,
  FRONTIER_SIMILARITY_SCALE=rrf
- Tunables: SCALAR_THRESHOLD=2/3, SCALAR_VERDICT_MIN_SIMILARITY=0.675, SCALAR_OBSERVATION_RECALL=gated,
  MAX_LLM_CALLS_PER_JOB=100

On in every run but never off in the window, so no on/off test in the last two weeks:
- SUM_GROUNDING
- the reconcile flags
- VIEW_AUTHORITY
- SOURCE_MEMORIES
- the modes
- WARDEN_GATE and CONTRADICTION_INTERRUPT (off at `f30aed3b`, on since 10-21)
- EDGE_INVALIDATION_STATEMENT_ORDER (new in `b7bea48c`)
- COUNT_ANCHOR_HINT (new; a placement A/B only)
- ASSISTANT_TURNS_STORE_ONLY. Its explicit `store_only` equivalent built the aso snapshots; the flag
  itself has run only in tests.

Off now and not on in any run in the window, so they stay off:
- SHADOW_CONTEXT_COMPOSITION
- ANCHORED_TIME_RENDER
- PERSONAL_MEMORY_CONSOLIDATION_ENABLED (a deployment override keeps working)
- SCALAR_VIEW_COLLAPSE, SCALAR_VIEW_ECHO_DROP
- OBSERVATION_DEDUP
- SCALAR_CANONICAL_SELF
- SCALAR_HISTORY
- SCALAR_DETERMINISTIC_SHADOW, SCALAR_DETERMINISTIC_ROUTER
- EVENT_HISTORY, EVENT_HISTORY_AUTHORITY
- FRONTIER_BM25, FRONTIER_CONTENT_VECTOR(_REPLACE_NAME)
- FRONTIER_DIVERSITY_GATE, FRONTIER_BELIEF_GATE, FRONTIER_EVIDENCE_ANCHOR
- FRONTIER_FACT_EDGES, FRONTIER_FACT_RELEVANCE, FRONTIER_VERB_FACETS
- FRONTIER_SHADOW, FRONTIER_SOURCE_MEMORY_POOLS
- FRONTIER_ORACLE_SUBSET
- VERIFIER_SYNC_ENABLED (a deployment override keeps working)

## C. On in every run, but not a behavior default

| Flag (MENHIR_) | Why the rule is not applied |
|---|---|
| BENCHMARK_MODE | A test switch: it turns off the scheduler and orphan recovery. As a default it would stop the scheduler everywhere, and the scheduler is what runs the scalar and counting lanes in a deployment (E) |
| PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED, _RECALL_AUDIT_ENABLED | Telemetry. Behavior-neutral; turning them on by default adds audit writes for every client. Owner's call |
| OPENAI_SERVICE_TIER=flex | Cost and latency, not behavior |

Everything else in `FEATURES` is configuration and is not affected:
- auth and OAuth;
- paths and identity;
- saga operator switches;
- timeouts and retries;
- logging and console;
- runtime and startup mode;
- the explorer;
- privacy redaction;
- the embedding cache;
- provider reasoning suppression;
- RECALL_COMPACT;
- the non-recall features (RECORD_DETAILED_REVISIONS, STRUCTURE_WATCHER, EXPERIENCE_COUNTER,
  ARTIFACT_RECONCILE_MODE).

## D. What the freeze score covers after the change

The fz5 aso snapshots were built with counting v1 and with anchored time off. With the new defaults,
the frozen behavior differs from what was scored in two places:
1. Counting v2 + matching act in the counting lane. Scoring them needs a counted-4r lane over each fz5
   `-scalar` snapshot: paid, no re-ingest.
2. Anchored time resolver/attach/expiry act during extraction. Scoring them needs a full fz5 re-ingest:
   paid. Their only run in the window (10-15) has no recorded score, and used canonical self `off`.

The other default changes (scalar state, counting, counting authority only and wording, the 2000-char
source cap) are already what the freeze scored.

## E. Parity notes

- The scalar and counting lanes ran as separate stages: `lane.py` called `POST /api/phase3/run` once
  per namespace after ingest, with `k=3, call_budget=50, counter_state=false`.
  - A deployment runs the same `consolidate_personal_memory` from the maintenance scheduler
    (`maintenance_scheduler.py:213-218,921-951`): every 300 s, `k=3`, a 300-call budget per pass over
    dirty namespaces.
  - The function is the same; the timing and budget differ. A question asked before the first pass
    sees no Views.
- Default changes alter the Menhir fingerprint and the bench surface manifest. Arms that set these
  flags explicitly are unaffected.
