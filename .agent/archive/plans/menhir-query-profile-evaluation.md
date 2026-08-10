# menhir Query Profiling — Evaluation & Smallest-Composition Recommendation

**Type:** research / evaluation (NO implementation). **Status:** evaluation complete.
**Project:** menhir (`menhir-frontier`). **Created:** 2026-06-29.

## Context

A handoff proposed a deterministic **Query Profiler -> QueryProfile** layer that runs
*before* the oracles and reconfigures retrieval (oracle-family weights, temporal lens,
preferred roles, budget, explanation). The follow-up refined the question after review:

> The question is no longer "should we add query profiling?" but
> **"what is the smallest composition layer that adds measurable value without
> duplicating the existing IntentOracle pipeline?"** -- with retrieval *budgets* as the
> primary new interest, an explicit "Menhir should get simpler" constraint, and
> "IntentOracle already solves this" stated as a *successful* outcome.

This document answers that. It is bench-first and recommends **rejecting** the proposed
new scoring layer, **adopting** a thin immutable composition object, and **bench-gating**
exactly one genuinely-new knob (a scalar per-intent candidate budget).

## Verdict (read this first)

1. **Do not build a parallel QueryProfiler/QueryProfile scoring layer.** ~70% of the
   proposal already ships, and the missing pieces mostly *duplicate* the IntentOracle.
2. **Query-aware retrieval already exists, by design, as evidence -- not config.** Intent
   enters as a capped, independence-discounted, *explainable* `OracleResult` from the
   already-graduated `IntentOracle` (`source_family="intent"`), and wardens stay
   authoritative. That is precisely the "no opaque pre-oracle scoring layer" the handoff
   wants -- it is the existing architecture.
3. **`QueryProfile` is worth keeping only as a lightweight immutable composition object**
   over the deterministic outputs that already exist (`classify_intent`,
   `task_intents_to_lens`, the `INTENT_ROLE_MATRIX` projection, the per-source cues). It
   adds **explainability and one threaded value**, *no new scoring*. This is the "simpler"
   outcome and is preferred.
4. **Per-query oracle-family-weight modulation should be rejected** unless an ablation
   proves independent value, because it has >=5 double-counting / non-linearity paths that
   route through the IntentOracle and the combiner cap -- the exact "additional opaque
   scoring" the constraint forbids.
5. **The one budget idea worth a bench is a *scalar* per-intent `candidate_k`** (cast a
   wider/narrower net by task) -- the only proposed knob that can move **latency**, and the
   only one that does not re-weight the existing signal. **Role-targeted** budget allocation
   (spend budget on tests vs decisions) is **blocked** on a role-faceted candidate source
   that does not exist yet (`CandidateSource.FACET/STRUCTURE` are reserved, ungenerated), so
   it is out of scope here.

## 1. Ruthless inventory -- what already exists

