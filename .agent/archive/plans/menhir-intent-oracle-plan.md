# menhir — Intent-aware retrieval (IntentOracle) implementation plan

**Design:** `docs/research/retrieval/intent-warden.md` (read first — this plan implements that design).
**Status:** CODE-COMPLETE, SHIPPED (bench-gated default-off). Every deliverable below is built,
tested, and landed; the bench promotion gate passed (§Phase 4). IntentOracle is in
`default_oracles()` and the AssertionPipeline auto-derives the temporal lens.

> **ARCHIVED 2026-07-11 (ctharvey-approved).** All deliverables shipped and verified against
> `src/menhir`. The frontier stack ships default-off (`config/settings.py` `frontier_*=False`;
> recall wiring gated behind `tuning.enable_intent_lens`) — that is a deferred *activation*
> decision, not unfinished plan work, so the owner approved archiving this as code-complete.

**Track:** research execution ladder (sibling of the R6 cheap oracles). See
`.agent/plans/menhir-research-execution-ladder.md`.

> **Status note 2026-07-11 (code-reconciled).** Verified against `src/menhir`: `IntentOracle`
> present in `retrieval_oracles.py` and included in `default_oracles()`; `domain/query_intent.py`,
> `domain/artifact_role.py`, `domain/intent_affinity.py` all present; `AssertionPipeline` carries
> `auto_intent`/`_resolve_intent`; tests `test_query_intent.py` + `test_intent_oracle.py` present.
> Correction to Phase 2 item 5's scope note: the AssertionPipeline **is now wired into**
> `recall_service` (imports `classify_intent`/`task_intents_to_lens`; instantiated with
> `auto_intent=tuning.enable_intent_lens` at `recall_service.py:567,702`), gated behind the
> `enable_intent_lens` tuning flag rather than default-on.

---

## Note: why Intent gets an Oracle but no paired Warden

It is tempting to assume every oracle pairs with a warden. It does not — only **three of the
five** shipped oracles do:

| Dimension | Oracle | Warden |
|-----------|--------|--------|
| Scope | `ScopeOracle` | `ScopeWarden` |
| Temporal | `TemporalOracle` | `CurrentnessWarden` |
| Evidence | `EvidenceOracle` | `EvidenceAnchorWarden` |
| Semantic | `SemanticOracle` | *(none)* |
| Structure | `StructureOracle` | *(none)* |

**Pairing rule:** an oracle earns a paired warden **iff its dimension has a binary "must not
enter current-truth context" line.** Scope (wrong repo/branch -> REFUSE), currentness
(superseded asserted as current -> REFUSE/FLAG), and anchoring (synthetic-only -> REFUSE) each
have such an invariant. Semantic and Structure are **pure relevance** — a weak hit is
lower-ranked, never *unsafe* — so they have no warden.

**Intent is a pure-relevance dimension.** A DECISION artifact surfaced for a DEBUG query is
not unsafe to assert, only less helpful for the task. There is no refuse line, so there is no
`IntentWarden` — Intent ships as an **oracle only**, exactly like Semantic and Structure. This
is consistency with the pattern, not an exception to it. (A warden form is sketched in design
§7 only to show it is strictly weaker: a warden can demote but cannot promote to #1, which is
the whole point of intent-aware ranking.)

---

## Scope

Add deterministic, no-LLM intent-awareness as one new RELEVANCE-family oracle plus a
classifier that feeds the existing temporal `QueryIntent`. **No combiner redesign. No second
supersession logic.** Everything else in the stack is unchanged.

## Deliverables / phases

### Phase 1 — pure-domain producers (the shared core) — **DONE**
1. `domain/query_intent.py`
   - `TaskIntent` enum (8): `DEBUG_FAILURE`, `AVOID_REPEAT`, `EXPLAIN_DECISION`,
     `VERIFY_CURRENTNESS`, `EVIDENCE_LOOKUP`, `CHANGE_ANALYSIS`, `PLAN_NEXT_ACTION`,
     `UNDERSTAND_SYSTEM` (default). Single-purpose by design so the matrix is the one
     extension point (design §2 extensibility principle).
   - `IntentConfidence` enum (`HIGH`/`LOW`).
   - `classify_intent(text) -> (intents: list[(TaskIntent, cue)], confidence)` — keyword
     cascade returning the **set** of all matched intents (multi-hit, design §4A); precedence
     orders only the primary label. Reuses the `classify_temporal_intent` pattern (literal cue
     for explainability). LOW confidence only on the bare default.
2. `domain/artifact_role.py`
   - `ContentRole` enum (10): FAILURE, INCIDENT, DECISION, EXPERIMENT, BENCHMARK, TEST, PLAN,
     RUNBOOK, EVIDENCE, REFERENCE (default).
   - `derive_content_role(metadata) -> ContentRole` — from `artifact_type` / `artifact_anchors`
     / `evidence_kinds`. Pure, deterministic.
