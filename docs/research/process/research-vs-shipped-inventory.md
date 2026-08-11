# Research vs shipped — what actually exists, and what's genuinely new

## Status

canonical (snapshot) — **re-audited 2026-06-28 (late), spot-re-audited 2026-06-29, reconciled again
2026-07-11 against `src/menhir` on `main`.**
A reconciliation of the research corpus against the shipped code so a fresh chain doesn't rebuild what
exists or assume what doesn't. This is a point-in-time snapshot; it drifts as code lands — re-audit
before trusting it.

> ## 2026-07-11 reconciliation (READ FIRST — corrects the dated deltas below)
>
> Verified against `main` @ current HEAD. Four deltas since the 06-28/29 audit:
>
> 1. **Oracle pipeline R4-R7 is now IN `src`** (Tier 3 said "nothing in src / bench-prototyped only").
>    The menhir port landed: `domain/oracles.py` (RetrievalOracle), `services/oracle_executor.py`
>    (OracleExecutor), `services/retrieval_oracles.py` (`default_oracles`), `domain/oracle_combiner.py`
>    (WeightedOracleCombiner E + LogSpaceOracleCombiner F). **Bench verdict is neutral-to-negative on
>    LongMemEval** (node-only 0.400 > full stack 0.333) — it ships default-off, not as a win. See
>    `.agent/research/menhir-research-execution-ladder.md` "Bench verdicts".
> 2. **All frontier read-side levers now default `False`** (`config/settings_model.py:278-295`) — this
>    **supersedes the 2026-06-29 delta's "defaults `oracle_ranking/intent_lens/shadow=ON`" claim.**
>    After the LME campaign proved read-time levers neutral-to-negative, the defaults were flipped OFF;
>    with no `MENHIR_FRONTIER_*` env set the recall path is byte-for-byte baseline ScoringService.
> 3. **Facet is no longer "reserved only"** — `CandidateSource.FACET` is wired as an **observe-only
>    shadow** in recall (facet Phase 1-3 landed; `recall_service.py` + `context_builder.py`). Still
>    not a default candidate generator; graduation still gated on real derived facets + ANCHORED_TO
>    coverage (24.5% live).
> 4. **The write-side consolidation arc is entirely absent below** (this snapshot predates the
>    2026-07-02 pivot). It is now the **active direction and is BUILT** — see the new Tier 4 section.
>    The doc's "net-new is three clusters" framing is now four: add write-time consolidation.
>
> Still accurate: L4 types (DECISION/FAILURE/INCIDENT) exist and L3 types (capability/policy/
> constraint/invariant) do not (`domain/artifacts.py`); `:Evidence` node exists
> (`infrastructure/artifact_repository.py`); MemoryMutator (R9) is still unbuilt.

## Tier 4 — BUILT since this audit: write-time consolidation (the current direction)

The post-LME pivot (historical thesis:
`.agent/archive/plans/aggregation-as-consolidation.md`, 2026-07-02): aggregation
is a **consolidation** problem, not a retrieval one — maintain query-sufficient state at write time so
multi-session answers are a lookup, not a fuzzy re-rank. Shipped as code, runs write-time / explicitly
(scheduler off in bench mode):

```text
concept                              code surface
-----------------------------------  --------------------------------------------------------------
D0 retrieval entropy                 services/view_entropy.py (View-reachability probe),
  (the objective function)           infrastructure/view_repository.py, mcp/tools/ops/view_entropy.py
D1 QuantState                        services/quantstate_consolidator.py (LLM perception -> deterministic
  (supersedable counter/register)    fold -> in-graph counter), infrastructure/quantstate_repository.py
Event -> Fold -> View frame          one event-log/projection boundary + a fold library;
                                     .agent/architecture.md, .agent/data_models.md
event fold                           services/windowed_fold.py
agent-experiential counters          services/failure_counter_bridge.py, instability_counter_bridge.py
  (FailureEvent -> QuantState)       (no re-ingest, no LLM; commit f8dd8ab)
```

The three July owner plans (`aggregation-as-consolidation.md`, `quantstate-agent-counter.md`, and
`event-fold-view-architecture.md`) are historical decision records under `.agent/archive/plans/` as
of 2026-08-10. Track W in `.agent/research/menhir-research-execution-ladder.md` is the current status
authority. D0/W2's reported counting-slice delta lives in
`archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`; live architecture lives in
`.agent/architecture.md` and `.agent/data_models.md`.

> 2026-06-29 delta (SUPERSEDED re: defaults by the 2026-07-11 block above): the frontier portions are
> now **wired into production recall** (no longer "all gated") — `recall_service._apply_frontier` runs
> the oracle combiner + warden gate over the ScoringService survivors, ~~shipped defaults
> `oracle_ranking/intent_lens/shadow=ON`~~ (**now all default OFF**), each `MENHIR_FRONTIER_*`-
> overridable. Branch since merged to `main`. See the "Still genuinely NEW" + checklist edits below.

### Update 2026-06-28 (late) — what landed this session (moves Tier 3 -> EXISTS)

