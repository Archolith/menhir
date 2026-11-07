---
artifact_schema: 1
artifact_uuid: 2c1f4a76-5b83-4f0e-9d6a-71e8c3a5b204
artifact_type: plan
artifact_status: IMPLEMENTED
about: menhir typed-scalar perception (C.4) / ScalarStateView fold
---

# Typed-scalar boolean/categorical separation: the entailed-boolean shadow

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

**Disposition:** IMPLEMENTED (option (a) landed as e8418ce8; (c2) entailed-boolean suppression withdrawn after section 8 measurement), residuals tracked in Archolith/menhir#367 (was: ACTIVE, partial)
Last verified: 2026-10-21 - DONE (medium); anchors checked: commit e8418ce8 (fix(scalar) prompt no longer prefers boolean over named value); no entailed_boolean / scalar_entailment.py in src, which matches section 8's position that (c2) should not be built on the original premise. Evidence for (a) is one graph, k=3.

**Scope:** design investigation only. No source was modified; no LLM call, ingest, or benchmark was run;
the graph was not touched.

**Repo anchor:** Menhir, branch `master`.
All line numbers below were read from the working tree on 2026-09-09.

---

## 1. The defect, restated precisely

One sentence admits two legal readings under the typed-scalar taxonomy:

| reading | slot_key `(attribute, scope, value_kind, unit)` | informativeness |
|---|---|---|
| `python_tutorial_platform = "Pluralsight"` | `(python_tutorial_platform, '', status, '')` | names the value |
| `uses_pluralsight = true` | `(uses_pluralsight, '', boolean, '')` | value absorbed into the attribute |

The asymmetry is entailment: `platform=Pluralsight` entails `uses_pluralsight=true`; the reverse does not
hold. The boolean is a strictly less informative **shadow** of the categorical.

The two readings are *different slots*, so every existing consistency mechanism is blind to the
relationship:

* `gate_typed_scalars` (`typed_scalar_rules.py:1089`) votes **within** a claim group; groups come from
  `_claim_groups` (`:942`), keyed on `source_key` or (with `align_spans=True`) on a common span
  intersection **within one episode**. Two readings that quote different spans are two groups, each of
  which can independently reach unanimity.
* `fold_assertions` (`domain/scalar_state_fold.py:117`) buckets by `_slot_of(row)` and emits one state per
  slot. Two slots, two `scalar_state` Views, no interaction.
* `_reconciled_identity` (`:910`) can reconcile a disagreeing **attribute name** but only for members
  already inside one claim group, and it never touches `value_kind` — `_interpretation_label` (`:706`)
  documents that `value_kind` is deliberately never droppable from the vote.

So the defect is genuinely **cross-claim-group**, and the user's diagnosis of the gate's structural
blindness is correct.

---

## 2. Anchor verification (requested: correction, not confirmation)

### Verified exactly as stated

| claim | verdict |
|---|---|
| `TYPED_SCALAR_SYSTEM_PROMPT` starts ~line 150 | **true** — line 151 |
| `value_kind` is a flat 9-way enum | **true** — prompt line 168; `domain/typed_assertion.py:46` `VALUE_KINDS` |
| clause (h) boolean "INCLUDING completion or possession states" | **true** — lines 245-247 |
| clause (i) status = "categorical NON-boolean, NON-weekday state" | **true** — line 248 |
| every (h)/(i) example is single-reading | **true** — `I have a passport`, `I no longer smoke`, `married`, `red`, `employed` |
| prompt offers `'owned'` as an attribute-family example | **true** — line 164 |
| prompt forbids the time-axis version (`previous_/current_account_balance`) | **true** — line 166 |
| `extract_typed_scalars_once` at ~610 | **true** — line 609 |
| pre-gate drops surfaced through `on_drop` | **true** — and the reason set is larger than quoted (see below) |
| `_activity_scalar_admission_reason` is called from row admission with `attribute/value_kind/operation/source_context/event_context` in scope | **true** — definition `:1393`, call `:862` |
| `is_ambiguous_exact` at ~1438, word-boundary + documented conservative bias | **true** — line 1438, bias documented at `:1446` |
| `gate_typed_scalars` at ~1089; `_claim_groups` at ~942; `align_spans=True` still buckets `by_episode` | **true** — `:967` |
| `TypedScalarProposal.slot_key` at ~333; `source_key` at ~344; `claim_ordinal` pinned to 0 | **true** — 333 / 344, pinned at `:330` and `:906`, rationale at `:306-315` |
| drop reasons land in `consolidation_audit` → telemetry `lifecycle_events`, read with `scripts/inspect_consolidation_audit.py`, gated on `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED` | **true** — emit at `typed_scalar_service.py:861`; env read at `infrastructure/consolidation_audit.py:41` |
| `align_spans=True` in production | **true** — hardcoded at `typed_scalar_service.py:876` |
| perceiver v2 | **true** — `settings_model.py:307` default `"v2"` |

### Corrections

**C1 — "There is no equivalent rule against the attribute absorbing the VALUE." Partly wrong.**
The prompt already contains one, at line 279:

> `Prefer the shortest natural attribute ('finished_reading', not 'finished_reading_dune')`

This *is* the anti-absorption rule, but it is (a) filed under **CONSISTENCY**, framed as vote-agreement
hygiene rather than as a semantic constraint; (b) phrased as a soft *preference*; and (c) only covers the
shape where the object is **appended** to a real property name. It says nothing about the shape where the
object replaces the property name entirely (`uses_pluralsight`). The fix is therefore a *relocation and
hardening* of an existing clause, not a new concept — which is a cheaper and less risky prompt edit than
introducing an unprecedented rule.

**C2 — The ordering instruction actively causes the defect; this is stronger than "circular".**
Clause (i) ends with: `Only fall through to status when none of (a)-(h) fit.` Since (h) is boolean and (i)
is status, the prompt **explicitly instructs the model to prefer boolean over status** whenever both fit.
The defect is not merely an underspecified boundary — the specified boundary points the wrong way relative
to the entailment asymmetry. Any prompt fix must invert this clause, not just add an example.

