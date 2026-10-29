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
| Diversity gate (Guard 4, set-level anti-spiral) | `frontier_diversity_gate` / `MENHIR_FRONTIER_DIVERSITY_GATE` | recall tuning | opt-in (#344) | `menhir-r8-control-rails-plan.md` |
| Belief gate (CurrentnessWarden + belief scoring; incl. git/structure staleness feed) | `frontier_belief_gate` / `MENHIR_FRONTIER_BELIEF_GATE` | requires `frontier_warden_gate` | opt-in (#173): marks an Entity superseded when any fact edge expired; with git/test/log evidence that scores CONFLICT_SET and the contradiction guard refuses it under the current lens, and the Warden lens ignores "before/previously/used to" history cues. LME 500 q x 3 with it on: quality delta 0, p50 +1.8%, p95 +12.6%, 40% of candidates flagged. Follow-up: #350 | `menhir-belief-gate-activation.md` + `menhir-belief-gate-git-staleness.md` |
| Evidence-anchor warden (Guard 5) | `frontier_evidence_anchor` / `MENHIR_FRONTIER_EVIDENCE_ANCHOR` | under `warden_gate` | opt-in: provenance comes from the client label, so 3 of 33,861 LME copy-graph entities pass and the strict profile refused all 205 AMA questions; reimplementation in #349 | R8 rails |
| Shadow pass (observe-only oracle/warden trace) | `frontier_shadow` / `MENHIR_FRONTIER_SHADOW` | recall tuning | observe-only | oracle stack |
| Event History Phase 3 Consolidation | `personal_memory_event_history_enabled` / `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED` | backfill :TurnEvidence -> assertions via independent watermark cursor | production-capable but default-off; Phase 1-5 complete | `menhir-event-history-plan.md` + commits 048b8d9..51c11cf |
| Event History Phase 4 Recall Authority | `personal_memory_event_history_authority_enabled` / `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED` | conditional first-person event route probed only when enabled and namespace present | production-capable but default-off; independent of scalar authority | commits 048b8d9..51c11cf |
| Brief builder (append temporal Timeline in build_context) | `frontier_brief_builder` | removed 2026-10-01, superseded by `recall_timeline` | — | brief-builder work |

## Removed 2026-10-27

Eleven default-off experiments that were measured and lost, tied, or were deferred were removed
from `main`: BM25 hybrid, content vector, fact edges, similarity scale, oracle subset, View
collapse, scalar canonical self, the deterministic scalar shadow and router, and shadow context
composition. The code is on branch `archive/bucket1-features-2026-10-27`. Per-feature evidence,
removal commits and restore steps are in
[`archive/removed-features-2026-10-27.md`](archive/removed-features-2026-10-27.md).

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

### #168 freeze 2026-10-27: tested behavior becomes the default

Owner rule: a feature on in a bench run since 2026-10-13 is on by default, and its flag is removed,
except the three switches that guard LLM cost. Evidence per flag:
workspace `.agent/reviews/menhir-168-freeze-feature-flag-review-2026-10-27.md`. Retired keys are
listed in `RETIRED` (`config/feature_flags.py`); component knobs stay as per-request or test seams.

| Feature | Flag / env | Now | Commit |
|---|---|---|---|
| Scalar state lane (scheduler) | `MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED` | default on, switch kept (LLM cost) | `cc931186` |
| Counting lane (scheduler) | `MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED` | default on, switch kept (LLM cost) | `cc931186` |
| Anchored time: resolver, blind attach check, world-end expiry | `MENHIR_ANCHORED_TIME_RESOLVER` (one switch); `_ATTACH`, `_EXPIRY` retired | default on, switch kept (LLM cost at ingest); `_MODEL`, `_ATTACH_MODEL`, `_TIMEOUT_S` unchanged | `cc931186` |
| Source-memory max chars | `MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS` | 2000 (was 600) | `cc931186` |
| Warden gate, contradiction interrupt, fact said-time, source memories, source-memory spread, count anchor hint, stale-lead demotion | retired | always on | `370fd94e` |
| Edge-invalidation statement order | retired | always on | `8ec03d26` |
| Scalar reconcile attribute/scope/subject, scalar View authority (follows the scalar lane), View freshness gate, sum grounding | retired | always on | `d022a7ed` |
| Counting recall authority-only, wording, extraction v2, related-count matching (stamp `+x2+m`) | retired | always on | `9ec4d08c` |
| Assistant turns stored only | retired | always on | `bdad118c` |

### Graduated to default-on 2026-09-30: source-memory recall lane

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Source-memory recall lane (additive raw-episode section next to ranked results, never fused; ingest-side episode-content embedding + `scripts/backfill_episode_embeddings.py` backfill; optional anchor pools via `frontier_source_memory_pools`; per-session spread via `frontier_source_memory_spread`, default on since 2026-10-25: o500 changed 18/500 sections, fixed 3 / broke 0, PR #386) | `frontier_source_memories` / `MENHIR_FRONTIER_SOURCE_MEMORIES` (+ `_SOURCE_MEMORY_LIMIT`, `_SOURCE_MEMORY_MAX_CHARS`, `_SOURCE_MEMORY_POOLS`, `_SOURCE_MEMORY_SPREAD`; per-call `source_memory_limit` on REST/MCP/recall) | recall tuning (`enable_source_memories`) + ingest gate (`source_memories_enabled`). Recall Lab exposes it as opt-in arm tunings only — no default arms | AMA-Bench live A/B (505 q x 2: limit 10 fixed 104 / broke 52 vs off; non-state types improved) and LongMemEval oracle 500 q x 2 (0.340 -> 0.711 correct, knowledge-update 0.365 -> 0.782, every type improved, consistent fixed 183 / broke 4) | `menhir-source-memory-lane-plan.md` |

### Graduated to always-on 2026-10-20: Oracle ranking and intent lens (flags removed)

| Feature | Former flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Blend the oracle combiner order into survivors (weight 0.25) | `frontier_oracle_ranking` / `MENHIR_FRONTIER_ORACLE_RANKING` (retired) | always on; per-request `RetrievalTuningConfig.enable_oracle_ranking` remains for Recall Lab | LME copy graph 500 q x 3, server path: top-10 hit delta 0.000, MRR delta +0.0000 [-0.0004, +0.0004]; p50 +8.2%, p95 +10.7%; outage now order-neutral (tie-break fix) | #172 |
| **IntentOracle** temporal lens from query text | `frontier_intent_lens` / `MENHIR_FRONTIER_INTENT_LENS` (retired) | always on; per-request `enable_intent_lens` | with Oracle: MRR delta -0.0001 [-0.0004, +0.0001]; p50 +8.4%, p95 +16.1% | #172 |

Not yet measured (non-blocking, #345): AMA stale-before-current and answer-level quality.

### Graduated to default-on 2026-10-20: canonical-self binding (`enforce`)

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Deterministic canonical-self binding | `canonical_self_binding_mode` / `MENHIR_CANONICAL_SELF_BINDING_MODE` (`off`/`observe`/`enforce`, default `enforce`; pin `off` to opt out) | atomic projection claim plus `_run_graphiti_combined_extraction`, after relationless repair and before graphiti candidate acquisition | 22-case corpus: 0 false-positive rewrites across off/observe/enforce. Held-out third-party panel (40 LongMemEval turns per mode, gpt-6-luna): no third-party fact attached to the user; 99/99 projection facts bound to the canonical self. Known cost: agent-written memories duplicate user facts on a separate "user" entity (#343); addressed by the two-tier default (2026-10-21, below). Eval: `scripts/canonical_self_attribution_eval.py` (#342) | `menhir-canonical-self-remediation-plan.md`; runbook `workflows/canonical-self-migration-runbook.md` (activation canary; existing forks stay until a consolidation tool exists) |

### Graduated to default-on 2026-10-21: two-tier self identity (`enforce`)

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Two-tier self identity (episodes that cannot reach the anchored speaker marker bind the user to one inferred self per namespace instead of an ordinary "user" node; recall reads both tiers labelled; verbatim client copies of hook turns skip extraction) | `inferred_self_mode` / `MENHIR_INFERRED_SELF_MODE` (`off` / `observe` / `enforce`, default `enforce`; pin `off` to opt out) | `begin_extraction_receipt` / `_record_inferred_binding` in `graphiti_extraction_policy.py`; recall in `recall_pipeline.py` (backend) and `cli/bootstrap.py` (hook context); skip in `ingest_worker._try_verbatim_skip`; the re-enable sweep runs in lease recovery whatever the mode | offline and live tests; E2E-19 `two_tier_self` on the deterministic provider, now on shipped defaults; one manual pass on local `qwen3.6-35b-a3b` (2026-10-21): `two_tier_self` passed and, with the mode off, `canonical_self_binds` reproduced the #343 fork | workspace `menhir-two-tier-self-identity-plan-2026-10-21.md` (#343; owner decision 2026-10-21) |

### Graduated to default-on 2026-10-23: scalar View authority (inert until the scalar lane is on)

| Feature | Flag / env | Gate location | Evidence | Source plan |
|---|---|---|---|---|
| Scalar View authority (a current scalar_state View may suppress an older overlapping graph fact, only when all six authority gates pass) | `personal_memory_scalar_view_authority_enabled` / `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED` (default true; pin `false` to opt out) | `scalar_view_authority_effective` in `config/authority_gates.py`: applied only while `personal_memory_scalar_state_enabled` is on (default off), so a default install is unchanged. Collapse, echo-drop and observation-dedup stay off | canonical +0.795 lift on the frozen LME panel (scalar roadmap 2026-08-07); E2E-17 and E2E-18 cover authority on and the opt-out | `menhir-scalar-default-enablement-plan.md` (owner decision 2026-10-23, #168; `menhir-agent-memory-writer-plan-2026-10-23` P1b) |

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