The earlier snapshot below predates this session. Now built (frontier branch
`claude/menhir-chain-handoff-doc-7iuat2`), pure-domain + bench, production wiring still gated:

```text
L4 institutional overlay     domain/artifacts.py (Decision/Failure/Incident + Evidence),
  (was Tier 3)               infrastructure/artifact_repository.py (first-class :Evidence node via
                             SUPPORTED_BY), services/artifact_service.py + memory_oracle_service.py.
                             NOTE: institutional types live in domain/artifacts.py, NOT memory_types.py
                             (the re-audit checklist below greps the wrong file — fixed in checklist).
R3 belief currentness        domain/belief.py currentness_bucket + intent-aware packet (HISTORICAL_ONLY/
  + the WARDEN layer         ANERGIC_CURRENT/BLOCKED); domain/warden.py = the DECIDE layer (peer of
                             Oracle/Mutator): CurrentnessWarden/ExhaustionWarden/ScopeWarden + WardenChain.
R3 rung E exhaustion         domain/exhaustion.py (== control-rails Guard 6 RetrievalExhaustionPenalty).
R3 rung F structural exp.    domain/structural_expansion.py (bounded blast radius + guards).
Chronostratum signal layer   domain/temporal.py (bitemporal clock model + intent + ingestion-order),
                             domain/git_staleness.py (ancestry/branch/stash/rename-correct),
                             domain/repo_snapshot.py (durable file identity, Rev 5).
StructureTemporalOracle      domain/structure_temporal.py (time-aware blast radius; the killer query).
```

**Still genuinely NEW after this session:** the oracle pipeline R4-R7 menhir-side port (ported into
production recall as of 2026-06-29 — see delta above — but unproven on a labeled corpus; the bench A-E
ladder is still owed); L3 semantic types (capability/policy/constraint/invariant — only L4 institutional
types exist); ColdStartBrief + Context Engine; CostAwareOracleScheduler + R10/R11 amplification/rerank.

**No longer NEW (landed 2026-06-29):** wiring the wardens + oracle combiner + intent lens into PRODUCTION
recall. `recall_service._apply_frontier` consumes the oracle/warden stack; `warden_gate` ships OFF
(agent-written store -> evidence sparse -> would over-refuse) but is wired and env-flippable.

---

### Original snapshot (pre-session, retained for the tier structure)

## Why this exists

The corpus describes a "Semantic OS" vision; the code has shipped a lot of the substrate. Without this
map, the docs read as all-greenfield and you'd re-litigate machinery that's already there. The honest
finding: **the net-new engineering is narrow — three clusters — and one is already bench-prototyped.**

## Tier 1 — EXISTS (shipped code, reuse it)

```text
concept                              code surface
-----------------------------------  --------------------------------------------------------------
Layer 1/2 structural foundation      ingest_project, structural_anchoring, structure_queries,
                                     query_structure; ANCHORED_TO edges
the blended relevance scorer         scoring_service.score_candidates: relevance = similarity +
  (== combiner condition E)          adjacency + recency + prominence + conflict  (one fused score)
R1 hybrid candidate gen + priors     domain/retrieval_tuning.py, services/hybrid_retrieval.py
  (landed, default-off)              (weighted_rrf, CandidateSource, source-aware floor)
CANDIDATE review tier                infrastructure/candidate_repository.py (scope='CANDIDATE',
                                     promote_candidate/delete), services/candidate_service.py
                                     (approve/reject + contradiction check), explorer review surface,
                                     mcp add_candidate
conflict / supersession              conflict_status, `contradicts` edge, resolution-history + cooldown
                                     (conflict-resolution-history-proposal.md)
consolidation / decay / scope tiers  ConsolidationRepository (Entity decay, session promotion,
                                     SESSION->PERSISTENT->PROMOTED)
trust / provenance                   source_confidence (1.0/0.9/0.5), source, user_flagged
memory types (+ per-type policy)     domain/memory_types.py (EPISODIC/SEMANTIC/PROCEDURAL/PREFERENCE/
                                     IDENTITY/TEMPORAL/SPATIAL)
belief DOMAIN model                  domain/belief.py: BeliefHead, RecallBucket (SAFE_TO_ASSERT/
                                     MENTION_WITH_UNCERTAINTY/CONFLICT_SET/DO_NOT_ASSERT),
                                     EvidenceSignal/Polarity, BeliefScorer.classify
                                     (SUPERSEDED -> DO_NOT_ASSERT)
v1 infra                             circuit breakers, budget caps, embedding cache, MCP server (23 tools)
```

## Tier 2 — PARTIAL (substrate exists, not wired / not named)

```text
belief integration into live recall  belief.py model is built; plugging it into recall/scoring/
  (ladder R3)                         lifecycle is planned
knowledge-promotion lifecycle         realized today across scope + source_confidence + conflict_status
                                      + freshness — NOT one clean `status` enum
the MemoryMutator write boundary      the write OPS exist scattered (promote_candidate, decay,
                                      conflict-resolve, delete); not a named/enforced single writer
evidence                              belief-evidence as a SCORING signal exists (belief.py
                                      EvidenceSignal/BeliefEvidence); a first-class Evidence GRAPH NODE
                                      does not
facet                                 bench-only (archolith_bench/facet); CandidateSource.FACET is a
                                      reserved enum value, not wired into recall
```

