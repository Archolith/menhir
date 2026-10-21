# Default-off shipped features — activation ledger

**Purpose.** A single place to track features that are **code-complete and shipped but default-off**,
so activation is a deliberate decision, not a rediscovery. A plan being archived as "code-complete"
does NOT mean its feature is live — most of the frontier retrieval stack ships off by design (the
2026-07-04 read-side bench verdict: neutral-to-negative on LongMemEval, so it does not earn being on
by default). This ledger is the bridge between "built" and "on".

The governing rollout decision is
[ADR 0007](adr/0007-evidence-gated-default-off-feature-activation.md).

**Rule of thumb.** Default-off-but-working stays out of production behavior until a per-deployment
flag is set (or a bench lift verdict flips the default). Enabling any of these is an owner decision.

**Maintenance.** When a new default-off feature ships, add a row. When one is activated by default
(flag flips to `True` or the gate is removed), move it to the "Activated" section with the date.

---

## Active default-off features

Anchor for the flags: `src/menhir/config/settings.py` (Frontier retrieval block, ~L225-245),
mapped into `RetrievalTuningConfig` at the recall entry via `retrieval_tuning()`.

| Feature | Flag / env | Gate location | Bench status | Source plan |
|---|---|---|---|---|
| Attributed hybrid (vector+BM25) candidate gen | `frontier_bm25` / `MENHIR_FRONTIER_BM25` | recall tuning | neutral-to-negative on LME | `r1-hybrid-candidate-generation.md` |
| Diversity gate (Guard 4, set-level anti-spiral) | `frontier_diversity_gate` / `MENHIR_FRONTIER_DIVERSITY_GATE` | recall tuning | opt-in (#344) | `menhir-r8-control-rails-plan.md` |
| Belief gate (CurrentnessWarden + belief scoring; incl. git/structure staleness feed) | `frontier_belief_gate` / `MENHIR_FRONTIER_BELIEF_GATE` | requires `frontier_warden_gate` | opt-in (#173): marks an Entity superseded when any fact edge expired; with git/test/log evidence that scores CONFLICT_SET and the contradiction guard refuses it under the current lens, and the Warden lens ignores "before/previously/used to" history cues. LME 500 q x 3 with it on: quality delta 0, p50 +1.8%, p95 +12.6%, 40% of candidates flagged. Follow-up: #350 | `menhir-belief-gate-activation.md` + `menhir-belief-gate-git-staleness.md` |
| Evidence-anchor warden (Guard 5) | `frontier_evidence_anchor` / `MENHIR_FRONTIER_EVIDENCE_ANCHOR` | under `warden_gate` | opt-in: provenance comes from the client label, so 3 of 33,861 LME copy-graph entities pass and the strict profile refused all 205 AMA questions; reimplementation in #349 | R8 rails |
| Fact-edge injection (RELATES_TO into candidate pool) | `frontier_fact_edges` (+ `frontier_fact_edge_mode`) | recall tuning | standalone net-negative at N=30; "pointer" preferred | retrieval fact-edge work |
| Similarity lane scale | `frontier_similarity_scale` (`rrf` default / `normalized`) | recall tuning | ranking change; A/B before flip | `retrieval-scale-contract-and-gap-remediation.md` (1a/1b) |
| Shadow pass (observe-only oracle/warden trace) | `frontier_shadow` / `MENHIR_FRONTIER_SHADOW` | recall tuning | observe-only | oracle stack |
| Deterministic typed-scalar shadow | `personal_memory_scalar_deterministic_shadow` / `MENHIR_SCALAR_DETERMINISTIC_SHADOW` | `TypedScalarPerceptionService` after the LLM gate; audit rows also require consolidation audit | observe-only; held-out agreement/router gates not yet measured | `menhir-deterministic-first-event-scalar-2026-07-30.md` |
| Event History Phase 3 Consolidation | `personal_memory_event_history_enabled` / `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED` | backfill :TurnEvidence -> assertions via independent watermark cursor | production-capable but default-off; Phase 1-5 complete | `menhir-event-history-plan.md` + commits 048b8d9..51c11cf |
| Event History Phase 4 Recall Authority | `personal_memory_event_history_authority_enabled` / `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED` | conditional first-person event route probed only when enabled and namespace present | production-capable but default-off; independent of scalar authority | commits 048b8d9..51c11cf |
| Brief builder (append temporal Timeline in build_context) | `frontier_brief_builder` | removed 2026-10-01, superseded by `recall_timeline` | — | brief-builder work |
| Anchored-time resolver (overlay a dated window midpoint onto `valid_at` for user-turn edges with temporal cues; keeps Graphiti's value when inside the window, never touches `invalid_at`) | `anchored_time_resolver_enabled` / `MENHIR_ANCHORED_TIME_RESOLVER` (+ `_MODEL`, `_TIMEOUT_S`) | end of `_run_graphiti_combined_extraction` via `MenhirExtractionHook(anchored_time=...)`, before `resolve_extracted_edges`; resolver built in `graphiti_client.py` only when the LLM is on | offline replay: frozen held-outs written 26/32, 19/23, 19/23, 25/28 dated vs baseline 18/32, 12/23, 12/23, 18/28; live L1 (Flex, 120 s timeout) passed: written 26/32, 19/23, 23/28, 1/89 timeouts; L3 fresh held-out: C1 not met, C2/C3 met; gate run 1 (b11) FAIL on d16485d, year rule added in efdbfd4; accepted on non-blind evidence, no gate pass | workspace `menhir-anchored-time-resolver-p1-plan.md` |
| Anchored-time render (show the persisted time contract as each fact's event time: the resolved window, a plan's planned window, or "event time unknown (said <speech date>)" instead of a speech-date `valid_at`; timeline facts order by window start) | `anchored_time_render_enabled` / `MENHIR_ANCHORED_TIME_RENDER` | `_build_temporal_facts(event_time=...)` in recall enrichment, `_source_time_lines` in the context builder, MCP recall formatter, `timeline_facts(event_time=...)`; contracts are written by the resolver (`anchored_time_persist.py`) whenever it is on, independent of this flag | offline render and flag-off identity tests (`tests/test_anchored_time_render.py`); no live readback yet | workspace `menhir-anchored-time-resolver-p2-plan.md` |
| Anchored-time expiry (a fact's own end is world time, not supersession: Graphiti does not set `expired_at` only because an extracted point event or state carries its own `invalid_at` (`MenhirEdgeExpiryHook` answers `WORLD_END`; needs archolith-graphiti-core 0.30.2.post6), the edge is stamped `time_expiry`/`time_world_end`, and recall labels such live ended facts `ended`) | `anchored_time_expiry_enabled` / `MENHIR_ANCHORED_TIME_EXPIRY` | `_apply_anchored_time(expiry=...)` records the world end before resolve; `MenhirEdgeExpiryHook` in `graphiti_extraction_policy.py` decides expiry inside the resolver (owner-checked, fails to `EXPIRE`); `EXPIRY_PERSIST_CYPHER` in `anchored_time_persist.py` stamps live edges under the P2 lock only while `invalid_at` is unchanged and never un-expires; `_build_temporal_facts(ended=...)` and `_source_time_lines`; needs the resolver at ingest, no backfill; a restatement with no usable report keeps a stored ended fact live only from its own stamp (`time_expiry='world_end'` and `time_world_end` equal to `invalid_at`), which the fork keeps through duplicate resolution via `preserved_edge_attributes` (P3c); a concurrent writer's stale save cannot revive an edge another writer expired or move its end later on Neo4j (archolith-graphiti-core 0.30.2.post7, Codex P3c-1); the operator unmerge (`restore_merge_snapshot`) can still recreate a live copy from its merge-time snapshot | offline and live counterexample tests (`tests/test_anchored_time_expiry.py`, `tests/test_anchored_time_expiry_live.py`, `tests/test_anchored_time_restatement.py`, `tests/test_anchored_time_restatement_live.py`); no live LLM readback yet | workspace `menhir-anchored-time-resolver-p3-plan.md`, `menhir-anchored-time-p3c-restatement-evidence-plan.md` |
| Two-tier self identity (episodes that cannot reach the anchored speaker marker bind the user to one inferred self per namespace instead of an ordinary "user" node; recall reads both tiers labelled; verbatim client copies of hook turns skip extraction) | `inferred_self_mode` / `MENHIR_INFERRED_SELF_MODE` (`off` / `observe` / `enforce`) | `begin_extraction_receipt` / `_record_inferred_binding` in `graphiti_extraction_policy.py`; recall in `recall_pipeline.py`; skip in `ingest_worker._try_verbatim_skip`; the re-enable sweep runs in lease recovery whatever the mode | offline and live tests; E2E-19 `two_tier_self` on the deterministic provider | workspace `menhir-two-tier-self-identity-plan-2026-10-21.md` (#343) |
| Anchored-time attach check (a blind second model call must quote the same time words with the right relation before a resolver time is written; otherwise the edge keeps Graphiti's value) | `anchored_time_attach_enabled` / `MENHIR_ANCHORED_TIME_ATTACH` (+ `MENHIR_ANCHORED_TIME_ATTACH_MODEL`, empty means the resolver's model) | `_attach_check` after `plan_overlay` in `_apply_anchored_time` (one blind call per episode with candidates; V1-V3 in `anchored_time_attach.decide`; any failure writes nothing); attacher built in `graphiti_client.py` only with the resolver; persisted contract `at2`; ingest-only, no backfill | unit tests (`tests/test_anchored_time_attach.py`, `tests/test_anchored_time_attach_hook.py`); flag-off replay identical to a0a9814 over 2374 facts; no live run yet (P3 gates G1-G4 pending) | workspace `menhir-anchored-time-attachment-plan.md` |

## Warden profiles and context wiring (2026-10-05)

Detailed execution status is a per-call opt-in (`include_warden_status`, false by default), available
without enabling the larger retrieval trace. It changes visibility only. Brief failed/unapplied or
incomplete-check notices remain in default output; configured guard settings never certify checked
results. Context diagnostics consume its normal token budget. Recall Lab requests detail explicitly.
This receipt covers ranked Entity enforcement and labels pending bypasses; authority/source/TODO/
recent bootstrap sections are outside its contract. No new-install default or activation decision is
made by diagnostic tests. Ingest and paid usefulness qualification remain deferred.

Verifier sync remains opt-in. When an enabled trusted verifier detects drift, linked same-namespace
non-derived Entity beliefs retain a review flag through ranked recall, compact MCP, REST and context.
The advisory is independent of Warden gates: verify the source before asserting current truth.
It does not change scores, refuse prose, clear existing review markers, or certify new defaults.
Invalid/unavailable observations leave the register and successful-verification timestamp untouched.
Recall/context show linked register observations and absolute last-success timestamps alongside
separate latest-probe status/time. Pending means an incomplete attempt; legacy missing status is
unknown. Evidence adds no expiry cutoff or ranking effect. This is code-side coverage only: deployed
qualification, mixed-version/concurrent writers, ingest, and paid usefulness runs remain pending.

The Warden master switch and evidence-anchor guard are separate choices. The strict Recall Lab
arm D enables both and intentionally refuses agent-only or unanchored candidates. Arm E disables
the evidence-anchor guard while retaining scope and Oracle admission decisions; its results are
not proof of externally verified truth. For conversational qualification, compare that profile
with the Warden-off control, keeping ranking and sources fixed. Do not promote either profile
from the earlier all-refused result.

With both master Warden and contradiction interrupt enabled, ranked Entity candidates with an
existing conflict group and exact `unresolved` status are refused for current queries, or retained
with a conflict label for historical queries (subject to other guards). This metadata handoff does
not require the belief gate or alter ranking. Missing/unknown/resolved status supplies no new
contradiction signal; existing oracle/belief conflicts still apply independently. With contradiction
off, the existing scoring/context advisory remains. The offline panel covers these transitions;
corpus usefulness and default eligibility remain unqualified.

Runtime and direct-hook context builders carry the configured retrieval tuning into their recall
call, matching ordinary backend recall. Constructors used without tuning retain their prior
behavior. Warden settings remain opt-in. This gate covers the scored candidate set, not pending
fallbacks, the additive source-memory section, or every authority output. Missing scope remains
permissive, missing support is refused only by the enabled evidence-anchor guard, and existing
frontier errors can fall back to baseline results. This is not universal enforcement.

## Activated (moved on default-on)

_(none yet — add rows here with the activation date when a flag flips `True` or a gate is removed)_

### Graduated to default-on 2026-09-30: source-memory recall lane

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Source-memory recall lane (additive raw-episode section next to ranked results, never fused; ingest-side episode-content embedding + `scripts/backfill_episode_embeddings.py` backfill; optional anchor pools via `frontier_source_memory_pools`) | `frontier_source_memories` / `MENHIR_FRONTIER_SOURCE_MEMORIES` (+ `_SOURCE_MEMORY_LIMIT`, `_SOURCE_MEMORY_MAX_CHARS`, `_SOURCE_MEMORY_POOLS`; per-call `source_memory_limit` on REST/MCP/recall) | recall tuning (`enable_source_memories`) + ingest gate (`source_memories_enabled`). Recall Lab exposes it as opt-in arm tunings only — no default arms | AMA-Bench live A/B (505 q x 2: limit 10 fixed 104 / broke 52 vs off; non-state types improved) and LongMemEval oracle 500 q x 2 (0.340 -> 0.711 correct, knowledge-update 0.365 -> 0.782, every type improved, consistent fixed 183 / broke 4) | `menhir-source-memory-lane-plan.md` |

### Graduated to always-on 2026-10-20: Oracle ranking and intent lens (flags removed)

| Feature | Former flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Blend the oracle combiner order into survivors (weight 0.25) | `frontier_oracle_ranking` / `MENHIR_FRONTIER_ORACLE_RANKING` (retired) | always on; per-request `RetrievalTuningConfig.enable_oracle_ranking` remains for Recall Lab | LME copy graph 500 q x 3, server path: top-10 hit delta 0.000, MRR delta +0.0000 [-0.0004, +0.0004]; p50 +8.2%, p95 +10.7%; outage now order-neutral (tie-break fix) | #172 |
| **IntentOracle** temporal lens from query text | `frontier_intent_lens` / `MENHIR_FRONTIER_INTENT_LENS` (retired) | always on; per-request `enable_intent_lens` | with Oracle: MRR delta -0.0001 [-0.0004, +0.0001]; p50 +8.4%, p95 +16.1% | #172 |

Not yet measured (non-blocking, #345): AMA stale-before-current and answer-level quality.

### Graduated to default-on 2026-10-20: canonical-self binding (`enforce`)

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Deterministic canonical-self binding | `canonical_self_binding_mode` / `MENHIR_CANONICAL_SELF_BINDING_MODE` (`off`/`observe`/`enforce`, default `enforce`; pin `off` to opt out) | atomic projection claim plus `_run_graphiti_combined_extraction`, after relationless repair and before graphiti candidate acquisition | 22-case corpus: 0 false-positive rewrites across off/observe/enforce. Held-out third-party panel (40 LongMemEval turns per mode, gpt-6-luna): no third-party fact attached to the user; 99/99 projection facts bound to the canonical self. Known cost: agent-written memories duplicate user facts on a separate "user" entity (#343). Eval: `scripts/canonical_self_attribution_eval.py` (#342) | `menhir-canonical-self-remediation-plan.md`; runbook `workflows/canonical-self-migration-runbook.md` (activation canary; existing forks stay until a consolidation tool exists) |

### Graduated to default-on 2026-10-20: Warden gate with the contradiction guard

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Warden gate: drop REFUSED / label FLAGGED (scope + Oracle admission) | `frontier_warden_gate` / `MENHIR_FRONTIER_WARDEN_GATE` (default true; set false to opt out) | recall tuning; ranked Entity candidates only (see "Warden profiles" above) | LME copy graph 500 q x 3, server path: top-10 hit and MRR delta 0.0000 [0.0000, 0.0000]; p50 +0.3%, p95 +1.5%; 0 refused (LME has no scoped or agent-only rows) | R8 rails; #173 |
| Contradiction interrupt (Guard 7) | `frontier_contradiction_interrupt` / `MENHIR_FRONTIER_CONTRADICTION_INTERRUPT` (default true) | requires `frontier_warden_gate` | with the gate: hit and MRR delta 0.0000; p50 +0.2%, p95 -0.4%; offline panel 108 decisions (#228, #229) | `menhir-r8-control-rails-plan.md`; #173 |

The belief gate (see the opt-in table) and the evidence anchor (#349) stay opt-in. Recall Lab arm
D pins the anchor on as the strict profile; arms E-H pin the guards they were defined with.
Verifier sync is not a Warden and stays opt-in.

---

**Last reconciled:** 2026-08-07; event history (Phase 3 consolidation + Phase 4 recall authority)
added against `src/menhir/config/settings_model.py` and commits 048b8d9..51c11cf.