3. `domain/intent_affinity.py`
   - `Affinity` enum (PREFER/NEUTRAL/PENALIZE/IGNORE) + `INTENT_ROLE_MATRIX` (the 8x10 table,
     design §4) + `affinity_to_weight(affinity) -> float`.
   - `resolve_affinity(matched_intents, candidate_roles) -> (Affinity, winning_intent)` — the
     multi-hit reduction: **max over (matched_intents x candidate_roles)** (most-helpful-wins,
     design §4A). Single operator covers both "several intents" and "several roles"; the winner
     is returned for the explainable `reason`.
   - `task_intent_to_query_intent(matched_intents) -> QueryIntent` (the status-routing table,
     §4: AVOID_REPEAT->HISTORICAL, VERIFY_CURRENTNESS->CONFLICT/neutral, else CURRENT). Under
     multiple intents the **history-wanting lens wins**. **Bench correction (Phase 4):** verify
     must select a *neutral* lens that surfaces current+superseded WITHOUT boosting the stale one
     — in the bench's 3-value combiner that is `any`; in menhir map VERIFY_CURRENTNESS to the
     CONFLICT QueryIntent (which is the neutral-surfacing one, not HISTORICAL). Routing verify to
     HISTORICAL is wrong — it makes the temporal producer boost the superseded item. This is how
     LifecycleStatus is handled WITHOUT a second supersession rule — the classifier picks the
     temporal lens; `temporal.py`/`belief.py` still own what the lens does.
   - `IntentOpinion` value object (design §1.2).

### Phase 2 — the oracle (consumer) — **DONE (item 5 deferred — see note)**
4. `services/retrieval_oracles.py` — add `IntentOracle`:
   - `evaluate(query, candidate)`: classify once into the intent set (cache on query is fine —
     pure), derive the candidate role set, call `resolve_affinity` (max over the cross-product),
     emit `OracleResult(target=RELEVANCE, source_family="intent",
     probability=normalized_weight, polarity=SUPPORT|NEUTRAL, note=reason-with-winning-intent)`.
   - LOW confidence -> neutral result (probability 0.5, polarity NEUTRAL) so an unclassifiable
     query never distorts ranking (intent-axis "missing != falsity").
   - Do **not** add to `default_oracles()` yet — bench-gated (see promotion gate).
5. Wire the classifier->temporal feed. **DONE (menhir, this commit).** Two changes:
   - `default_oracles()` now includes `IntentOracle()` — graduated after the real-embedder gate
     (bench `d3811a2`). It is a capped RELEVANCE family that returns NEUTRAL on low-confidence
     queries, so a query with no task intent is unaffected.
   - `AssertionPipeline.__init__(auto_intent=True)` + `_resolve_intent`: at the pipeline entry
     (the seam future recall wiring calls), a default `intent="current"` is replaced by
     `task_intents_to_lens(classify_intent(text))` — avoid_repeat->historical,
     verify_currentness->any, else current. An explicitly non-default intent is honored as-is; a
     no-cue query classifies LOW and stays "current" (no change). `auto_intent=False` disables it.
   - **Scope note:** the oracle pipeline (AssertionPipeline) is itself not yet wired into
     `recall_service` (it is build-first; only instantiated by callers/tests). So this makes
     intent first-class *within the oracle pipeline*; wiring the whole pipeline into live recall
     is a separate, larger task and is NOT part of the intent track.

### Phase 3 — tests (menhir side, pure) — **DONE (32 pass, ruff clean, no regressions)**
6. `tests/test_query_intent.py` — classifier: each intent's signature cues, precedence
   (DEBUG vs EXPLAIN on "why ..."), default + LOW confidence path, returned cue. **DONE.**
7. `tests/test_artifact_role.py` — role derivation per source-metadata shape. **DONE.**
8. `tests/test_intent_oracle.py` — matrix cell -> OracleResult mapping; neutral on LOW;
   read-only (no candidate mutation); same topic + different intent -> different probability;
   multi-hit max affinity; history-wanting lens wins; verify->neutral lens; IntentOracle
   excluded from `default_oracles()`. **DONE.** Existing oracle/combiner tests unmodified
   (36 still pass).