| Capability | Exists? | Location | Remaining gap |
|---|---|---|---|
| Task-intent classification (DEBUG_FAILURE, AVOID_REPEAT, EXPLAIN_DECISION, VERIFY_CURRENTNESS, EVIDENCE_LOOKUP, CHANGE_ANALYSIS, PLAN_NEXT_ACTION, UNDERSTAND_SYSTEM) | **COMPLETE** | `domain/query_intent.py::classify_intent` (multi-hit, ordered cue cascade, confidence, per-hit cue) | None. Taxonomy matches the handoff; handoff's DOC_DRIFT_REVIEW / BENCHMARK_INTERPRETATION fold into VERIFY_CURRENTNESS / EVIDENCE_LOOKUP. |
| Temporal lens routing (current / historical / any; include-invalidated) | **COMPLETE** | `domain/temporal_intent.py::classify_temporal_intent`, `domain/intent_affinity.py::task_intents_to_lens`; consumed in `oracle_combiner._ranking_score` and `TemporalOracle` | None. Wired via `enable_intent_lens` (P3). |
| Intent->role preference (preferred_roles) | **COMPLETE** | `domain/intent_affinity.py::INTENT_ROLE_MATRIX` (8x10), `resolve_affinity` (max over intents x roles), `affinity_to_weight` | None as *scoring*. "preferred_roles" as a separate steering field would duplicate this. |
| Candidate-level intent scoring (the "oracle routing" the proposal wanted) | **COMPLETE & SHIPPED** | `services/retrieval_oracles.py::IntentOracle` in `default_oracles()`; graduated bench `d3811a2`/`05a89da`/`1bf31fa` (lexical + nomic + OpenAI) | None. Capped + independence-discounted "intent" family; NEUTRAL on low-confidence. |
| Content-role derivation | **COMPLETE** | `domain/artifact_role.py::derive_content_role` (artifact_type + anchors + evidence_kinds -> role set) | None. |
| Oracle-family weighting | **GLOBAL CONSTANT** | `domain/oracle_combiner.py::FAMILY_ALPHA` {semantic .8, structure 1.0, temporal 1.0, scope .6, evidence 1.2}; `TARGET_LAMBDA`, caps | **Per-query** modulation does not exist. This is the proposal's one new *scoring* knob -- see s3/s4. |
| Candidate budget (how many to fetch) | **GLOBAL CONSTANT** | `recall(..., candidate_k=50, limit=10)` in `services/recall_service.py` | **Per-intent** budget does not exist. Cheap scalar knob (s3a). |
| Role-targeted candidate budget (spend on tests/failures/decisions) | **NOT POSSIBLE TODAY** | candidate gen is fused vector+BM25 + file-context + pending; `CandidateSource.FACET/STRUCTURE` reserved but **ungenerated** | Needs a new role-faceted candidate generator first. Out of scope. |
| Oracle execution ordering / early-stop / per-query oracle subset | **DOES NOT EXIST** | `services/oracle_executor.py` runs all (cand x oracle) pairs; stable order is for *reduction determinism* only | A real knob, but low value: cheap deterministic oracles, latency dominated by graph IO not oracle CPU (s3c). |
| Explanation generation | **PARTIAL (per-component)** | `IntentHit.cue`, `TemporalIntent.cue`, `IntentOpinion.reason`, `OraclePacket.rationale`, warden `reason` | No single bundled query-level "why we retrieved this way" object. This is the real, cheap win (s3d / s5). |
| Topic-fixed / intent-varied benchmark | **COMPLETE** | `archolith-bench/archolith_bench/intent/` (`runner.py`, `metrics.py`, `validate.py`, `fixtures/intent_floor_corpus.json`); arms baseline / oracle-default / intent_on / shuffle / no-harm; gate intent-correct@1 | Reusable as-is; extend with arms for any new knob (s6). |
| Warden authority (safety unaffected by routing) | **COMPLETE** | `domain/warden.py` (`WardenChain` most-restrictive-wins, REFUSE vetoes regardless of score); runs observe->rank->**decide** after the combiner | None. Any profile must not touch this. |

Bottom line: the only rows that are not already "complete" are **per-query family
weights**, **per-intent candidate budget**, **role-targeted budget** (blocked),
**execution ordering/early-stop** (low value), and a **bundled explanation object**.

## 2. Where the proposal collapses into what exists

- The handoff's "Query Profiler classifies the query; QueryProfile reconfigures oracles"
  is, in menhir terms, *already* split correctly: `classify_intent` is the query producer,
  `IntentOracle.evaluate` is the per-candidate consumer. The intent-warden design doc
  (`docs/research/retrieval/intent-warden.md`) already states "the profiler classifies the QUERY;
  the IntentOracle scores a CANDIDATE ... the profiler is the input producer; the oracle is
  the per-candidate scorer," and already defines an immutable `IntentOpinion`.
- "Oracle routing -> change relevance emphasis, never override safety" = the IntentOracle
  emitting RELEVANCE-only support (never CONTRADICT, no paired warden) while wardens keep
  veto power. Already true.
- "preferred_roles per profile" = the `INTENT_ROLE_MATRIX` row, already consumed as
  evidence. Re-expressing it as a steering field re-introduces the signal a second time.

So a "QueryProfile" that carried `oracle_weights` + `preferred_roles` would **re-emit the
intent signal the IntentOracle already emits**, at a coarser, less explainable altitude.

## 3. The genuine delta -- each candidate, verified