**C3 — The prompt is internally inconsistent at this exact boundary, and the deterministic lane disagrees
with it.** Clause (i) offers `'married'` as its status example, while clause (h)'s ordering and
`'I have a passport'` example would route `I am married` to boolean; the unit fixture at
`tests/test_typed_scalar_perception.py:217` encodes `married` as **boolean**, while
`deterministic_scalar_extractor.py:432` classifies `married|employed|unemployed|retired` as a **status**
cue. Three surfaces, two answers. Worth fixing in the same pass so the deterministic router and the LLM
lane cannot disagree about a promoted class.

**C4 — "A new reason would fit this architecture" is right about the pattern, wrong about the
function.** `_activity_scalar_admission_reason` (`:1393`) takes `attribute, value_kind, operation,
source_context, event_context` and immediately returns `None` unless
`value_kind == "count"` **and** `attribute` ends in `_count` (`:1404-1409`). It receives neither `value`
nor `stated_span`, so a boolean guard (which must exempt `value is False` and inspect the source quote's
capitalization) **cannot** be an extension of it. It must be a **sibling** function called from the same
site. The good news: at `:862` the call site already has `value` (computed `:831`, validated `:833`),
`stated_span`, `content`, `span_start`, `span_end` all in scope, so the sibling needs no plumbing.

**C5 — The quoted gate audit line does not show a mutual veto; it shows the status *winning*.**
The reported distribution is `{"boolean||absolute|true|": 1, "status||absolute|Pomodoro Technique|": 2}`
with `agreement 0.667`. In `gate_typed_scalars` the winner is the **unique modal real** interpretation
(`:1199-1220`): 2 > 1, so there is no `TS_VETO_TIE`, and `0.6667 >= 2/3` passes the threshold check at
`:1203`. At the stated threshold of exactly 2/3 that claim **commits the status and discards the boolean**
— the intra-group vote resolved the ambiguity *correctly*. (The empty attribute field in both labels also
confirms `reconcile_attribute` was on.) At the production default `threshold=1.0` the same 2-1 split is a
`TS_VETO_SELF_CONSISTENCY` and **both** readings are lost.

This matters for the design: when the two readings land in one group, the existing gate already does the
right thing at 2/3 and the wrong thing (losing the fact entirely) at 1.0. I could not re-derive the
"2 states written" figure without re-running the measurement, so I am flagging the prose/evidence
discrepancy rather than asserting the run was misread. **If** the reasoning-ON case really did lose both
readings, the cause is the unanimity threshold, not a mutual veto — and no fold-time suppression can
recover a fact the gate refused.

**C6 — the pre-gate drop-reason list is larger than quoted.** `parse_scalar_row` emits
`not_an_object`, `missing_required_field`, `bad_attribute_or_operation`, `bad_value_kind`,
`bad_optional_field`, `bad_episode_index`, `unresolvable_episode`, `value_failed_validation`,
`malformed_when`, `span_not_uniquely_located`, `activity_*`, `hedged_value`, `temporal_*`; plus the
response-level `malformed_json` / `not_a_json_array`. Immaterial to the design, but a new reason joins a
15-label vocabulary, not a 4-label one.

---

## 3. Survey: how widespread is this?

### 3.1 The LLM lane is the only producer

The deterministic lane cannot emit a value-absorbed boolean. `_SCALAR_CUE_RE`
(`deterministic_scalar_extractor.py:424-447`) recognises `boolean` only from a literal `true|false` token,
and its `semantic_boolean` group (`I have finished|completed|read|watched|seen|visited`) is explicitly
**cue-completeness reporting with no template**, so it "never produce[s] deterministic authority"
(`:434-436`). Therefore the defect is confined to the probabilistic path, and a fix there cannot regress
the deterministic router or its promoted classes.

### 3.2 Tests that encode the current permissive behaviour

Only two test files mention `value_kind="boolean"` at all:

* `tests/test_typed_scalar_perception.py:217, :230-233` — `test_each_of_the_nine_kinds_round_trips_with_real_grounding`
  feeds a **canned** LLM response and asserts the parser admits one row of each of the nine kinds,
  including `attribute="married", value_kind="boolean"`. This **records parser behaviour**, not taxonomy
  intent: the model is stubbed, so a prompt change cannot affect it. A pre-gate guard *would* break
  `len(out) == 9` if it dropped `married=true` — under the predicate proposed in §5 it does not, because
  no named value appears in the span and no status sibling carries `married` as its value.
* `tests/test_typed_scalar_perception.py:245-250` — `test_value_coercion_rescues_common_near_misses`,
  same shape, asserts `"true" -> True` coercion. Behaviour-recording, unaffected.
* `tests/test_typed_assertion_repository.py:162, :169` — `validate_value("boolean", ...)` type discipline.
  Asserts **intent** (an `int` is not a `bool`) and is untouched by anything proposed here.

**No test anywhere asserts that a value-absorbed boolean should commit.** A repo-wide search for
`uses_*`, `has_passport`, `is_vegetarian`, `finished_reading*` as attribute literals in `src/`, `tests/`
and `scripts/` returns exactly one hit — the prompt string at `:279`. Test blast radius for the
recommended option is therefore effectively zero (see §5.4).

### 3.3 Prior art under `.agent/`

No plan, review, or handoff addresses boolean-vs-status or entailed-slot redundancy. What exists and is
relevant:

* **`ViewRepository.retire_counters_superseded_by_scalar`** (`infrastructure/scalar_view_repository.py:372`,
  run from `services/scalar_consolidation.py:216`) — the **closest precedent**: a deterministic,
  namespace-scoped, idempotent, cross-subsystem retirement of a *less authoritative duplicate of the same
  fact*, with an explicit ambiguity abstention (`WHERE size(slots) = 1`), an in-place retirement that
  keeps the version for audit, and a `retired_reason` label. Its bridge rule is **provenance-linked, not
  textual**: `a.episode_uuid IN c.episode_uuids AND a.subject_display = c.view_subject AND
  toFloat(a.value) = toFloat(c.view_value)`. This is the shape a boolean-shadow suppression should imitate.