### Phase 4 — bench (archolith-bench, falsifies) — **BUILT, gate GRADUATES** (bench `1bf31fa`)
9. `archolith_bench/intent/` — **DONE.** Pure-stdlib prototype that is also the Phase 1 spec:
   `classifier.py` (8 intents, multi-hit) / `roles.py` / `matrix.py` (8x10 + `resolve_affinity`
   max-reduction + `task_intents_to_query_intent`) / `oracle.py` (IntentOracle, plugs into the
   existing `LogSpaceOracleCombiner` as one capped RELEVANCE family) / `models.py` / `metrics.py`
   / `runner.py`. Fixture `fixtures/intent_floor_corpus.json` (7 roles, one topic, 7 intent
   queries + 2 no-harm). Script `scripts/run_intent_bench.py`. Tests `tests/test_intent_oracle.py`
   (28, ruff clean). Notes: `.agent/benchmark-notes/intent-oracle-demo-run.md` (in bench repo).
   - arms: baseline (semantic-only), intent_on, **shuffle** (mean over ALL wrong intents),
     **no_harm** (full stack +/- intent — isolates intent, not the whole stack).
   - **Result (lexical stand-in — harness sanity, not a promotion decision):**
     intent-correct@1 **0.143 -> 1.000**, shuffle collapses (lift +0.309 over a random wrong
     intent), no-harm holds (0.431 >= 0.387), determinism 1.0 -> **gate GRADUATES**.
   - **Findings carried back to this plan:** (a) the headline 1.000 is role-based (top-1 carries
     a *preferred* role) — the real proof is the shuffle collapse, not the absolute. (b) no-harm
     MUST hold the oracle stack constant (+/- intent); comparing to semantic-only conflates
     intent with the whole stack. (c) **lens fix:** `VERIFY_CURRENTNESS -> "any"` (neutral lens),
     NOT historical — routing verify to historical made TemporalOracle BOOST the superseded item.
     The design's CONFLICT lens maps to the bench combiner's `any` (surface current+superseded
     without lifting the stale one). Phase 1's `task_intents_to_query_intent` reflects this.
   - **Fixture validator added** (bench `c3e6718`, `intent/validate.py`): intent-specific
     silliness guards (SINGLE-ROLE-CORPUS, NO-PREFERRED-ROLE, EXPECTED-TOP-MISMATCH,
     UNCLASSIFIED-QUERY, NO-SUPERSEDED, TOPIC-NOT-CONSTANT) + structural errors; auto-runs
     before the ladder. Shipped fixtures validate clean.
   - **Real-embedder re-run DONE** (bench `05a89da`, `intent/embedder.py`, nomic-embed on LM
     Studio): it *changed the conclusion* on the single-topic floor fixture (does NOT graduate
     under a real embedder — prose names the role, so the embedder recovers role-matching and a
     wrong intent rides the floor; shuffle won't collapse). **Fixture-design law:** carry role in
     metadata, not prose. The controlled two-topic fixture (within-topic text identical) graduates
     **identically under lexical AND embedder** (intent_on 0.714 vs shuffle 0.429) — IntentOracle's
     contribution is embedder-invariant. Gate re-confirmed.
   - **Still owed before production:** more topics/roles + de-bias baseline id-tiebreak (bench
     polish); then the gated menhir integration below.

## Bench promotion gate (all required before adding to `default_oracles`)
- `intent-correct@1` (intent-on) >> baseline, AND
- shuffle-ablation collapses `intent-correct@1` back to chance (proves it is the intent
  signal, not topic leakage), AND
- no-harm: `nDCG@5` intent-on >= baseline on a generic `UNDERSTAND_SYSTEM`/off-topic set.

## Composition guarantees (from design §6 — verify, do not weaken)
- IntentOracle is one RELEVANCE family in `LogSpaceOracleCombiner`; the existing per-family cap
  + `1/sqrt(n)` discount keep it from dominating SemanticOracle. **Combiner code unchanged.**
- Status handled by feeding temporal `QueryIntent`; `TemporalOracle`/`CurrentnessWarden`
  untouched. No supersession recomputed.
- Orthogonal to Scope/Evidence/Structure; `OracleAdmissionWarden` untouched (Intent only
  contributes the RELEVANCE target, never currentness/conflict/blocked).

## Out of scope / non-goals
- No LLM. No combiner redesign. No `IntentWarden` (see Note). No new supersession logic.
- Real embedder for SemanticOracle is a separate gated item.

## Open questions (design §9)
1. **Resolved / approved:** ships as an IntentOracle, not a Warden (pairing rule). No
   `IntentWarden`. Phase 1 is unblocked.
2. **Resolved:** 8 single-purpose intents over the data-driven matrix (`EVIDENCE_LOOKUP` split
   out) — the most extensible shape.
3. **Resolved by Phase 4:** signs (P/N/X/-) are the human contract; magnitudes are the bench's
   first calibration — PREFER=1.0, NEUTRAL=0.25, PENALIZE=0.05, IGNORE=0.0 (`intent/matrix.py`).
   These graduate with the rung; re-tune on the real-embedder run.

**COMPLETE.** Design resolved; Phase 4 bench graduates under three backends incl. OpenAI;
Phases 1-3 ported; **production integration DONE** — IntentOracle is in `default_oracles()` and
the AssertionPipeline auto-derives the temporal lens. 70 oracle/intent/pipeline tests green,
ruff clean. The only thing NOT done (and out of this track's scope) is wiring the whole oracle
pipeline (AssertionPipeline) into `recall_service` — that is a separate effort; intent rides it
for free once it lands.
