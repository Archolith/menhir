# Deterministic-First Event Scalar Extraction

Status: **reviewer-approved**

Implementation status (2026-08-05): **Phase 1 + Phase 2A code-complete; bounded Phase 2 smoke
completed and deterministic bypass rejected.** The pure v0.1 extractor, default-off
observe-only live shadow comparison, Bench-owned offline instrument, fail-closed static capture
input, scheduler-parity graph fallback, and non-LME typed-scalar held-out smoke fixture are landed
and focused-tested. The six-call (`2 namespaces × k=3`) smoke had zero truncations, routed 3/7
episodes as fully eligible, and theoretically saved one three-call namespace batch, but only 1/3
fully-covered claims aligned with the LLM baseline and two were router misses. Count/money values
agreed; their free-text attribute identities did not. This fails the zero-router-miss gate and is
smoke evidence only, not a promotion or population gate. Semantic boolean/status/weekday cues
remain fallback-only. The compositional relation + open-target identity supplement in
`menhir-compositional-scalar-identity-2026-08-05.md` has now landed through its first independent
24-case non-LME panel. That corrective supplement rejects expanding the closed canonical-attribute
registry as the primary strategy and remains `not_evaluable` for promotion. Larger preregistered
population gate evidence,
scalar-vs-recall spend attribution, deterministic routing/promotion, savings, and any LME
evaluation remain pending; Phase 2 and the overall plan are not complete.

Goal: admit explicit scalar statements deterministically, without an LLM, while preserving the
existing LLM k=3 / 2-of-3 consensus gate for every ambiguous case. This is a cost/architecture
experiment — "is scalar LLM spend avoidable?" — not a tune toward the 78 LongMemEval items. The
70/78 scalar-write-repair run is the reference, not the target.