* **`domain/scalar_view_suppression.py:8-11`** — the governing owner decision on suppression:
  *"The suppression source is provenance-linked ONLY (owner decision) ... No fuzzy text/slot matching — a
  candidate with no such provenance edge is never touched."* This is the **house constraint** any lexical
  predicate must answer to, and it is the basis of the strongest objection in §6.
* **`.agent/handoffs/HANDOFF-2026-07-19-scalar-state-C4-continuation.md:23-28`** — the spine invariant:
  *"perception may be probabilistic; folds and Views must stay deterministic."* A suppression implemented
  in the fold satisfies it; one implemented in the prompt does not (it only reduces the rate).
* **Menhir memory, score 0.66 (> 0.6 action threshold):** *"The typed-scalar perception path is missing the
  canonicalization stage the counter path has."* — recorded 2026-07-28, and this defect is an instance of it.
* `.agent/archive/plans/aggregation-as-consolidation.md:166` — the project's objective function is
  `Entropy(q) = size/spread of the MINIMAL evidence set that STILL ENTAILS the answer`. Two Views for one
  fact is an entropy regression by the project's own metric, and the shadow boolean is by construction
  never in the minimal sufficient set when the categorical is present. This is the strongest
  *motivational* argument for fixing it at all.

### 3.4 Not determined

I did not query the live graph for committed boolean slots whose attribute embeds a proper noun. The task
constrained me to treat the graph as read-only evidence and I had no read-only Neo4j session available in
this lane, so **the production prevalence of the pattern is unmeasured**. See §7.

---

## 4. Options evaluated

### (a) Prompt-only: make boolean the fallback when no named value exists

