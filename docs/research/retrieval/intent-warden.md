# Intent-aware retrieval — the Intent Warden (graduated → IntentOracle)

## Status

supported-by-eval

The bench gate is cleared: archolith-bench shows intent
changes the top artifact *for the right reason* without harming baseline retrieval, and the
result is embedder-invariant (`archolith_bench/intent/`, bench `1bf31fa`/`d3811a2`). The
component shipped as `IntentOracle` (RELEVANCE family) in `default_oracles()` (menhir
`c979ca4`, domain port `dcf795e`). This doc was originally written design-only/bench-first;
that gate has since graduated. The "Warden" framing resolved to an oracle (see the
oracle-vs-warden pairing rule below).

> **2026-07-11:** `IntentOracle`'s own bench graduated (this `supported-by-eval` stands), but the
> oracle-ranking path it runs under (`frontier_oracle_ranking`) ships **default-off**, and the aggregate
> oracle stack benched neutral-to-negative on LongMemEval; the active build direction is write-time
> consolidation.

**Question this answers:** the retrieval stack reasons about semantic similarity, temporal
validity, scope, evidence, and structure. It does *not* yet reason about *"which candidate
best helps the user accomplish THIS specific task?"*. The same knowledge is a great hit for
one task and mediocre for another. This doc designs a deterministic (no-LLM) component that
supplies an *intent relevance opinion*, and determines whether it deserves to be a
first-class peer of the existing components.

---

## 0. Architectural reconciliation (read this first)

The handoff calls the existing components "Wardens." In the *shipped* code there are two
distinct object types, and the distinction decides where intent belongs:

| Type | Contract | Examples (in code) | Verb |
|------|----------|--------------------|------|
| **Oracle** | `evaluate(query, candidate) -> OracleResult(probability, polarity, target)`; read-only; composed by a combiner into role logits | `SemanticOracle`, `StructureOracle`, `ScopeOracle`, `TemporalOracle`, `EvidenceOracle` (`services/retrieval_oracles.py`) | **ranks** |
| **Warden** | `evaluate(ctx) -> WardenVerdict(admit/flag/attenuate/refuse)`; composed by `WardenChain` (most-restrictive-wins) | `CurrentnessWarden`, `ExhaustionWarden`, `ScopeWarden`, `EvidenceAnchorWarden`, `OracleAdmissionWarden` (`domain/warden.py`) | **decides** |

The five components named in the handoff (Semantic/Structure/Scope/Temporal/Evidence) are
**Oracles** in code. The handoff's own success criterion — *"these should not retrieve the
same top artifact"*, *"expected top artifacts should change"* — is a **ranking** outcome, not
an admission outcome. By the shipped taxonomy, intent relevance is therefore an **Oracle**
concern.

