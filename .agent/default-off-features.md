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
| Reorder survivors by oracle combiner | `frontier_oracle_ranking` / `MENHIR_FRONTIER_ORACLE_RANKING` | recall tuning | neutral-to-negative on LME | oracle stack (R4-R7) |
| **IntentOracle** temporal lens from query text | `frontier_intent_lens` / `MENHIR_FRONTIER_INTENT_LENS` | `AssertionPipeline(auto_intent=tuning.enable_intent_lens)`, `recall_service.py:567,702` | bench-graduated (real embedder), off by default | `menhir-intent-oracle-plan.md` (archived 2026-07-11) |
| Warden gate: drop REFUSED / label FLAGGED | `frontier_warden_gate` / `MENHIR_FRONTIER_WARDEN_GATE` | recall tuning | opt-in; aggressive | R8 rails |
| Diversity gate (Guard 4, set-level anti-spiral) | `frontier_diversity_gate` / `MENHIR_FRONTIER_DIVERSITY_GATE` | recall tuning | opt-in | `menhir-r8-control-rails-plan.md` |
| Contradiction interrupt (Guard 7) | `frontier_contradiction_interrupt` / `MENHIR_FRONTIER_CONTRADICTION_INTERRUPT` | recall tuning | opt-in | `menhir-r8-control-rails-plan.md` |
| Belief gate (CurrentnessWarden + belief scoring; incl. git/structure staleness feed) | `frontier_belief_gate` | requires `frontier_warden_gate` | opt-in; aggressive | `menhir-belief-gate-activation.md` + `menhir-belief-gate-git-staleness.md` |
| Evidence-anchor warden (Guard 5) | `frontier_evidence_anchor` | under `warden_gate`; TRUE for code corpora | corpus-dependent | R8 rails |
| Fact-edge injection (RELATES_TO into candidate pool) | `frontier_fact_edges` (+ `frontier_fact_edge_mode`) | recall tuning | standalone net-negative at N=30; "pointer" preferred | retrieval fact-edge work |
| Similarity lane scale | `frontier_similarity_scale` (`rrf` default / `normalized`) | recall tuning | ranking change; A/B before flip | `retrieval-scale-contract-and-gap-remediation.md` (1a/1b) |
| Shadow pass (observe-only oracle/warden trace) | `frontier_shadow` / `MENHIR_FRONTIER_SHADOW` | recall tuning | observe-only | oracle stack |
| Deterministic typed-scalar shadow | `personal_memory_scalar_deterministic_shadow` / `MENHIR_SCALAR_DETERMINISTIC_SHADOW` | `TypedScalarPerceptionService` after the LLM gate; audit rows also require consolidation audit | observe-only; held-out agreement/router gates not yet measured | `menhir-deterministic-first-event-scalar-2026-07-30.md` |
| Event History Phase 3 Consolidation | `personal_memory_event_history_enabled` / `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED` | backfill :TurnEvidence -> assertions via independent watermark cursor | production-capable but default-off; Phase 1-5 complete | `menhir-event-history-plan.md` + commits 048b8d9..51c11cf |
| Event History Phase 4 Recall Authority | `personal_memory_event_history_authority_enabled` / `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED` | conditional first-person event route probed only when enabled and namespace present | production-capable but default-off; independent of scalar authority | commits 048b8d9..51c11cf |
| Brief builder (append temporal Timeline in build_context) | `frontier_brief_builder` | removed 2026-10-01, superseded by `recall_timeline` | — | brief-builder work |
| Deterministic canonical-self binding | `canonical_self_binding_mode` / `MENHIR_CANONICAL_SELF_BINDING_MODE` (`off`/`observe`/`enforce`) | atomic projection claim plus `_run_graphiti_combined_extraction`, after relationless repair and before graphiti candidate acquisition | verified evidence projections have an exact subject-endpoint producer; default remains off pending repeated real-model corpus and persistence acceptance | `menhir-canonical-self-remediation-plan.md`; runbook `workflows/canonical-self-migration-runbook.md` |
| Anchored-time resolver (overlay a dated window midpoint onto `valid_at` for user-turn edges with temporal cues; keeps Graphiti's value when inside the window, never touches `invalid_at`) | `anchored_time_resolver_enabled` / `MENHIR_ANCHORED_TIME_RESOLVER` (+ `_MODEL`, `_TIMEOUT_S`) | end of `_run_graphiti_combined_extraction` via `MenhirExtractionHook(anchored_time=...)`, before `resolve_extracted_edges`; resolver built in `graphiti_client.py` only when the LLM is on | offline replay: frozen held-outs written 26/32, 19/23, 19/23, 25/28 dated vs baseline 18/32, 12/23, 12/23, 18/28; live L1 (Flex, 120 s timeout) passed: written 26/32, 19/23, 23/28, 1/89 timeouts; L3 fresh held-out: C1 not met, C2/C3 met; gate run 1 (b11) FAIL on d16485d, year rule added in efdbfd4; accepted on non-blind evidence, no gate pass | workspace `menhir-anchored-time-resolver-p1-plan.md` |
| Anchored-time render (show the persisted time contract as each fact's event time: the resolved window, a plan's planned window, or "event time unknown (said <speech date>)" instead of a speech-date `valid_at`; timeline facts order by window start) | `anchored_time_render_enabled` / `MENHIR_ANCHORED_TIME_RENDER` | `_build_temporal_facts(event_time=...)` in recall enrichment, `_source_time_lines` in the context builder, MCP recall formatter, `timeline_facts(event_time=...)`; contracts are written by the resolver (`anchored_time_persist.py`) whenever it is on, independent of this flag | offline render and flag-off identity tests (`tests/test_anchored_time_render.py`); no live readback yet | workspace `menhir-anchored-time-resolver-p2-plan.md` |
| Anchored-time expiry (a fact's own end is world time, not supersession: clear the `expired_at` Graphiti sets only because an extracted point event or state carries its own `invalid_at`, stamp `time_expiry`/`time_world_end`, and label such unexpired facts `ended` in recall) | `anchored_time_expiry_enabled` / `MENHIR_ANCHORED_TIME_EXPIRY` | `_apply_anchored_time(expiry=...)` records the world end before resolve; `EXPIRY_PERSIST_CYPHER` in `anchored_time_persist.py` clears under the P2 lock and re-check only while `invalid_at` is unchanged; `_build_temporal_facts(ended=...)` and `_source_time_lines`; needs the resolver at ingest, no backfill | offline and live counterexample tests (`tests/test_anchored_time_expiry.py`, `tests/test_anchored_time_expiry_live.py`); no live LLM readback yet | workspace `menhir-anchored-time-resolver-p3-plan.md` |

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

---

**Last reconciled:** 2026-08-07; event history (Phase 3 consolidation + Phase 4 recall authority)
added against `src/menhir/config/settings_model.py` and commits 048b8d9..51c11cf.
