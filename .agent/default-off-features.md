# Default-off shipped features — activation ledger

**Purpose.** A single place to track features that are **code-complete and shipped but default-off**,
so activation is a deliberate decision, not a rediscovery. A plan being archived as "code-complete"
does NOT mean its feature is live — most of the frontier retrieval stack ships off by design (the
2026-07-04 read-side bench verdict: neutral-to-negative on LongMemEval, so it does not earn being on
by default). This ledger is the bridge between "built" and "on".

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
| Event History Phase 3 Consolidation | `personal_memory_event_history_enabled` / `MENHIR_EVENT_HISTORY_ENABLED` | backfill :TurnEvidence -> assertions via independent watermark cursor | production-capable but default-off; Phase 1-5 complete | `menhir-event-history-plan.md` + commits 048b8d9..51c11cf |
| Event History Phase 4 Recall Authority | `personal_memory_event_history_authority_enabled` / `MENHIR_EVENT_HISTORY_AUTHORITY_ENABLED` | conditional first-person event route probed only when enabled and namespace present | production-capable but default-off; independent of scalar authority | commits 048b8d9..51c11cf |
| Brief builder (append temporal Timeline in build_context) | `frontier_brief_builder` | `build_context` stage (not recall tuning) | safe/neutral on LME (append +0.03); pending lift at larger N | brief-builder work |
| Deterministic canonical-self binding | `canonical_self_binding_mode` / `MENHIR_CANONICAL_SELF_BINDING_MODE` (`off`/`observe`/`enforce`) | `_run_graphiti_combined_extraction`, after relationless repair and before graphiti candidate acquisition | mechanism tested but no production subject-authority producer exists, so prevention is incomplete and `enforce` is inert | `menhir-canonical-self-remediation-plan.md`; runbook `workflows/canonical-self-migration-runbook.md` |

## Activated (moved on default-on)

_(none yet — add rows here with the activation date when a flag flips `True` or a gate is removed)_

---

**Last reconciled:** 2026-08-07; event history (Phase 3 consolidation + Phase 4 recall authority)
added against `src/menhir/config/settings_model.py` and commits 048b8d9..51c11cf.