**(a) Scalar per-intent candidate budget -- REAL, cheap, the only latency lever.**
Today `candidate_k=50` and `limit=10` are flat. A profile could set, e.g.,
DEBUG_FAILURE/CHANGE_ANALYSIS -> wider `candidate_k` (recall-hungry, blast-radius);
UNDERSTAND_SYSTEM/PLAN_NEXT_ACTION -> narrower (precision/orientation). This is the **only**
proposed knob that changes work done: graphiti search + `fetch_candidate_metadata` +
adjacency + oracle fan-out all scale with `candidate_k` (see the `_t_phases` breakdown in
`recall`). It does **not** re-weight the existing signal, so it has **no double-counting
path**. Independent, falsifiable, latency-relevant.

**(b) Role-targeted candidate budget -- BLOCKED, out of scope.** "Spend budget on tests /
failures / decisions / ADRs" requires fetching candidates *by role*. Candidate generation
today is semantic+BM25 + structural file-context + pending injection -- there is no
role-indexed retrieval. `CandidateSource.FACET` / `STRUCTURE` are reserved but ungenerated.
Building a role-faceted source is a separate, larger project; do not fold it in here.

**(c) Oracle execution ordering / early-stop -- REAL but low-value.** No early-stop exists;
all pairs run. But the cheap oracles are pure CPU and the executor already bounds
concurrency and degrades on timeout; recall latency is dominated by graph IO, not oracle
CPU. Skipping oracles per intent also risks non-determinism and loses the "missing !=
falsity" uncertainty signal. Defer unless profiling shows oracle CPU is material.

**(d) Bundled query-level explanation -- REAL, cheap, no risk.** Cues exist per component
but are never assembled into one "why this query retrieved this way" record. A composition
object that carries `[task_intent+cue, temporal_lens+cue, budget, ...]` is pure
observability over already-computed values. This is the safe, simplifying win.

**(e) Per-query family-weight modulation -- REAL but high-risk; see s4.**

## 4. Family-weight modulation -- every double-counting / opacity path

Hypothesis: let a profile scale `FAMILY_ALPHA` per query (e.g. DEBUG_FAILURE -> structure
x1.3, temporal x1.2, evidence x1.1). Risk inventory (all verified against
`oracle_combiner.py` + `retrieval_oracles.py`):

1. **Intent^2 (direct).** The combiner treats "intent" as a source family. Scaling the
   intent family alpha by an intent-derived factor literally squares the IntentOracle's
   contribution.
2. **Preferred-role <-> family bump (indirect, strong).** IntentOracle already lifts
   Failure/Test/Evidence relevance for DEBUG_FAILURE. Those roles *carry* structure +
   evidence anchors, so bumping structure/evidence alpha lifts the **same** artifacts a
   second time. Role preference and family are correlated by construction.
3. **Temporal lens <-> temporal alpha.** VERIFY_CURRENTNESS already routes the lens to "any"
   and the TemporalOracle changes polarity by lens. Also bumping temporal alpha compounds
   one underlying signal at two altitudes.
4. **Cap non-linearity.** `MAX_FAMILY_CONTRIBUTION=3.0` caps per-family support. A bumped
   family hits the cap sooner, so the multiplier's effect is partially swallowed and
   becomes candidate-dependent -- i.e. opaque and hard to calibrate.
5. **Independence-discount non-linearity.** Magnitude already includes `1/sqrt(n)` per
   family; multiplying alpha interacts non-linearly with the discount and the contradiction
   penalty `D = lambda * q^gamma`. The knob's real effect is not legible from its value.

Conclusion: family-weight modulation is the literal "additional opaque scoring" the
constraint forbids, and most of its intended effect is **already delivered, transparently,
by the IntentOracle**. Reject unless s6 proves independent lift.

## 5. Recommended smallest composition layer

A frozen, deterministic value object assembled at the recall entry point from existing
producers -- **no new scoring, no combiner change, no candidate-gen change**:

```
QueryProfile(frozen):
    task_intents:  tuple[TaskIntent]      # classify_intent(text) -- existing
    temporal_lens: str                    # task_intents_to_lens(...) -- existing
    candidate_k:   int                    # the ONE new knob, scalar, bench-gated (s3a/s6)
    explanation:   tuple[str]             # bundled cues: intent cue + lens cue + budget reason
```

- It **carries**, it does not **re-score**: `oracle_weights` and `preferred_roles` are
  deliberately **absent** (they would duplicate the IntentOracle / matrix).
- Replaces three loose values threaded separately today with one explainable object; the
  IntentOracle and combiner are untouched.