Scope boundary: this plan covers **typed-scalar** perception only (count, money, measurement,
duration, frequency, clock_time, weekday, boolean, status, ranges, deltas, expires,
previous/current). Categorical temporal **events** (purchased/acquired/attended, "which lens was
bought most recently") are the separate `.agent/plans/menhir-temporal-event-history-view-2026-07-30.md`
plan. The two paths must not merge.

## Why

- Every scalar perception batch currently costs exactly `k=3` LLM calls at temp 0.7 regardless of
  content. Measured on the LME campaign: namespaces pay for 3 calls each; `lme-6071bd76` paid 3
  calls and produced zero scalar assertions/Views, and its answer was later corrected by recall
  metadata alone (bench `.agent/benchmark-notes/lme-score-campaign.md` TODO). The same TODO asks
  for exactly this experiment: staged gate — deterministic candidate detection, deterministic
  parsing for clear absolutes, two samples first / third on disagreement — measured against the
  always-3-call 2/3 policy.
- The codebase already contains most of the deterministic machinery: unique span grounding
  (`_ground_span`), kind-typed validation (`validate_value`), span-local hedge abstention
  (`is_ambiguous_exact`), span-local temporal disposition (`resolve_temporal_disposition`),
  interval-frequency and colon-duration normalizers, correction classification
  (`classify_absolute_semantics`), and fail-closed unique-name binding. A deterministic extractor
  can reuse these instead of building a parallel pipeline.
- The extraction spend must be separated from recall spend before another full ingest, per the
  campaign TODO. Deterministic-first is the natural instrument for that separation.

## Scope

In scope:

- A deterministic, pure, offline-testable extractor producing the **same `TypedScalarProposal`
  contract** the LLM path produces, running on the same episode content strings.
- Episode-level routing: a fully-eligible episode skips the LLM entirely; any episode with an
  unmatched or ambiguous scalar-like sentence routes to the existing k=3 / threshold gate.
- Shadow mode (deterministic parses, LLM writes, byte-identical behavior), a dual-run ablation
  matrix, class-level promotion gates, and rollback.
- A receipt/metadata provenance contract for deterministic admissions with extractor kind,
  version, parse class/reason, and invariant-check record — **metadata only, never identity keys**.
- A scalar-vs-recall spend attribution report instrument (bench side).

Out of scope (explicit non-goals):

- Any change to `perception.py` (the counter path) or to the fold/authority/recall layers.
- New node types, new View kinds, or a new source of truth. Deterministic claims are
  `TypedAssertion` events in the same log.
- Categorical event history (sibling plan).
- Any change to `IDENTITY_VERSION`, `source_key`, `claim_key`, `assertion_key`, or `slot_key`
  composition. No `extractor_kind` in any identity key.
- LLM-free general NLP: no stemming, synonyms, fuzzy subject matching, tense inference, or
  relative-date arithmetic (the existing resolver's abstain-on-relative rule stays).
- Changing LongMemEval fixtures, gold answers, or the bench harness contracts.
- "Percent" as a new `value_kind`; percentages map onto existing kinds or abstain.

## Proposed Design

### 0. Architecture: extend, don't fork

```text
TurnEvidence rows
  -> _build_episodes (unchanged; same content strings, same coordinate space)
  -> route_episode:
       fully eligible  -> DeterministicScalarExtractor (pure, no LLM)
                          -> invariant checks -> admission receipt
                          -> TypedScalarProposal (same contract)
                          -> TypedScalarDecision(committed, reason="deterministic_admission:<class>")
       any ambiguity   -> existing extract_typed_scalars_once x k -> gate_typed_scalars (unchanged)
  -> bind_and_persist_typed_scalars (unchanged)
  -> scalar_state / scalar_history rebuild (unchanged)
```

One extractor per source claim in the write path (see Routing, D1). Deterministic proposals feed
the identical bind/persist/rebuild pipeline, so they land in the same assertion log and the same
disposable Views — no parallel source of truth.

New module: `src/menhir/services/deterministic_scalar_extractor.py` (pure; imports the existing
pure helpers from `typed_scalar_rules.py` by the codebase's existing private-import convention,
mirroring `typed_scalar_perception.py`). `typed_scalar_rules.py` itself stays untouched except
for any shared helper lifted out — prefer importing over moving.

### 1. Deterministic eligibility / abstention contract

A claim is **eligible** only when the matcher resolves **every** dimension below; any dimension
that fails closes the claim (and, at episode level, routes the episode to the LLM). Every
decision is span-local: the matcher operates on one grounded span region of the episode content,
and the quote it constructs must occur exactly once in the content (`_ground_span`, defense-in-depth).

| Dimension | Eligible | Abstain -> LLM |
|---|---|---|
| Subject | v0.1 authority is limited to (a) canonical self: first-person via `SELF_TOKENS` -> 'user' (canonical-self seam at bind), and (b) an **already uniquely bound explicit entity** whose name matches exactly AND whose property phrase is separately grounded in the span. An owned object is **never inferred** merely because its head noun equals a slot anchor ("my car" is not a subject unless a bound entity named "car" exists and the span states its property) | anything else: unbound/named third parties, possession inference from head-noun equality, ambiguous identity |
| Slot (attribute/scope/unit) | attribute from a **closed, versioned surface-template -> canonical slot mapping** (template registry shipped with the extractor version; every entry is a literal surface pattern mapping to exactly one snake_case slot). scope only from an explicit adjacent qualifier in the template, else "". unit from the number's unit token (cm, kg, usd, minutes, percent...) canonicalized by `_canon_modifier`. Unknown surface, new phrasing, synonyms outside the registry, or two candidate slots -> abstain. No broad ontology table; arbitrary NP semantics are explicitly not solved | any phrase not in the registry, synonym paraphrase, ambiguous mapping |
| Value kind | integer/decimal -> count/money/measurement by unit token; currency tokens ($, USD, dollars) -> money/usd; colon time with a **standing-property / recurring-state cue** (wake time, day off, schedule, "I wake at 7:30") -> clock_time; elapsed cue (took/finished/personal best/race) + M:SS/H:MM:SS or unit word -> duration (colon -> seconds via `_normalize_colon_duration_value`); "N times a/per period", "every other week", "every N weeks" (reuse `_FREQUENCY_INTERVAL_RE`) -> frequency with rate normalization; "N%" with a measurement subject -> measurement/percent; boolean/status/weekday only via exact copular phrases in the registry and remain LLM-fallback-only for v0.1 (see Routing) | ambiguous colon (no standing-property/elapsed cue), bare number with no unit, mixed 12/24h, one-off event times ("met at 3pm yesterday") |
| Operation | absolute: present copular/possessive ("I have N", "my X is N", "I wake at T", "X is now N", "so far"/"to date" cumulative totals); delta: additive verb + amount ("added/bought/earned/gained N") signed +, subtractive verb ("sold/gave away/lost N") signed -, **both requiring explicit held-slot/accumulator context in the span** (the quantity remains held by the subject); expire: "used to X", "no longer X", "don't ... anymore" carrying the old value | "spent/payed N" without a balance/account slot anchor, one-off purchases/payments/events ("I paid $250" — the bench negative control — must never become a View), change verbs without held-quantity context |
| Source/world time | reuse `resolve_temporal_disposition` + `_parse_source_date` exactly: explicit calendar date in span -> that date; else episode reference time; past-only/bounded/relative-future/unresolvable-date -> drop as today | — (deterministic uses the identical resolver, so dispositions match the LLM path by construction) |
| Negation | expire forms only ("no longer", "don't ... anymore", "not ... anymore") | bare "didn't" / "did not" without a current-state reading |
| Modality | none | "maybe/possibly/probably/plan to/hoping to/might", future intentions without a current reading, hypotheticals ("if I had N") |
| Uncertainty | genuine closed interval (between/from-to/N-N with both endpoints parsing, lo <= hi) -> [lo, hi] range | approximation/vague ("about/around/roughly/a few/several/or so", "30 or 40" discrete alternatives) — reuse `is_ambiguous_exact` |
| Comparisons | none | "more than/less than/at least/at most/over/under", superlatives ("the most/the best"), ">N"/"<N" |
| Lists | none for v1: a sentence enumerating multiple scalars ("3 cats, 2 dogs") has no single deterministic reading per element without an ontology | any list/coordination |
| Quote offsets | the **shortest COMPLETE grounded fragment** containing every cue the parse consumed for subject/slot/operation/value/time — cue-bearing pronouns and verbs are NOT stripped ("I have 37 coins", "I wake at 7:30", "sold 2 coins"); located against the **same content string the LLM path sees** (including the `[YYYY-MM-DD] ` prefix), so offsets share one coordinate space with `source_key` | zero or multiple occurrences (`_ground_span` fails) |

Previous/current statements ("I used to wake at 9, now I wake at 7:30") are **two** claims —
expire(old, span A) + absolute(new, span B) — each with its own span, offsets, and source_key,
mirroring the LLM prompt's rule. A bare "my previous balance was X" with no current statement is
`past_only` and drops via the shared resolver.

Class taxonomy (each carries its own grammar + promotion gate, Section 5):
`c_count`, `c_money`, `c_measurement`, `c_percent`, `c_clock_time`, `c_duration`,
`c_frequency` (explicit numeric) with optional subclass `c_frequency_word` (daily/weekly/
monthly — decided by shadow data), `c_range`, `c_delta_add`, `c_delta_sub`, `c_expire`,
`c_prev_current` (composed), `c_correction` (existing `classify_absolute_semantics` over an
eligible absolute span). Boolean/status/weekday have **no v0.1 authority class**: they remain
LLM-fallback-only until shadow demonstrates a defensible registry of exact copular templates
(Section 3).

### 2. Provenance and the admission receipt (same contract as LLM, plus determinism)

Deterministic admissions must be reconstructible and must not silently outrank LLM evidence.

- **Identity**: proposals carry the same `source_key` (episode_uuid + span offsets + ordinal, via
  `build_source_key`) and the same `slot_key`/`assertion_key` semantics. No key change.
- **Perceiver version**: deterministic claims write with the **same `perceiver_version` the LLM
  path uses** (today "v1"). Consequence, inherited from the store, not invented here: an
  identical interpretation collides into the *same* assertion node (no fork), and a *different*
  interpretation at the same span is stored non-current rather than superseding
  (`typed_assertion.py`: "a different value at the SAME version does NOT supersede"). That is
  precisely the no-silent-supersede guarantee we want for conflicts (D2). Deterministic
  supersession rights are a later, explicitly gated decision.
- **Receipt**: `TypedAssertion.metadata` (the field exists on the dataclass) carries
  `extractor_kind="deterministic"`, `extractor_version="det-v0.1"`, `parse_class`,
  `parse_reason` (which grammar + which invariant checks passed), `fully_grounded=true`, and
  `conflict_status` (none | same_value_existing | quarantined). The consolidation-audit event
  mirrors the same fields and keeps the existing quote-free convention (source_key + offsets +
  fields, joinable to the episode). Implementation MUST verify `metadata` survives the Neo4j
  write/read round-trip; if it is dropped by the repository, that is a persistence bug to fix in
  the existing write path — the field is never conditionally invented or added to a key.
- **Reconstructibility**: the extractor is pure — `(episode content, episode reference time) ->
  proposals`, identical on every replay. A shadow ledger (bench-side, keyed by source_key +
  extractor_version) can re-derive any admission offline.
- **Belief conservation / replayability invariants** (unchanged from the LLM path): every
  admitted claim has a located exact span, a validated kind-typed value, and a founded
  temporal disposition; the extractor never does arithmetic (atomic observations only; deltas
  are signed as stated; range endpoints preserved); folds are untouched.

### 3. Routing, and when deterministic replaces the 3 LLM calls

Decision (staged, explicit):

- **Class eligibility is a necessary condition; episode completeness is the sufficient one.**
  An episode is fully eligible when every scalar-like sentence in it matches a promoted-class
  grammar with all dimensions resolved. The scalar-like detector (number / currency / colon-time
  / percent / unit token / change verb / "used to") **cannot prove completeness** — especially
  for boolean/status/weekday or semantic scalars with no number — so **every unsupported cue
  class forces the LLM**: a sentence bearing any scalar cue that the router cannot fully match
  routes the **whole episode** to the LLM path. Boolean/status/weekday have no v0.1 authority
  class and therefore always force the LLM. Determinism never silently drops a claim the LLM
  might catch; a missed scalar-looking sentence is a routing failure to the LLM, not an
  abstention.
- **The router itself has its own promotion gate** (Section 7): before any class can be
  authoritative, shadow must demonstrate **zero LLM-baseline claims missed in episodes the
  router would bypass** (every claim the k=3 gate would have committed is produced by the
  deterministic parse on the same content). If completeness cannot be proven for an episode,
  the episode keeps the LLM path.
- **A single deterministic parse replaces all 3 LLM calls for a fully eligible episode.** No
  synthetic k=3 vote is manufactured: determinism is an **admission**, not a vote (D3). The
  residual deterministic checks are not a consensus gate; they are invariant checks —
  (i) unique grammar match, (ii) exact-once span location, (iii) kind/unit/operation coherence
  (e.g., delta requires an additive/subtractive verb; money requires a currency token; colon
  duration requires an elapsed cue), (iv) temporal disposition resolved, (v) **no silent
  supersede**: if an existing current assertion already commits this source_key with a different
  value, the deterministic claim abstains and the conflict is receipted. These produce a
  `TypedScalarDecision(committed=True, reason="deterministic_admission:<class>", ...)` for the
  persistence pipeline; the audit trail carries a distinct label so deterministic admissions are
  never confused with gate votes.
- Episodes that are not fully eligible keep the **exact current behavior**: k LLM samples,
  `gate_typed_scalars` with the configured threshold (2/3 in the candidate LME config, 1.0
  default), align_spans, and the configured reconciliations.

### 4. Conflict policy: deterministic vs LLM

- **Shadow (no writes)**: both paths parse; LLM decisions persist (byte-identical to today);
  deterministic decisions go to the shadow ledger. Agreement/disagreement is measured per claim
  and per class (Section 5).
- **Dual-run validation (promotion)**: sampled episodes are parsed by both paths on frozen
  captures; a disagreement on the same source_key (same span, different interpretation) is a
  **quarantine**: that claim is excluded from deterministic writes (LLM continues to own it) and
  the class's suspicion counter increments. N quarantines in a window -> the class auto-demotes
  to shadow and the incident is receipted for review (D4).
- **Authoritative (promoted class)**: per-claim conflict check (v above) still runs — a
  deterministic claim never silently supersedes an existing current LLM interpretation of the
  same span. Different spans on the same slot are *not* conflicts: both are source-time
  observations and the fold orders them by valid_at, exactly as it does today.
- Same-source_key, same-interpretation deterministic and LLM claims collide into one assertion
  node by identity; the receipt still records which extractor produced it.

### 5. Shadow mode and the ablation matrix

Shadow has two halves: (a) live in-process shadow audit behind a flag (deterministic proposals
computed, never written; agreement stats appended to the existing `consolidation_audit` "extract"
events, which the dirty worktree already enriches with per-sample proposal summaries), and
(b) **offline** dual-run over frozen episode captures (bench-side, like
`scripts/probe_scalar_extraction.py` but deterministic-only plus LLM replay), which is the cheap,
reproducible engine for rule iteration.

Ablation matrix (all measured separately; never collapsed):

| Metric | Definition |
|---|---|
| Deterministic coverage | fully-eligible episodes / episodes; eligible claims / LLM-baseline claims |
| Exact agreement | deterministic admission == LLM decision on the **same `source_key`** (same span + same interpretation + same committed value) |
| Aligned semantic agreement | deterministic vs LLM decisions matched through the existing common-span/interpretation machinery (`_claim_groups` with `align_spans` + `_interpretation_label`): quotes whose boundaries differ are compared on the aligned common span and interpretation, so agreement does not assume quote boundaries match. Both metrics are reported; a claim counts toward exact agreement only when both hold, toward aligned agreement when the aligned reading matches |
| Safe abstention | episodes routed to LLM with no loss vs LLM-only baseline |
| False-positive rate | deterministic-only admissions judged wrong on labeled held-out negatives |
| False-current rate | deterministic admissions that are past-only/bounded/modal (adversarial negatives) |
| Four-stage realization | bench `scalar_state_coverage.py` stages on a fresh isolated stack with deterministic-first on: assertion_emitted / subject_bound / view_materialized / fold_correct, plus a separate extractor stage (deterministic parse succeeded) — extractor, binding, projection, and recall failures are four different numbers |
| Recall impact | frozen LME evaluation vs the 70/78 reference (rules locked first; Section 6) |
| Latency | deterministic parse time vs k=3 LLM calls per batch |
| Token/dollar savings | LLM calls + tokens per namespace; **scalar extraction spend separated from recall spend** (telemetry "extract" events + batch counters vs recall-side calls); cost per corrected answer per the campaign TODO |

### 6. Anti-benchmark-tuning discipline

- Property-based / parser tests: parse -> quote -> re-parse idempotence; offsets always inside
  content; deterministic output for a given content is byte-stable across calls; never crashes
  on adversarial input (fuzz the grammar).
- Generated linguistic perturbations: parametrized templates sweeping number formats
  (`$250`, `250 USD`, `$250.00`, `250 dollars`, `1,000`, decimals), clock forms (`7:30`,
  `07:30`, `7:30 PM`), negation, modality, hedges, tense, and word order — run before any LME run.
- Held-out non-LME conversations: the bench `menhir_scalar_state` fixtures and hand-written
  conversations; the LME corpus is **not** used to tune rules.
- Adversarial negatives: one-off events ("I paid $250"), possessed objects ("my car is red"),
  questions ("did you use to smoke?"), hypotheticals, jokes, nested clauses.
- Frozen LME evaluation only after the rule set is locked; any rule change invalidates the
  number and requires a fresh frozen run. No task IDs, namespace-name keys (`lme-*`), fixture
  phrases, or gold-value heuristics anywhere in the grammar.

### 7. Rollout gates and rollback

Class-level promotion, never an aggregate score. For each class: shadow -> class-authoritative
(flag) -> default (flag on in fresh deploys, removable for rollback).

**All gates are pre-registered before the frozen LME evaluation** — written into the bench run
manifest before the run starts, never chosen after seeing LME results (no post-hoc A/B/N
selection). The router gate (below) and every class gate must pass on held-out material before
the frozen run; the frozen LME run then only confirms them.

Router promotion gate (prerequisite for any class):
1. 100% structural invariant pass on the full held-out set (every admission grounded, kind-typed,
   temporally resolved, receipt complete — mechanical checks);
2. zero router-missed LLM-baseline claims in shadow dual-run (Section 3);
3. zero known false positives / false-current admissions on the adversarial held-out set.

Per-class promotion gate:
1. zero false-positive and false-current admissions on the adversarial held-out set;
2. zero quarantines in the trailing window (Section 4);
3. a **statistically defensible precision floor with confidence interval and a minimum
   sample size, both locked before the frozen LME run** (e.g., lower CI bound on
   exact+aligned agreement >= floor, n >= minimum);
4. frozen-LME recall non-negative for that class's stratified task subset (confirmed after the
   run, not a selection criterion);
5. latency/cost improvement confirmed (extraction spend, kept separate from recall spend).

Feature flags (all default off => byte-identical behavior):
`MENHIR_SCALAR_DETERMINISTIC_FIRST_ENABLED` (master),
`MENHIR_SCALAR_DETERMINISTIC_SHADOW` (parse, never write),
`MENHIR_SCALAR_DETERMINISTIC_CLASSES` (comma list of promoted classes; default empty).

Rollback is per class: flip the flag; auto-demotion on suspicion counter (Section 4). The LLM
path is always reachable: routing falls back per episode, and demoting a class returns its
episodes to the k=3 gate. Namespace reset/idempotency discipline is unchanged.

### 8. Migration / backward compatibility

- Flags off -> `perceive_and_persist` takes its current path untouched; the new module is
  imported but never invoked. `k`, `threshold`, reconciliations, and the gate are unchanged.
- Deterministic writes go through the same repository path; the store schema is untouched.
  `TypedAssertion.metadata` exists on the dataclass; implementation verifies it survives the
  Neo4j write/read round-trip (if the repository drops it, that persistence bug is fixed in the
  existing write path — the field is never conditionally invented).
- Bench contracts unchanged; new instruments follow the naming convention and register in
  `.agent/scripts-index.md`.
- Re-perception across extractor flips is governed by the existing namespace-reset/idempotency
  discipline (documented in the gate docstring); span/source identity is provenance, and no
  retire/merge pass exists in v1. Ablation experiments always use fresh isolated namespaces.

## Alternatives Considered

- **A: deterministic-first with per-claim merging (hybrid).** Deterministic claims persist
  directly and only ambiguous claims go to the LLM. Rejected for v1: it creates two extractors
  for one episode, dual candidates on the same facts, and a prompt/grounding mismatch — the
  LLM would re-emit claims the deterministic path already wrote. Episode-level routing keeps
  one extractor per claim and a clean write path. Per-claim merging can be revisited after
  promotion data exists.
- **B: deterministic parse as a synthetic k-th sample.** Rejected on review: determinism is not
  a vote; manufacturing it as a unanimous k=3 sample would let a silent rule outrank the
  consensus gate on the same source. The admission receipt and invariant checks are the gate.
- **C: keep always-LLM and only reduce k (two samples first, third on disagreement).** Cheaper
  to ship, but keeps ~66% of the spend on fully eligible episodes and leaves the deterministic
  question unanswered; the campaign TODO explicitly wants the deterministic-candidate stage.
  The two-sample policy remains a viable fallback arm in the ablation (measure it, do not
  pre-commit).
- **D: reuse the counter perception path's extractor.** Rejected: different proposal contract,
  different gate, and the typed-scalar path is the one with the measured spend problem.

Chosen: A's routing with B's rejection, i.e., episode-level deterministic-first with explicit
admission receipts, invariant checks, and LLM fallback.

## Risks

- **Deterministic mis-parses that look grounded**: mitigated by exact-once span grounding,
  span-local hedge/temporal abstention, invariant checks, shadow + dual-run + adversarial
  negatives, and class-level promotion; determinism never silently supersedes an LLM reading.
- **Quote-boundary forking across extractors**: mitigated by one-extractor-per-claim routing and
  by the aligned semantic agreement metric (Section 5) measuring boundary differences before any
  promotion; residual risk on re-perception is bounded by the existing namespace-reset/
  idempotency discipline (documented in the gate docstring today). No retire/merge pass in v1.
- **Over-routing**: a too-conservative eligibility contract sends everything to the LLM and the
  experiment measures nothing. Mitigated by the coverage metric and the `c_frequency_word`
  decision point; the contract is tightened from data, never from LME items.
- **Benchmark tuning**: the Section 6 discipline (frozen rules, held-out sets, perturbations) is
  the explicit guard; the task's framing ("learn whether spend is avoidable, not tune to the 78")
  is recorded here as the evaluation policy.
- **Spend attribution confusion**: scalar extraction, recall, and answer-generation calls are
  separated in the attribution report; no aggregate "LLM cost" number is used for decisions.
- **Metadata not persisted**: `TypedAssertion.metadata` exists on the dataclass; implementation
  must verify it survives the Neo4j round-trip, and if the repository drops it, fix that
  persistence bug in the existing write path (still metadata, never a key).
- **Dirty worktrees**: both repos carry uncommitted scalar work; the plan touches
  `typed_scalar_service.py`/`scalar_consolidation.py`/`scheduler_tasks.py` wiring only through
  additive flags and an injected extractor, and never rewrites the dirty rules/persistence code.
  Both worktrees must be preserved exactly; implementation starts from a clean rebase decision
  with the owner.

## Invariants

- Belief is conserved from admitted evidence; deterministic claims terminate in founded
  `TurnEvidence` exactly like LLM claims.
- Identity keys, `IDENTITY_VERSION`, `source_key`/`assertion_key` composition, and the
  no-silent-supersede store rule are untouched.
- Flag-off behavior is byte-identical to today.
- The LLM k=3 / threshold gate remains the fallback for every ambiguous episode, forever.
- One extractor per source claim in the write path.
- Namespace isolation exact; source/world time controls ordering; Views disposable and
  deterministically reconstructible.
- Scalar extraction spend is always reported separately from recall spend.

## Validation

- Unit: grammar classes, all abstention dimensions, normalizer reuse, quote minimality,
  offsets-in-content, admission receipt fields, invariant checks, conflict/quarantine logic.
- Property-based: idempotence, stability, fuzz safety.
- Bench: perturbation suite + held-out conversations + adversarial negatives -> coverage /
  agreement / FP / false-current; four-stage coverage on a fresh isolated stack
  (`run_scalar_state_e2e.sh` conventions) with deterministic-first on; frozen LME evaluation
  (rules locked) vs the 70/78 reference; latency + token/dollar attribution report.
- Manual: probe flow (`probe_scalar_extraction.py` gains a `--deterministic` arm) on a handful
  of namespaces; explorer shows the receipt metadata.
- Rollout: per-class promotion gates, dual-run quarantine, auto-demotion drill.

## Implementation Sequence

1. **Rules freeze v0.1 + extractor**: this plan's eligibility contract and class taxonomy;
   implement `deterministic_scalar_extractor.py` + unit/property tests. No runtime wiring.
2. **Shadow instruments**: offline dual-run script (bench) + live shadow audit flag; iterate
   rules on perturbations/held-out/adversarial material only. The first bounded evidence bundle is
   the pre-registered non-LME typed-scalar smoke fixture: its six-call static freeze and offline
   report are complete. It rejected bypass readiness (1/3 aligned fully-covered claims; two router
   misses) and identified the global free-text attribute identity contract as the next design
   question. Do not tune grammar or comparator behavior to those three rows. Build a larger generic
   held-out panel before another metered run. Produce the spend-attribution report over existing
   telemetry later (scalar vs recall separated).
3. **Pre-register gates, then frozen LME evaluation**: write the router + per-class gates
   (Section 7) into the run manifest before the run; confirm them on held-out material; then one
   frozen LME evaluation with locked rules (recall impact vs 70/78) — after which no gate value
   is changed post-hoc.
4. **Class-level promotion**: per-class flags, dual-run quarantine, suspicion-based
   auto-demotion. No retire/merge pass exists in v1.
5. **Measured default + docs**: flip per-class defaults after gates, CHANGELOGs,
   `.agent/architecture.md`, `.agent/data_models.md` (receipt metadata), `.agent/endpoints.md`
   (if any audit surface), `.agent/memory-governance.md` (admission receipt for deterministic
   extractor), bench docs, `.agent/scripts-index.md`.

## Docs To Update

- `.agent/architecture.md` (deterministic extractor module, routing)
- `.agent/data_models.md` (admission receipt metadata fields)
- `.agent/memory-governance.md` (deterministic admission as a governed receipt, not a vote)
- `.agent/CHANGELOG.md`, `archolith-bench/.agent/CHANGELOG.md`
- `.agent/scripts-index.md` (new instruments)
- `archolith-bench/.agent/benchmark-notes/lme-score-campaign.md` (TODO resolution)
- `.agent/workflows/scalar_state_measurement.md` (deterministic arm)

## Decision Log

- **D1** Episode-level routing, not per-claim merging, for v1: one extractor per source claim in
  the write path; revisit per-claim merging only after promotion data.
- **D2** Deterministic claims write at the LLM `perceiver_version` so identical interpretations
  collide into one node and different interpretations at the same span never silently supersede
  (store-enforced). Extractor kind/version/class live in `metadata` + audit receipts, never in
  identity keys. Open question: whether a promoted class ever earns deterministic supersession
  rights (deferred, explicitly gated).
- **D3** Determinism is an admission with invariant checks, not a vote; no synthetic k=3
  consensus; distinct audit label; `reason="deterministic_admission:<class>"`.
- **D4** Shadow -> class-authoritative -> default; per-class promotion gates with suspicion-based
  auto-demotion; LLM fallback forever.
- **D5** Percentages map to `measurement`/unit `percent` only with a measurement subject;
  otherwise abstain. No new value_kind.
- **D6** Categorical event history is the sibling plan; typed scalars and events never share a
  path.
- **D7** Promotion gates are **pre-registered before the frozen LME run** (structural invariant
  100%, zero FP/false-current on adversarial held-out, zero router-missed LLM-baseline claims,
  statistically defensible per-class precision floor/CI with minimum sample size locked in
  advance). No gate value is chosen after seeing LME results.
- **D8** No claim-merge reconciliation in v1: span/source identity is provenance, and matching
  (episode, slot, value, valid_at) across spans could collapse two distinct claims. Re-perception
  across extractor flips uses the existing namespace-reset discipline; ablation experiments use
  fresh isolated namespaces only.

## Non-Goals (restated)

- No new node types / View kinds / sources of truth; no identity changes; no counter-path or
  recall-path changes; no categorical events; no ontology expansion beyond the class taxonomy;
  no benchmark fixture or gold changes; no arithmetic in the extractor; no LLM-free claims for
  ambiguous anything.