## Tier 3 — NEW (genuinely absent from `src`)

```text
the oracle abstraction                RetrievalOracle interface, OracleExecutor, OracleCombiner
  (R4-R7)                             (log-space role logits), OracleResult/OraclePacket
                                      — bench-PROTOTYPED only (archolith_bench/oracle), nothing in src
task-level oracle runtime             OracleInput/OracleFinding, primitive/composite oracles,
                                      ColdStartOracle
ColdStartBrief + Context Engine       the task-shaped brief artifact + the packaging layer
first-class Evidence node             an `:Evidence` graph entity with derived_from (no label today)
institutional / L3 artifact TYPES     Decision/Incident/Failure; capability/policy/constraint/invariant
                                      (memory_types has none of these)
LLM semantic-node proposer            the emitter that proposes L3/L4 candidates
R8 SelfReinforcementGuard             Guards 1-7 BUILT (domain/self_reinforcement.py + exhaustion.py +
  (control rails)                     diversity.py + wardens in warden.py), default-off bench-gated
rerank/amplify (R10/R11)              CostAwareOracleScheduler, CrossEncoderRerankOracle,
                                      OracleAmplifiedRetrieval, MeasurementBudgetGate
parked/speculative                    connected-data-substrates, tracehead/braidtrace, cognitive-replay
```

## The clean boundary — what's *actually* new is three clusters

1. **The oracle pipeline (R4-R7).** Decompose the shipped fused scorer (`scoring_service` ≈ condition E)
   into named oracles + a bounded executor + an explicit combiner, and add F (log-space role logits).
   *New structure over existing signals; already bench-prototyped + ablation/validator-tested.*
2. **The L3/L4 semantic overlay.** New artifact **types** + a first-class **Evidence node** + an **LLM
   proposer** + the **ColdStartOracle / Brief / Context Engine** — layered onto the existing
   candidate/belief/conflict/decay substrate (Tier 1/2). Decided in `docs/roadmap/l3l4-hybrid-sketch.md`.
3. **CostAwareOracleScheduler + amplification/rerank (R10 / R11).** Genuinely new, and bench-gated / optional.
   (R8 SelfReinforcementGuard — Guards 1-7 — is BUILT as of 2026-06-29, default-off bench-gated.)

Everything else in the corpus is **wire / extend / consolidate**, not invent. Of the full vision, the
net-new is narrow: cluster #1 is prototyped, cluster #2 is mostly types+Evidence+proposer over shipped
machinery, cluster #3 is optional.

## Consolidation vs re-architecture (a useful sub-distinction)

```text
MemoryMutator (R9)   = mostly CONSOLIDATION: wrap existing write ops behind one boundary + the new
                       enforced invariant "only the Mutator writes; oracles never write in evaluate()".
oracle pipeline      = RE-ARCHITECTURE: the shipped scorer is ONE fused score (similarity+adjacency+
  (R4-R7)              recency+prominence+conflict); the oracle layer pulls those apart into inspectable
                       per-signal oracles + an explicit combiner. Signals reused; structure new.
```

## Re-audit checklist (when this drifts)

```text
- oracle PIPELINE code?   ls src/menhir/services + src/menhir/domain | grep -iE 'combiner|executor|retrieval_oracle|oracles'  (NOW PRESENT: domain/oracles.py, services/oracle_executor.py, services/retrieval_oracles.py, domain/oracle_combiner.py; bench verdict neutral-to-negative, default-off. memory_oracle_service is the separate L4 read oracle)
- Evidence node?          grep -rn ':Evidence' src/menhir                                  (NOW: artifact_repository.py + schema.py)
- institutional types?    grep -nE 'DECISION|INCIDENT|FAILURE' src/menhir/domain/artifacts.py   (NOW: present; NOT in memory_types.py)
- L3 semantic types?      grep -nE 'CAPABILITY|POLICY|CONSTRAINT|INVARIANT' src/menhir/domain/artifacts.py  (today: none — grep ArtifactType members, NOT lowercase: "invariant" appears ~15x in docstrings, all false positives)
- belief wired to recall? grep -rn '_apply_frontier\|enable_warden_gate' src/menhir/services  (NOW: recall_service._apply_frontier runs the oracle combiner + warden gate; default oracle_ranking/intent_lens ON, warden_gate OFF — gated by config, not by absence)
- facet wired?            grep -rn 'CandidateSource.FACET' src/menhir/services             (NOW: observe-only shadow wired in recall_service.py + context_builder.py; not a default generator; graduation gated on real derived facets + ANCHORED_TO coverage)
- control rails R8?       ls src/menhir/domain | grep -iE 'reinforcement|exhaustion|diversity' + grep -n ContradictionWarden src/menhir/domain/warden.py  (Guards 1-3,5,6 in self_reinforcement.py + exhaustion.py + wardens; Guard 4 = diversity.py; Guard 7 = ContradictionWarden; all default-off bench-gated)
```