- `candidate_k` ships **off / neutral** (a single default, == today) until the bench in s6
  graduates per-intent values, mirroring the `MENHIR_FRONTIER_*` off-by-default discipline
  in `menhir-frontier-toggle-wiring.md`.

If even the scalar budget fails to graduate, the honest outcome is: **QueryProfile is a
pure explanation/composition object** -- which the constraint explicitly calls a success.

## 6. Required ablation (bench-first, reuse `archolith_bench/intent/`)

Topic-fixed / intent-varied, mirroring the existing `IntentBenchmarkRunner` arms and the
handoff's "cosine floor" example (one topic; queries vary only in intent). Add arms so each
new knob is isolated **against the already-graduated `intent_on` baseline**, not the
semantic strawman:

1. `baseline` -- semantic only (existing floor).
2. `oracle_default` -- full stack, **no** IntentOracle (existing control).
3. `intent_on` -- **current shipped-frontier baseline** (existing).
4. `+family_mod` -- intent_on + per-query family weights.
5. `+budget` -- intent_on + scalar per-intent `candidate_k`.
6. `+both` -- intent_on + family weights + budget.

**Independent-value rule:** a knob graduates only if its arm beats `intent_on` by a margin
**and** `+both` shows the two knobs are additive (not redundant). If `+family_mod ~=
intent_on`, family modulation is double-counting -> **reject** (the s4 prediction). Keep the
existing `shuffle` (wrong-intent collapse) and `no_harm` (nDCG must not drop) guards, plus
the `validate.py` fixture invariants and the determinism check.

Budget needs a metric the current fixtures don't carry: **recall@k vs latency/candidate
cost**. Add a recall-oriented fixture (some answers only appear past the default
`candidate_k`) so wider budgets can show recall lift, and record candidate count + a
modeled cost as the latency proxy (the lexical bench has no live graph).

## 7. Promotion criteria

Graduate a knob into a (still off-by-default) profile field only if, on the bench:

- **Gains:** up intent-correct@1 / first-action quality (role@1), up stale suppression,
  up wrong-role suppression -- *independently* of `intent_on` (s6 rule).
- **Budget specifically:** up recall@k **and** down (or neutral) candidate cost / modeled
  latency vs the flat default.
- **No regressions:** no recall regression on no-harm/orientation queries; determinism
  holds; `shuffle` collapses.
- **No opacity:** the knob must be explainable in `QueryProfile.explanation` and must not
  re-emit a signal an existing oracle already emits (the s4 gate).
- **Wardens untouched:** routing changes emphasis only; REFUSE/scope/evidence authority is
  unchanged.

Anything that fails these is rejected, not shipped "because it's elegant."

## 8. Follow-up (not implemented here)

This task makes **no** oracle, combiner, executor, or recall changes -- it concludes
*toward simplification*. If pursued later:

1. Implement `QueryProfile` as the frozen composition/explanation object in s5 (carries
   `task_intents`, `temporal_lens`, scalar `candidate_k`, bundled `explanation`); thread it
   at the recall entry point in place of the three loose values. No combiner change.
2. Build the s6 ablation in `archolith_bench/intent/`: add `+family_mod`, `+budget`,
   `+both` arms and a recall/latency fixture; gate every new knob against `intent_on`.
3. Family-weight modulation and role-targeted budget remain **rejected / blocked** until
   (respectively) the ablation proves independent lift and a role-faceted candidate source
   (`CandidateSource.FACET/STRUCTURE`) exists.

## Evidence map

Claims above are traceable to: `domain/query_intent.py`, `domain/temporal_intent.py`,
`domain/intent_affinity.py`, `domain/artifact_role.py`, `domain/oracle_combiner.py`
(`FAMILY_ALPHA`, caps), `services/retrieval_oracles.py` (`IntentOracle` in
`default_oracles`), `services/oracle_executor.py` (no early-stop),
`services/recall_service.py` (`candidate_k`/`limit`, `_apply_frontier`),
`domain/retrieval_tuning.py` (`CandidateSource.FACET/STRUCTURE` reserved) -- all in this
repo (`menhir-frontier`) -- and `archolith_bench/intent/` (runner, metrics, fixtures, gate)
in the sibling `archolith-bench` repo.