**Determination (answers the handoff's closing question):** intent-aware retrieval *does*
deserve to be first-class, but as an **IntentOracle** (a new RELEVANCE-family oracle), not as
a standalone Warden. The one job a Warden could add — surfacing superseded/historical
attempts for "have we tried this" — is achieved more cleanly by having the intent classifier
**feed the existing temporal `QueryIntent`** (one producer flows downhill) than by adding a
gate that fights `CurrentnessWarden`.

The design below is written so the **producer is identical either way** (classifier + role
deriver + intent x role matrix). Only the *consumer* differs — Oracle (recommended) vs Warden
(the handoff's framing). Both are specified; §1 and §6 carry the recommendation, §7 carries
the Warden form for completeness. **The combiner is not redesigned and no second supersession
logic is introduced** (the two explicit non-goals).

---

## 1. Component design

### 1.1 Inputs

- `QueryContext` (existing, `domain/oracles.py`): `text`, `intent`, `repo`, `files`,
  `symbols`, `tests`, ...
- `CandidateMemory` (existing): `id`, `content`, immutable `metadata` (the prefetched
  snapshot: `artifact_type`, `artifact_anchors`, `evidence_kinds`, temporal fields,
  belief bucket, ...).

No new fetching. Like every cheap oracle, it reads ONE class of evidence off the snapshot.

### 1.2 Outputs

A single immutable opinion value object:

```
IntentOpinion(
    intent:      TaskIntent          # classified once per query
    cue:         str | None          # the phrase that triggered it (explainability)
    confidence:  IntentConfidence    # HIGH | LOW
    role:        ContentRole         # the candidate's derived role
    status:      LifecycleStatus     # CONSUMED from temporal/belief, never re-derived
    affinity:    Affinity            # PREFER | NEUTRAL | PENALIZE | IGNORE  (matrix cell)
    weight:      float               # affinity mapped to a [0..k] relevance weight
    reason:      str                 # one line, deterministic
)
```

As an **Oracle** the opinion is emitted as
`OracleResult(target=RELEVANCE, source_family="intent", probability=weight_norm,
polarity=SUPPORT|NEUTRAL, note=reason)` — i.e. it joins the combiner as just another
relevance family (§6). As a **Warden** the same opinion maps to a verdict (§7).

### 1.3 Deterministic algorithm

```
intent, cue, confidence = classify_intent(query.text)        # §2, keyword cascade
if confidence is LOW:                                         # never distort on a guess
    return IntentOpinion(intent, cue, LOW, role, status,
                         affinity=NEUTRAL, weight=1.0, reason="low-confidence intent -> neutral")
role   = derive_content_role(candidate.metadata)             # §3, from artifact_type/anchors/evidence
status = lifecycle_status(candidate.metadata)                # CONSUMED: temporal_role + belief bucket
affinity = INTENT_ROLE_MATRIX[intent][role]                  # §4
weight   = affinity_to_weight(affinity)                      # PREFER 1.5 / NEUTRAL 1.0 / PENALIZE 0.5 / IGNORE 0.0
return IntentOpinion(intent, cue, HIGH, role, status, affinity, weight,
                     reason=f"{intent}:{role}={affinity} (cue={cue})")
```

Every step is a table lookup or a keyword match. No model, no embedding, no randomness — the
output is a pure function of `(query.text, candidate.metadata)`, so it is unit-testable and
benchable to the cell.

**Status is consumed, not recomputed.** `lifecycle_status` reads what `domain/temporal.py`
(`temporal_role`) and `domain/belief.py` (`RecallBucket`) already produced for the candidate;
the Intent component never decides currentness. This is the ladder's *one producer, many
consumers* rule — the same rule `TemporalOracle` and `CurrentnessWarden` already obey.

### 1.4 Explainability model

The opinion is self-describing: classified `intent` + the literal `cue` that triggered it
(mirrors `TemporalIntent.cue`), the candidate's derived `role` and `status`, the exact matrix
cell (`affinity`), the resulting `weight`, and a one-line `reason`. A reviewer can read any
ranking change as *"query classified DEBUG_FAILURE (cue='failing'); candidate role=DECISION;
matrix cell PENALIZE; weight 0.5; demoted."* No opaque score ever appears.

---

## 2. Intent taxonomy

Prefer few stable categories over many fragile ones. Seven task intents, plus an explicit
default. They are the *task* axis and are **orthogonal to** the existing *temporal* axis
(`classify_temporal_intent` -> CURRENT_BELIEF / AS_KNOWN_AT / AS_OF_WORLD) and to the
`QueryIntent` (CURRENT/HISTORICAL/CONFLICT). They compose; §6 shows TaskIntent *feeding* the
temporal axis.

| TaskIntent | Plain question | Signature cues (deterministic) |
|------------|----------------|--------------------------------|
| `DEBUG_FAILURE` | why is X broken right now | "failing", "error", "crash", "broken", "not working", "stack trace", "why is ... failing" |
| `AVOID_REPEAT` | have we already tried this | "already tried", "have we tried", "did we try", "again", "previously attempted" |
| `EXPLAIN_DECISION` | why did we choose X | "why did we", "rationale", "reason for", "chose", "decided", "why do we" |
| `VERIFY_CURRENTNESS` | is X still true / is the doc current | "still", "up to date", "out of date", "stale", "accurate", "still true" |
| `EVIDENCE_LOOKUP` | what verifies / covers / proves X | "which benchmark", "which test", "what verifies", "what covers", "proof of", "evidence for" |
| `CHANGE_ANALYSIS` | what changed / blast radius | "what changed", "blast radius", "what depends on", "impact of", "since <ref>", "diff" |
| `PLAN_NEXT_ACTION` | what should I do next | "what next", "next step", "what should i", "how do i", "todo" |
| `UNDERSTAND_SYSTEM` | how does X work (default) | "how does", "what is", "explain how", or *no cue matched* |

**Extensibility principle (resolves design-question #2).** The taxonomy is sized for
single-purpose intents over a **data-driven matrix**, not for a small count. Each intent means
exactly one thing; adding a task type is a new matrix **row**, adding an artifact kind is a new
**column**, and *no consumer code changes* (the oracle reads the table). That is the most
extensible shape, so `EVIDENCE_LOOKUP` is split out from `VERIFY_CURRENTNESS` rather than
overloaded onto it ("which benchmark verifies X" is a different task from "is X still true").
The handoff's `DOC_DRIFT_REVIEW` still folds into `VERIFY_CURRENTNESS` (doc drift =
currentness verification whose subject has `ContentRole.REFERENCE`). `UNDERSTAND_SYSTEM` is the
**safe default**: a near-neutral row, so an unclassifiable query never distorts baseline rank.

**Classifier = keyword cascade returning a SET** (the `classify_temporal_intent` pattern,
generalized for multi-hit — see §4A): it collects *every* intent whose signature cue matched,
each with its cue, and returns `(intents: list[(TaskIntent, cue)], confidence)`. The
precedence `AVOID_REPEAT` -> `DEBUG_FAILURE` -> `EXPLAIN_DECISION` -> `VERIFY_CURRENTNESS` ->
`EVIDENCE_LOOKUP` -> `CHANGE_ANALYSIS` -> `PLAN_NEXT_ACTION` -> default `UNDERSTAND_SYSTEM`
only orders the *primary* label for display/logging; affinity maxes over the whole set, so
precedence is not load-bearing for ranking. (DEBUG vs EXPLAIN are still separable by content:
"why did we choose the floor" has a decision verb and no failure symptom.) `confidence = HIGH`
when any multi-word signature cue matched; `LOW` only on the bare default — LOW short-circuits
to neutral (§1.3), the intent-axis version of *missing != falsity*.

---

## 3. Artifact roles

The handoff's role list mixes **two orthogonal axes**; keeping them separate is what prevents
re-implementing supersession.

**Axis A - ContentRole** (new; derived deterministically from the snapshot):

| ContentRole | Derived from |
|-------------|--------------|
| `FAILURE` | `artifact_type == "failure"` |
| `DECISION` | `artifact_type == "decision"` |
| `INCIDENT` | `artifact_type == "incident"` |
| `TEST` | `evidence_kinds` has `test`, or anchors are test files |
| `BENCHMARK` | anchors/content reference a bench arm/suite |
| `EXPERIMENT` | bench arm / ablation / "tried X" record |
| `PLAN` | anchor under `.agent/plans/` |
| `RUNBOOK` | doc/runbook anchor |
| `EVIDENCE` | raw `:Evidence`-style node (git/log/symbol change) |
| `REFERENCE` | doc/wiki/explainer (default) |

**Axis B - LifecycleStatus** (CONSUMED, never re-derived): `CURRENT` / `SUPERSEDED` /
`HISTORICAL`, read from `temporal_role` + `RecallBucket`. The Intent component **must not**
compute this — it only *reads* it. The handoff's "Current Belief / Superseded Belief /
Historical" are this axis and already have a producer.

The Intent x Role matrix keys on **Axis A**. Axis B is handled by routing TaskIntent into the
existing temporal `QueryIntent` (§6) so that the two intents which *want* history
(`AVOID_REPEAT`, `VERIFY_CURRENTNESS`) get it from the currentness policy that already exists,
instead of a parallel rule.

---

## 4. Intent x ContentRole affinity matrix

Cells: **P**=prefer (weight 1.5), **.**=neutral (1.0), **x**=penalize (0.5), **-**=ignore
(0.0, rare). Read a row as "for this task, which roles help."

| TaskIntent \\ Role | FAILURE | INCIDENT | DECISION | EXPERIMENT | BENCHMARK | TEST | PLAN | RUNBOOK | EVIDENCE | REFERENCE |
|---|---|---|---|---|---|---|---|---|---|---|
| `DEBUG_FAILURE`     | **P** | **P** | . | . | x | **P** | x | **P** | **P** | x |
| `AVOID_REPEAT`      | **P** | . | **P** | **P** | **P** | . | x | x | . | x |
| `EXPLAIN_DECISION`  | . | . | **P** | **P** | **P** | x | x | x | **P** | . |
| `VERIFY_CURRENTNESS`| . | x | . | . | **P** | **P** | x | . | **P** | **P** |
| `EVIDENCE_LOOKUP`   | . | x | . | . | **P** | **P** | x | x | **P** | x |
| `CHANGE_ANALYSIS`   | **P** | **P** | . | . | . | **P** | x | x | **P** | x |
| `PLAN_NEXT_ACTION`  | **P** | . | **P** | **P** | . | x | **P** | . | x | x |
| `UNDERSTAND_SYSTEM` | . | x | . | . | . | x | . | . | x | **P** |

**Status preference (Axis B), expressed as TaskIntent -> temporal `QueryIntent`** (so it is
applied by the *existing* currentness policy, not a new rule):

| TaskIntent | temporal `QueryIntent` | effect on superseded/historical |
|------------|------------------------|---------------------------------|
| `AVOID_REPEAT` | `HISTORICAL` | first-class (the past attempt IS the answer) |
| `VERIFY_CURRENTNESS` | `CONFLICT` | surface current + superseded side by side (the drift) |
| all others | `CURRENT` | superseded suppressed/flagged as today |

This is the load-bearing inversion: `AVOID_REPEAT` and `VERIFY_CURRENTNESS` are the only
intents that *want* history, and they get it by setting the temporal intent the currentness
policy already understands — never by a second supersession computation.

Worked example (handoff's five queries, identical topic "cosine floor"):

| Query | TaskIntent | Expected top role |
|-------|-----------|-------------------|
| "Why did we choose the source-aware floor?" | EXPLAIN_DECISION | DECISION |
| "Have we already tried lowering the floor?" | AVOID_REPEAT | EXPERIMENT (incl. superseded) |
| "Is the old floor document still current?" | VERIFY_CURRENTNESS | CURRENT belief + REFERENCE drift |
| "Which benchmark verifies this?" | VERIFY_CURRENTNESS | BENCHMARK |
| "What should I do next?" | PLAN_NEXT_ACTION | PLAN |

Same topic, same symbols -> five different top artifacts, each by a traceable matrix cell.

---

## 4A. Multiple hits

"Multiple hits" shows up in three places. All three resolve with **one rule —
most-helpful-wins (max affinity)** — the ranking dual of the WardenChain's
most-restrictive-wins. One reduction, so adding intents/roles never changes the combination
logic (the extensibility payoff).

**(a) A query carries several intents.** Real queries do: *"have we already tried fixing the
failing floor?"* matches `AVOID_REPEAT` ("already tried") **and** `DEBUG_FAILURE` ("failing").
The classifier returns the *set* of matched intents (§1.3). For each candidate role the
affinity is the **max** over all matched intents:

```
affinity(role) = max( INTENT_ROLE_MATRIX[i][role] for i in matched_intents )   # P > . > x > -
```

Max, not sum: a role any active intent wants is preferred, but a query with many weak cues
cannot runaway-boost. The opinion's `reason` records which intent won the max (explainable).

**(b) A candidate carries several roles.** A doc that is both a `PLAN` and a `DECISION`:
`derive_content_role` returns a role *set*, and affinity is **max over the candidate's roles**
too. Same operator, both axes — `max over (matched_intents x candidate_roles)`.

**(c) Many candidates match (the normal case).** IntentOracle does not order candidates; it
sets each candidate's relevance **band** (prefer / neutral / penalize). Ordering *within* a
band is left to the other oracle families through the combiner — Semantic/Structure decide who
ranks first *among* the preferred artifacts. So **intent lifts the band; semantics orders
within it.** This is why intent must be an Oracle blended by the combiner, not a standalone
gate: the gate would have no within-band ordering to contribute.

**Status lens under multiple intents.** When matched intents route to different temporal
`QueryIntent`s (§4 table), the **history-wanting lens wins**: `CONFLICT` > `HISTORICAL` >
`CURRENT`. Rationale: if *any* active intent wants history surfaced, suppressing it loses what
the user asked for; surfacing extra history is recoverable (they can ignore it), suppression
is not. The currentness policy still owns what each lens does — intent only selects it.

---

## 5. Benchmark plan

The experiment is designed so that **topic is held constant and only intent varies**, which
makes any ranking change attributable to intent alone.

**Corpus** (one topic, roles spanning the matrix) — fixture `intent/intent_floor_corpus.json`:

| id | role | status | gist |
|----|------|--------|------|
| A1 | DECISION | CURRENT | "we chose the source-aware floor because per-family recall..." |
| A2 | EXPERIMENT | SUPERSEDED | "tried a flat floor at 0.2; recall@10 dropped, reverted" |
| A3 | FAILURE | HISTORICAL | "floor at 0.5 dropped rare-source facets" (since fixed) |
| A4 | BENCHMARK | CURRENT | "r1 bench verifies floor recall@10 by source family" |
| A5 | REFERENCE | CURRENT(maybe drifted) | "recall-tuning.md - floor section" |
| A6 | PLAN | CURRENT | "next: ablate floor per source family" |
| A7 | CURRENT_BELIEF | CURRENT | "floor is now a per-family rank cut, not a cosine gate" |

**Queries** (same topic, varying intent), each with an expected top-1 role:

```
Q_explain  -> A1   Q_avoid -> A2   Q_verify -> A7/A4   Q_plan -> A6
Q_debug    -> A3   Q_change-> A2/A4 (recent evidence)
```

**Arms / metrics:**

1. **baseline** (semantic-only): record top-1. Expectation: *same* artifact for every query
   (highest lexical overlap), i.e. `intent-correct@1` near chance.
2. **intent-on** (IntentOracle in the combiner): `intent-correct@1` should jump to ~1.0;
   `nDCG@5` against the per-intent gold ordering improves.
3. **shuffle-ablation** (intent labels permuted across queries): `intent-correct@1` must
   collapse back to chance. This proves the lift is the intent signal, not topic leakage.
4. **no-harm arm** (a generic `UNDERSTAND_SYSTEM` query set, off-topic mix): `nDCG@5`
   intent-on must be `>=` baseline. Intent must never degrade ordinary retrieval.

**Promotion gate** (all required): `intent-correct@1` on >> baseline, *and* shuffle-ablation
collapses, *and* no-harm holds. Until then the IntentOracle stays out of the production set
(same bench-gating as the belief currentness layer).

Runner: `archolith_bench/intent/` with `run_intent_bench` + a `--live` variant, mirroring
the r1/r3 ladder.

---

## 6. Interaction with the existing oracles (recommended consumer: IntentOracle)

IntentOracle composes with the shipped oracles **without replacing any of them and without
touching the combiner**:

- **SemanticOracle** sets the topic floor. IntentOracle emits `target=RELEVANCE,
  source_family="intent"`; in `LogSpaceOracleCombiner` it is just another relevance family,
  subject to the existing per-family cap and `1/sqrt(n)` independence discount. Because
  similarity and intent are *different families*, intent can re-rank *within* topically
  relevant candidates but the cap prevents it from resurrecting a zero-semantic candidate.
  **No combiner change** — it already blends families this way.
- **TemporalOracle / CurrentnessWarden** own LifecycleStatus. IntentOracle does **not**
  re-derive supersession; instead the classifier sets the temporal `QueryIntent` (§4 table),
  so the *existing* currentness policy surfaces or suppresses history. Each direction is owned
  once (intent decides *which* temporal lens; temporal decides *what* that lens does) -> no
  double-apply, no drift.
- **ScopeOracle / ScopeWarden** (where) and **EvidenceOracle / EvidenceAnchorWarden**
  (trust/anchor) are orthogonal to intent (what-for). They compose independently;
  DEBUG/AVOID_REPEAT *preferring* the EVIDENCE role is a content-role preference, not an
  anchor-trust claim, so it does not overlap EvidenceOracle.
- **StructureOracle** is amplified by `CHANGE_ANALYSIS` (which prefers EVIDENCE/FAILURE roles
  that tend to carry file/symbol overlap) but the two stay separate signals.
- **OracleAdmissionWarden** is untouched: IntentOracle contributes only the RELEVANCE target,
  never currentness/conflict/blocked, so admission logic is unchanged.

Net: one new producer (classifier + role deriver + matrix) surfaced as one new RELEVANCE-family
oracle + a one-line classifier->temporal-intent feed. Everything else is unchanged.

---

## 7. The Warden form (handoff framing, for completeness)

If intent is instead wired as a Warden (the handoff's literal request), the **same producer**
drives `IntentRelevanceWarden.evaluate(ctx) -> WardenVerdict`:

```
affinity = INTENT_ROLE_MATRIX[ctx.intent][role(ctx)]
PREFER   -> ADMIT
NEUTRAL  -> ADMIT
PENALIZE -> ATTENUATE(factor=0.5)        # damp, do not refuse a topically-relevant hit
IGNORE   -> ATTENUATE(factor=0.0)        # effectively drop from this task's view
```

It would slot into `WardenChain` like the others (most-restrictive-wins; ATTENUATE factors
multiply). **Why this is not recommended:** Wardens *gate*, they do not *reorder by
preference*, so a PREFER cell can only ADMIT — it cannot lift the preferred artifact to #1,
which is precisely the handoff's success criterion. A Warden can demote (attenuate) but not
promote; ranking lift requires the Oracle path. Hence the determination in §0: **ship it as an
IntentOracle.** The Warden form is viable only as a redundant safety damp and is not worth a
separate first-class object given `CurrentnessWarden`/`OracleAdmissionWarden` already cover the
hard mismatches.

---

## 8. Non-goals honored

- No implementation (design only).
- No LLM reasoning anywhere — pure keyword cascade + table lookups.
- No new oracle *in the Warden framing*; in the recommended framing the new object is an
  Oracle by the codebase's own taxonomy (the handoff's "no new oracle" reflects the merged
  mental model; §0 reconciles it). The combiner is **not** redesigned.
- No second supersession logic — LifecycleStatus is consumed from `temporal.py`/`belief.py`;
  intent only *selects the temporal lens*.

## 9. Open questions for review

1. ~~Oracle vs Warden framing~~ **Resolved / approved:** intent ships as an **IntentOracle**
   (RELEVANCE family), not a Warden. Locked by the pairing rule (only invariant dimensions —
   scope/currentness/anchoring — get a warden; intent is pure-relevance) and the fact that a
   warden cannot promote to #1. No standalone `IntentWarden`.
2. ~~Granularity~~ **Resolved:** 8 single-purpose intents over a data-driven matrix
   (`EVIDENCE_LOOKUP` split out) — the most extensible shape (§2 extensibility principle).
3. Matrix weights (1.5 / 1.0 / 0.5 / 0.0) are a transparent first guess — leave the *tuning*
   of the magnitudes to the bench, keep the *signs* (P/./x) as the human-authored contract?