* **Correctness:** addresses the true cause. Clause (i)'s `Only fall through to status when none of
  (a)-(h) fit` must be inverted at the (h)/(i) boundary, and line 279's soft preference hardened into a
  rule ("the attribute names the QUESTION, the value is the ANSWER; if the fact has a nameable value, the
  attribute must not contain it").
* **Determinism:** none. Sampled k times at temp>0; the two readings will keep splitting, just less often.
  Worse, a *partial* improvement is actively harmful at `threshold=1.0`: moving the split from 3-0 to 2-1
  converts a wrong-but-committed fact into a **vetoed** one — the fact disappears instead of being
  downgraded. Measured recovery numbers in `gate_typed_scalars`'s docstring (`:1134-1139`) show how
  sensitive commit rate is to exactly this kind of key disagreement.
* **Blast radius:** one string. But it is the shared prompt for every namespace, it is part of the
  perception contract, and changing it warrants a `personal_memory_scalar_state_perceiver_version` bump
  (the convention is documented at `settings_model.py:328, :337`), which invalidates frozen sample
  captures used by `scripts/freeze_scalar_samples.py` and the A/B instruments.
* **Cannot fix:** anything already committed; any residual split; and it provides no invariant — only a
  rate change. Also cannot fix C3's three-way inconsistency on its own (the deterministic cue table must
  change too).
* **Verdict:** necessary, not sufficient. Keep it, but not as the guarantee.

### (b) Deterministic pre-gate drop, sibling of `_activity_scalar_admission_reason`

A new reason, e.g. `value_absorbed_into_attribute`, returned from a sibling guard called at `:862`.

* **Correctness:** the predicate available span-locally is weaker than the real one. Without the
  categorical sibling in scope, the guard must infer "this attribute embeds a value" from the boolean row
  alone — e.g. the attribute contains a token that appears **capitalised** in the episode text and is not
  a function word. That is a proper-noun heuristic, and it has a fatal asymmetry: **if the boolean is
  dropped and the categorical reading does not commit (vetoed at `threshold=1.0`, or never emitted), the
  fact is lost entirely.** Dropping the only surviving witness to a fact is strictly worse than storing a
  redundant shadow of it.
* **Determinism:** excellent — span-local, pure, all k samples decide identically, exactly the
  `hedged_value` / `activity_*` precedent.
* **Blast radius:** small and well-precedented; a new audit label; no schema change.
* **Cannot fix:** anything already in the graph; any case where the attribute embeds a lowercase common
  noun (`uses_pomodoro_technique` when the source writes "pomodoro technique" lowercase); and it cannot
  know whether the better reading survived.
* **Verdict:** right mechanism, wrong information. Viable only as a **conditional** variant of (c) — which
  is not expressible pre-gate, because the other claim group's outcome is not yet known. Reject as primary.

### (c) Structural separation / entailment suppression (cross-claim-group)

Two sub-variants, and the distinction is load-bearing:

**(c1) Post-hoc View retirement, mirroring `retire_counters_superseded_by_scalar`.**
Attractive by precedent — but **broken for this case**. `retire_counters_superseded_by_scalar` retires a
`view_kind:'counter'` View, which the scalar fold does not own and therefore never rebuilds. A
`scalar_state` boolean View is **re-materialised by the very next** `rebuild_scalar_state(subject_uuid)`
(`scalar_state_service.py:238-265`), which writes one View per `result.states` entry and retires only
slots *absent from the freshly-folded desired set* (`:307-313`). An external retirement of a live folded
slot would flap: retired by consolidation, recreated by the next rebuild, forever. **Reject c1.**

**(c2) Suppress inside the fold's desired set — recommended.** A pure, cross-slot post-pass over
`FoldResult` that moves the entailed boolean state from `result.states` into `result.abstentions` with a
new reason code. This is correct *by construction* because the existing machinery already does the rest:

* the slot leaves `desired_slots`, so any existing boolean View is retired on the next rebuild
  (`:307-313`);
* `_retirement_reason` (`:134-144`) already maps "slot in `result.abstentions`" to `"abstain"`, so the
  retirement is labelled, not silent;
* live and rebuild share one fold (`fold_entity`, `:187`), so parity holds by construction — the spine
  invariant from the C.4 handoff;
* nothing is deleted from the `:TypedAssertion` log, so the decision is **fully reversible**: turn the
  flag off, rebuild, and the boolean View returns. No migration, no backfill.
* **Only one production call site** (`scalar_state_service.py:187`). `fold_assertions` itself stays
  untouched, so its 24 tests in `tests/test_scalar_state_fold.py` are unaffected.

* **Correctness:** sees both readings' *outcomes*, so it can only ever remove a shadow when the more
  informative reading actually survived. Cannot lose a fact.
* **Determinism:** pure function of the fold's input rows; same rows → same suppression; replay-stable.
* **Cannot fix:** the case where the categorical never commits (that is the prompt's and the threshold's
  job); and the `scalar_history` projection (see §5.5).
* **Verdict: recommended**, paired with (a).

### (d) Other options considered

**(d1) Entailment-aware vote collapse in the gate** — treat a boolean-shadow reading and its entailing
categorical as the *same* reading inside a claim group, so the 2-1 split becomes 3-0 for the categorical
and commits even at `threshold=1.0`. This is the within-group complement of (c2) and the only thing that
fixes the "both readings lost to unanimity" case. It requires relaxing the documented invariant that
`value_kind` is never dropped from `_interpretation_label` (`:739-742`) — but that invariant's stated
rationale ("constrained vocabularies where disagreement is a genuine difference of reading") is precisely
what fails here: boolean-vs-status at this boundary is a taxonomy artifact, not a difference of reading.
High value, higher risk; **propose as a measured follow-on**, gated separately, after (a)+(c2) land and
the audit trail quantifies the residual.

**(d2) Taxonomy change: split `boolean` into `boolean_intrinsic` and `boolean_derived`, giving derived
booleans their own slot namespace** — rejected. It does not reduce View count (a derived boolean in its
own lane is still a View); it widens `VALUE_KINDS` (`typed_assertion.py:46`), `ScalarStateKind`, the
prompt enum, and every persisted slot key; and it pushes the same judgment call onto the model under a
new name.

**(d3) Raise the threshold / rely on the gate** — rejected on the user's own structural argument, and
C5 shows the gate already behaves correctly *within* a group; the grouping is the gap.

---

## 5. Recommendation

**Adopt (c2) as the invariant and (a) as rate reduction. Defer (d1) behind its own flag.**

### 5.1 The invariant, in one sentence

> For one subject, a folded `value_kind="boolean"` state whose value is `true` and whose attribute
> lexically contains the whole normalized value of a co-episodic folded `value_kind="status"` state for
> the same subject is an entailed shadow of that state and must not materialize a View — and no other
> boolean state may ever be suppressed.

### 5.2 Exact files and functions to change

| file | change |
|---|---|
| `src/menhir/domain/scalar_state_fold.py` | add reason code `ENTAILED_BOOLEAN_SHADOW = "entailed_boolean_shadow"` beside `NO_ANCHOR` / `AMBIGUOUS_ANCHOR` / `DELTA_ON_RANGE` (`:38-41`). **Do not** modify `fold_assertions`. |
| `src/menhir/domain/scalar_entailment.py` *(new, pure, offline)* | `suppress_entailed_boolean_shadows(result: FoldResult) -> FoldResult` — the whole predicate, plus a small token normalizer. New module rather than an edit to `scalar_state_fold.py` so the per-slot fold's contract and its 24 tests stay intact. |
| `src/menhir/services/scalar_state_service.py` | in `fold_entity` (`:178-187`), wrap the single `return fold_assertions(rows, as_of=as_of)` so the suppression post-pass runs when the flag is on. This is the **only** production fold call site, so live and rebuild inherit it together. |
| `src/menhir/config/settings_model.py` | `personal_memory_scalar_entailed_boolean_suppression: bool = False` + `MENHIR_PERSONAL_MEMORY_SCALAR_ENTAILED_BOOLEAN_SUPPRESSION` env read, following `:373` / `:864`. Wire it into `ScalarStateService` construction. |
| `src/menhir/services/typed_scalar_rules.py` | prompt only: invert clause (i)'s fall-through (`:248-249`) so **boolean is the fallback** when the fact has no nameable value; promote `:279`'s soft preference into a KIND-ROUTING rule ("the attribute names the question, the value is the answer"); add the negative example pair (`uses_pluralsight=true` vs `python_tutorial_platform="Pluralsight"`) next to the existing `previous_account_balance` prohibition at `:166`. Resolve C3 by making `married` unambiguously a **status** in clause (i) and the (h) examples. |
| `src/menhir/services/deterministic_scalar_extractor.py` | only if C3 is fixed: confirm the `status` cue group (`:432`) and the prompt now agree on `married|employed|unemployed|retired`. |
| docs | `.agent/CHANGELOG.md`; a line in `.agent/default-off-features.md` (the flag ships off). |

### 5.3 The predicate, stated so it can be implemented without judgment

Suppress folded boolean state `B` iff **all** of:

1. `B.value_kind == "boolean"` and `B.value is True`. (`False` is never suppressed — §5.4.)
2. There is exactly **one** folded state `C` in the same `FoldResult` with
   `C.subject_uuid == B.subject_uuid`, `C.value_kind == "status"`, and conditions 3-5 satisfied. **If two
   or more distinct `C` qualify, abstain from suppressing** — mirroring
   `retire_counters_superseded_by_scalar`'s `WHERE size(slots) = 1`.
3. **Provenance link:** `set(B.episode_uuids) & set(C.episode_uuids)` is non-empty. The readings must be
   co-extracted from a shared source episode. This is what keeps the rule inside the house policy
   (`scalar_view_suppression.py:8-11`) rather than being free-floating text matching.
4. **Lexical entailment:** every token of `normalize_scalar(C.value)` (lowercased, split on
   non-alphanumerics, tokens of length ≥ 3, stop-words removed) appears as a token of `B.attribute`
   (split on `_`), and the token set is non-empty. `uses_pluralsight` ⊇ {`pluralsight`} ✓;
   `uses_pomodoro_technique` ⊇ {`pomodoro`, `technique`} ✓; `has_passport` vs `car_color="red"` ✗.
5. **Authority:** `evidence_rank(B.effective_tier) <= evidence_rank(C.effective_tier)`
   (`typed_assertion.py:81`). A user/manual-tier boolean is never suppressed by an agent-tier status.

On suppression: drop `B` from `result.states` and append
`Abstention(slot_key=B's 5-tuple, reason=ENTAILED_BOOLEAN_SHADOW)`, carrying the suppressing slot in the
audit detail so `inspect_consolidation_audit.py` can answer "which categorical retired which boolean".

### 5.4 Counterexamples that must not regress (each becomes a unit test)

| case | why it survives |
|---|---|
| `has_passport = true`, no status slot ("I have a passport") | condition 2 finds no `C`. |
| `is_vegetarian = true` alone | no `C`. (If `diet="vegetarian"` *also* commits from the same episode, suppression is the intended outcome — the categorical is strictly more informative.) |
| `smokes = false` / `no_longer_smokes = false` | condition 1: `value is False` is exempt. **Negation carries information a categorical cannot express** — `platform="Pluralsight"` does not entail `uses_udemy=false`. |
| `uses_pluralsight = false` co-occurring with `python_tutorial_platform = "Pluralsight"` | condition 1 exempts it; the contradiction stays visible rather than being silently resolved. |
| `finished_reading = true` with `currently_reading = "Dune"` | condition 4: `{dune}` ⊄ `{finished, reading}`. |
| `owned = true` with `car_color = "red"` | condition 4 fails. |
| boolean and status from **different episodes** with no shared episode | condition 3 fails — suppression is co-extraction-scoped, not global. |
| status slot **expired** ("I used to use Pluralsight") | the fold yields an `Expiry`, not a state (`scalar_state_fold.py:154-175`), so no `C` exists and the boolean survives. |
| two statuses match one boolean ("I use Pluralsight and Udemy") | condition 2's uniqueness abstention. |
| user-tier `uses_pluralsight = true` vs agent-tier status | condition 5. |

### 5.5 Known gap to close in the same change

`scalar_history` is built by a **separate** pure function — `build_history(rows, ...)` over
`materializable_assertions_for_entity` (`scalar_state_service.py:378-380`), not from `FoldResult`. A
fold-level suppression therefore still leaves a **recallable `scalar_history` View** for the suppressed
boolean slot whenever `personal_memory_scalar_history_enabled` is on (default `False`,
`settings_model.py:373`). Note also that `_blocked_slots` (`:147-156`) only blocks history for
`MALFORMED_VALID_AT` abstentions, so a new reason code does **not** automatically propagate. Either extend
the suppressed-slot set into the history projection's desired set, or explicitly document that history
retains the shadow (defensible: history is an audit surface). Decide deliberately; do not leave it
implicit.

### 5.6 How it would be proven

1. **Unit, offline, no LLM** — `tests/test_scalar_entailed_boolean_suppression.py`: the Pluralsight and
   Pomodoro cases suppress; every row of the §5.4 table survives; idempotence (applying the pass twice
   equals once); order independence (shuffle `result.states`, identical output); and a same-rows
   live-vs-rebuild parity assertion through `fold_entity`.
2. **Existing suites must stay green unchanged** — `tests/test_scalar_state_fold.py` (24 uses of
   `fold_assertions`, untouched by construction), `tests/test_typed_scalar_gate.py`,
   `tests/test_typed_scalar_perception.py`, `tests/test_cumulative_activity_scalars.py`,
   `tests/test_scalar_hedged_abstention.py`. Route via
   `query_structure("affected_tests", path=...)`; routine gate `pytest tests/ -m unit -q` (serial).
3. **Replay, no new LLM spend** — the frozen captures from `scripts/freeze_scalar_samples.py` plus
   `scripts/replay_fold_flags.py` give a flag-on/flag-off A/B on identical samples: the metric is
   *states written* and *distinct slots per fact*, which should fall, with *facts represented* unchanged.
   A fact that disappears entirely is a failed run.
4. **Audit trail** — with `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED=true`,
   `scripts/inspect_consolidation_audit.py` must show every suppression as an `abstain` retirement naming
   its suppressing slot. Zero silent suppressions.
5. **Prompt arm, measured separately** — one paid A/B on the date-smoke panel comparing the reworded
   prompt at `threshold=1.0` and `2/3`, reading *both* the commit rate and the
   `TS_VETO_SELF_CONSISTENCY` rate, because (a)'s failure mode is converting a wrong commit into a veto.
   Bump `personal_memory_scalar_state_perceiver_version` when the prompt ships.

### 5.7 Sequencing

1. (c2) fold suppression + flag + unit tests + replay A/B. Ships default-off; zero effect until enabled.
2. Prompt rework (a) + C3 consistency fix, own perceiver-version bump, own measured A/B.
3. Re-read the audit trail. Only if a material residual remains, take (d1) as a separate flagged change.

---

## 6. The strongest objection to the recommendation

**Condition 4 is fuzzy text matching, and this codebase has an explicit owner decision against exactly
that.** `domain/scalar_view_suppression.py:8-11` states that suppression authority is
*"provenance-linked ONLY (owner decision) ... No fuzzy text/slot matching."* Condition 3 adds a
provenance gate, but the thing that decides *which* categorical entails *which* boolean is still a token
subset test over a free-text attribute name produced by an LLM. The failure mode is a **false
entailment**: two genuinely distinct facts co-extracted from one episode where one attribute happens to
contain the other's value tokens — `programming_language = "Python"` alongside a real, independent
`uses_python_for_automation = true` — and the independent fact stops surfacing. Unlike a missed
suppression (which merely leaves a redundant View, the status quo), a false suppression **removes
information**, which inverts the conservative-bias discipline that `is_ambiguous_exact` (`:1446`) and
`classify_absolute_semantics` (`:1301`) both explicitly adopt.

The mitigations are real but partial: the value must be a *whole* token subset (not a substring), the
suppression is reversible without data loss (the assertion log is untouched; flip the flag and rebuild),
it is audited per event, it abstains on multiplicity, and it respects evidence tier. The honest position
is that condition 4 is a **new kind of authority** for this codebase and needs the owner's explicit
sign-off, not an appeal to the `retire_counters_superseded_by_scalar` precedent — because that precedent
matches on *numeric value equality*, never on text.

A narrower fallback if the owner refuses lexical authority: keep only conditions 1-3 and 5 plus an
*exact* requirement that `B.attribute` equals `<verb>_<normalized C.value>` for a closed, version-pinned
verb list (`uses`, `using`, `has`, `is`, `owns`, `plays`, `watches`, `reads`). Lower recall, near-zero
false-positive risk, and the same invariant shape.

---

## 7. What I could not determine

1. **Production prevalence.** No live-graph query was run (read-only-evidence constraint, no Neo4j session
   in this lane). The count of committed boolean slots whose attribute embeds a proper noun — and hence
   the real size of the win — is **unmeasured**. Run it before prioritising; a single Cypher read over
   `:TypedAssertion {value_kind:'boolean'}` joined to co-episodic `status` assertions would settle it.
2. **The reasoning-ON outcome (C5).** The quoted audit line shows a 2-1 status win, which commits at
   `threshold=2/3`; the prose says the readings vetoed each other and 2 states were written. I could not
   re-run the measurement to resolve which describes the run. This matters: if the ON case really loses
   both readings, the dominant term is the unanimity threshold, and the priority order of (a)/(c2)/(d1)
   shifts toward (d1).
3. **Whether `reconcile_attribute` was on in both measured arms.** The ON-arm label shows an empty
   attribute field (so it was on there); the OFF-arm's configuration is not recoverable from the quoted
   evidence, and it changes how the two readings group.
4. **Whether any *promoted deterministic class* could ever reach this boundary.** I verified the
   deterministic lane cannot emit a value-absorbed boolean today
   (`deterministic_scalar_extractor.py:433-444`), but I did not enumerate the configured
   `personal_memory_scalar_deterministic_classes` in any live deployment.
5. **Frozen-capture compatibility.** I did not verify that `scripts/freeze_scalar_samples.py` captures
   remain replayable across a `perceiver_version` bump; the step-2 A/B assumes they do.

---

## 8. Measured follow-up, 2026-09-09 — the defect's premise is partly WRONG, and option (a) is now landed

This section supersedes the framing in sections 1 and 5 on one load-bearing point. Written after
four live passes against the same throwaway graph, with `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED=true` so the
`extract`/`gate` audit events were readable.

### 8.1 There is NO intra-pass duplication. The plan's premise came from a measurement artifact.

The evidence that motivated this plan — a graph holding `python_tutorial_platform=Pluralsight`,
`uses_pluralsight=true` AND `using_pluralsight=true` simultaneously — was **three separate runs
accumulating in one graph**, not one pass emitting all three. Confirmed by `created_at` clustering:
22:10, 22:25 and 22:47 are three distinct consolidation passes. Each pass wrote one of them.

Within a single pass the gate behaves correctly. The competing readings DO land in the same claim
group (their spans overlap, and `align_spans=True` groups by common intersection), and one reading
wins the vote:

    reasoning ON,  ep 56965ca9:  {status Pluralsight: 2, boolean true: 1}  -> status commits
    reasoning OFF, ep 56965ca9:  {boolean true: 2, status Pluralsight: 1}  -> boolean commits

Same episode, same graph, same threshold. **The gate never committed both.**

**Consequence for the recommendation in section 5.** Fold-time entailment suppression targets a
redundancy that does not occur inside a pass. It would fire only on the rare case where two readings
reach the fold from genuinely different episodes, not on the motivating case. It is not wrong, but it
is no longer the primary fix, and it must not be implemented on the belief that it resolves the
Pluralsight example.

### 8.2 The real defect is slot INSTABILITY across passes, and in production it LOSES the fact

Which reading wins is sampling-dependent. Reasoning-on put the fact in `python_tutorial_platform`;
reasoning-off put the same fact in `uses_pluralsight`. Neither pass was wrong by its own vote. That
is the mechanism behind the slot-drift warning already in `archolith-bench/.env.example` ("a
namespace consolidated on-then-off can hold one fact under two slots — bump
`PERCEIVER_VERSION`") and behind the Menhir memory quoted in section 3.3 about the missing
canonicalization stage.

The production-relevant figure is worse than drift. Default
`personal_memory_scalar_threshold = 1.0`, so a 2-1 split commits **nothing**. Production does not get
a duplicate slot — it silently loses the fact. **Unanimity, not commit count, is the metric to
optimize.**

### 8.3 Option (a) was implemented and measured. It is landed as `e8418ce8`.

The plan rated prompt-only as drift-prone. Measured, it is better than that rating — because the
clause was not merely ambiguous, it was backwards. Clause (i) ended "Only fall through to status when
none of (a)-(h) fit" with (h) = boolean, so the prompt *instructed* boolean-preference whenever both
readings fit.

Swapped the clauses and added the missing rule: the attribute is the question, the value is the
answer. Both counterexamples from section 5.4 are stated explicitly in the prompt (negation stays
boolean; a named thing that is what the property is ABOUT becomes the subject).

Results — gpt-5.6-luna, reasoning off, k=3, same graph, prompt the only variable:

| | baseline | fix run 1 | fix run 2 |
|---|---|---|---|
| Pluralsight spans | `{boolean 2, status 1}` x2 | `{status 3}` x2 | `{status 3}` x2 |
| unanimous gates | 1 | 2 | 3 |
| boolean shadows committed | 3 | 0 | 0 |
| states written | 6 | 4 | 6 |
| samples kept | 7/6/6 | 4/10/8 | 4/6/7 |

### 8.4 Residuals, reproduced across both runs — this is what is left to fix

1. **The prompt reduces but does not eliminate the defect.** The Pomodoro span still flips:
   `{boolean 1, status 2}` in run 1, `{boolean 2, status 1}` in run 2. So a structural
   canonicalization still has a job — but it is **stabilizing the choice**, not de-duplicating two
   simultaneous commits. Re-scope option (c) accordingly before implementing it.
2. **The dominant failure moved to categorical value TEXT.** Morphological and substring variants of
   one answer now abstain: `"contribute to open-source projects"` vs `"contributing to open-source
   projects"`; `"broad understanding"` vs `"broad understanding of Python"`. This is the narrow case
   where similarity matching is the right instrument — categorical kinds ONLY, never
   count/duration/money/measurement/clock_time, where "close" is the opposite of what supersession
   needs (`"about an hour"` and `"about two hours"` are near-identical under any embedder).
3. **Perception recall is the largest remaining term, not representation.** Most abstentions are
   `(absent)` votes — a sample simply did not perceive a claim the others found. No representation
   fix addresses that. `scripts/measure_absence_band.py` already exists for it.
4. Run 1 mis-slotted two tracking tools as `tutorial_platform`; absent in run 2, so sampling noise
   rather than systematic over-application. n=2.

### 8.5 Unexplained, flagged rather than theorized

Fix run 1 reported `scalar_states_written=4` and created **zero** new View nodes; the pre-existing
`python_tutorial_platform: Pluralsight` View shows a fresh `last_accessed`. The new
`tutorial_platform` assertion appears to have converged onto the existing slot rather than creating a
second one. If that is attribute reconciliation canonicalizing against the assertion log, it partly
answers 8.2 on its own and changes the priority of option (c) — but the mechanism is unverified.
**Determine this before implementing any canonicalization work.**

### 8.6 Supporting fixes landed alongside, each needed before any of the above was measurable

- `b5be00a7` — completion cap 2048 -> 8192. Reasoning tokens are billed inside the same ceiling, so
  a reasoning model truncated one k-sample into `malformed_json`; a lost sample scores `absent`
  against every claim the others found, capping achievable agreement at 2/3. Nothing committed at all
  before this.
- `45c0afc8` — evidence-anchor invariant enforced in `ViewWriteRepositoryMixin.record()`.
  `record_scalar_history` and `record_counter` passed raw Episodic anchors to a gate that only accepts
  `evidence_finalized=true`, making those writes unsatisfiable by construction (14 refusals, 0 Views
  against 2 assertions). This is why the `FACT View write refused` defect had looked unreachable.
- `2ffae1bd`, `cf8253f3` — opt-in reasoning control (consolidation seam via `MemorySettings`;
  graphiti seam via env, inconsistency recorded).
- archolith-bench `0f40fee` — the consolidation/recall audits now default ON for every bench run.
  They were enabled only by `run_knowledge_update_buildout.sh`, which is why a DEBUG date-smoke
  produced 2,649 log lines containing none of the answers: the drop reasons and vote distributions go
  to telemetry `lifecycle_events`, never to the log.

### 8.7 Revised sequencing

1. DONE — prompt fix (`e8418ce8`).
2. Resolve 8.5. It may already supply canonicalization.
3. Categorical value-text variant matching (residual 2), fenced to categorical kinds.
4. Perception recall / absence band (residual 3) — likely the largest win and independent of all of
   the above.
5. Option (c), re-scoped as choice STABILIZATION rather than duplicate suppression, and only if 8.5
   shows it is still needed.

All figures above are n=1 fixture item. The fixture's own question (`"about two hours"`) is still
unanswerable by the scalar path because the gold answer is a hedge and `hedged_value` drops it — out
of scope here, but it means commit counts on this fixture are not a correctness proxy.

---

## 9. Section 8.5 RESOLVED, and it was not canonicalization (2026-09-09, later)

**8.5 asked why a run wrote 4 states but created zero new Views, with the pre-existing
`python_tutorial_platform` View merely touched. The answer is strict-rank supersession, not any
canonicalization stage. Treat 8.5's hypothesis as withdrawn.**

`typed_assertion_models.py:100` sets `a.superseded = true` **on create, as the default**, and flips it
to `false` only when the new assertion becomes current:

    cur IS NULL OR will_supersede OR cur.assertion_key = $assertion_key

and `will_supersede` requires `$perceiver_rank > cur.perceiver_rank` — **strictly** greater.
`perceiver_rank` derives from `perceiver_version` (`domain/typed_assertion.py:93`).

So a re-perception at the SAME perceiver_version against an already-consolidated namespace writes
assertions that can never become current. They sit at `superseded=true` with `superseded_by=NULL`
forever, never enter `desired_slots`, and materialize no View. Confirmed in the graph:
`tutorial_platform` x2 `superseded=TRUE / superseded_by=NULL / no View`, while
`python_tutorial_platform` stayed current.

**`superseded=true` carries two distinct meanings behind one flag** — "replaced by a named successor"
(`superseded_by` set) versus "never became current" (`superseded_by` NULL). That is why the log could
not explain itself and why this was initially misread as cross-slot canonicalization. Worth splitting
or at least recording a reason code.

### 9.1 Measurement consequence: four of the runs in section 8 had inert persistence

Every pass after the first ran at `v2` against a namespace already consolidated at `v2`. The
**gate-level** figures in 8.3 remain valid — vote distributions, unanimity, drop reasons are all
pre-persistence. The **committed counts** (`states_written`, View inventory) are not comparable across
those runs. `states_written` counts folded states, not assertions that won their slot.

### 9.2 Re-measured at `perceiver_version=v3`

Same graph, same prompt, reasoning off, k=3; only the perceiver version changed:

    extract: kept=14 dropped=10 (all hedged_value) samples=6/4/4
    GATE 1.000 {status Pluralsight: 3}        <- materialized
    GATE 1.000 {status Pluralsight: 3}        <- materialized
    GATE 0.667 {boolean 2, status Pomodoro 1} <- boolean still wins this span
    GATE 0.333 three-way split on the log-keeping value text
    states_written=3

And the new slot now exists as a current View: `user's tutorial platform (python): Pluralsight`.
**The prompt fix works end-to-end once the version is bumped.** Pluralsight has now been unanimous
`{status: 3}` in three consecutive runs.

### 9.3 The actual slot-proliferation mechanism, now confirmed

> **CORRECTED by section 10 (2026-09-09, later).** The OBSERVATION below holds: two current Views for
> one fact. The EXPLANATION and the prescribed fix are WRONG. Supersession across a taxonomy change
> works; the old slot survives on a head the new pass never re-perceived. **Do not build the slot
> retirement this section asks for** — read section 10 first.

After the v3 pass the namespace holds **both** `tutorial platform (python): Pluralsight` (new, v3) and
`uses pluralsight: true` (old, v2) as CURRENT Views. Bumping `perceiver_version` lets the new reading
materialize but does **not** retire the superseded taxonomy's slot: the old assertion is still current
in its own slot, so it still folds, so its View survives. The retirement loop in
`rebuild_scalar_state` only retires slots absent from the CURRENT fold's `desired_slots` — and the old
slot is present in it.

This is the concrete form of the warning in `archolith-bench/.env.example` ("a namespace consolidated
on-then-off can hold one fact under two slots"). **A taxonomy change therefore needs a namespace
rebuild or an explicit retirement of the old slot, not just a version bump** — and that retirement
step is the real work item, in place of the fold-time suppression recommended in section 5.

### 9.4 Residual failure modes, re-ranked by what the v3 run shows

1. **Categorical value TEXT variance is now the top abstention cause, and it is getting worse as more
   rows survive.** The log-keeping span produced a 1/1/1 three-way split (`keeping a log` /
   `log of challenges, concepts, and skills` /
   `log of completed challenges, learned concepts, and improved skills`). Similarity matching fenced
   to categorical kinds is the right instrument here; it must never touch
   count/duration/money/measurement/clock_time, where "close" defeats supersession.
2. **Perception recall.** `(absent)` votes persist and sample yield is uneven (6/4/4 here, 4/10/8 and
   4/6/7 earlier). Independent of representation.
3. **The Pomodoro span still flips to boolean** (`{boolean 2, status 1}`), so the prompt reduces but
   does not eliminate the original defect. This is the residual that a stabilization mechanism would
   own.

## 10. Section 9.3 CORRECTED — it is span drift orphaning heads, not a missing retirement

Section 9.3 concluded that a taxonomy change needs "an explicit retirement of the old slot" and named
that the real work item. Read-only Cypher against that graph
falsifies it. **Supersession across a taxonomy change already works.** The stale View survives for an
unrelated reason, and building the retirement would mask it.

### 10.1 Where the taxonomy flip DID supersede correctly

`:TypedAssertionHead` keys on `source_key` (episode + span + ordinal, NO extracted semantics)
specifically so a newer perceiver can re-interpret the same claim and supersede it — stated in the
`typed_assertion_models.py` module docstring, and working:

| head `source_key` | episode | span | v2 | v3 |
|---|---|---|---|---|
| `664b019d…` | `56965ca9` | 82–123 | `uses_pluralsight`/boolean, `superseded=TRUE` | `tutorial_platform`/status, **current** |
| `bc8dacec…` | `b422724a` | 23–61 | `tutorial_platform`/status, `superseded=TRUE` | **current** |

The boolean -> status flip superseded cleanly on both. No retirement mechanism is missing.

### 10.2 Why a stale boolean View is nonetheless current

Two OTHER heads carry the old reading and the v3 pass never visited them:

| head `source_key` | episode | span | state |
|---|---|---|---|
| `6fa79e88…` | `56965ca9` | **72–99** | `uses_pluralsight`/boolean v2, `superseded=FALSE`, no v3 assertion |
| `f42322774…` | `b422724a` | **13–40** | `uses_pluralsight`/boolean v2, `superseded=FALSE`, no v3 assertion |

Those spans OVERLAP the ones v3 committed (72–99 vs 82–123; 13–40 vs 23–61) — the same sentence,
grounded to a different extent, therefore a different `source_key`, therefore a **different head**,
therefore permanently unreachable by supersession. v2 did it to itself as well: episode `56965ca9`
committed a boolean at 72–99 AND at 82–123 within one arm.

Head census in that namespace: **10 current heads at v2, 3 at v3.** The v3 pass reached 3 of 13.

View-layer confirmation: `view_key …ss_2a17d65186618d30` ("user's uses pluralsight: true") carries
`ss_view_key_current` with `ss_retired_at` NULL, beside `…ss_ef1aca8d7dc5b917` ("user's tutorial
platform (python): Pluralsight"), same subject `b1c8d735`.

### 10.3 Two further corrections

- **The retirement loop behaved correctly.** The old slot is genuinely live on a head nothing
  superseded, so `desired_slots` rightly contains it. Retiring it would discard a current assertion on
  no evidence. 9.3 read a correct refusal as a missing feature.
- **The two `tutorial platform (python)` Views created 1.5s apart are NOT duplicates.** They share one
  `view_key` and only the later carries `ss_view_key_current` — ordinary versioning. There is no
  View-layer defect here.

### 10.4 Why `align_spans` does not already cover this

`align_spans=True` is live (`typed_scalar_service.py:876`), and `_canonical_aligned_proposal`
intersects the candidates of THAT pass (`max(span_start)` / `min(span_end)`). It removes sample-ORDER
dependence within a pass — which is all its docstring claims — not cross-pass convergence. A later
pass draws different samples, computes a different intersection, commits a different `source_key`.
Every span in 10.1 and 10.2 is a post-alignment committed span.

### 10.5 The corrected work item

> **Claim identity must be stable across passes, or supersession cannot reach a re-perceived claim.**

Not taxonomy-specific: ANY re-run whose spans drift orphans heads and accumulates stale current state
permanently. It is also a prerequisite for adding any new `value_kind` (`date` was the candidate), and
it is the blocker register item 16 already named — *"durable identity unresolved"*, span alignment
*"unreachable from settings until durable claim identity has a supersession story."*

Before attempting it, read register item 19 of the scalar perception abstention plan (not moved here):
the canonical-span-window prototype FAILED — buckets 38 -> 39, short-of-k 23 -> 26, and 1 collision
against a hard zero-collision gate — because the anchor is the value's occurrence in the span and only
58% of spans contain their value as a digit. Any future anchor must be (a) available for spelled-out
values, (b) stable across differing quote extents, (c) unique per CLAIM, not per sentence. Sentence
bucketing was considered and rejected: it converts a fragmentation loss into a conflict loss.

**Not scoped, not started.** Research-grade, with one measured failure behind it; it does not belong
in a session also carrying cost work.
