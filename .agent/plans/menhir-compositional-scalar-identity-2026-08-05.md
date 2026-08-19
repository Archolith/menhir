# Compositional Scalar Identity

Status: **Phases 1-4 implemented; larger population evidence is next**
**Last verified:** 2026-08-18 — CONSISTENT with Phases 1-4 implemented. `diagnostic_vs_llm` 2 hits, `TypedScalarProposal` 90.


This is a corrective supplement to
`menhir-deterministic-first-event-scalar-2026-07-30.md`. It replaces that plan's next-step idea of
solving free-text attribute disagreement with a larger canonical-attribute/template registry.
The landed extractor and shadow instruments remain useful; deterministic routing is still rejected
and the LLM gate remains authoritative.

## Why

- The first held-out smoke extracted the values correctly but disagreed on labels such as
  `coins` versus `count` and `savings_balance` versus `account_balance`. Exact agreement with an
  unstable free-text LLM attribute is not a semantic correctness test.
- Expanding a surface-template-to-attribute table is expensive, brittle, and likely to tune the
  system to the examples it has seen.
- Most explicit scalar claims are compositional: who the fact is about, what relation is asserted,
  what open-world target or scope it concerns, its typed value/unit/operation, and when it holds.
  Those parts can be represented and measured without inventing a large ontology.

## Scope

In this work:

- Add a pure, non-persisted compositional identity sidecar for typed-scalar proposals.
- Keep a small closed relation vocabulary while keeping target/scope open-world and grounded in
  the source phrase or an already resolved entity.
- Add shadow-only comparison fields without changing existing raw exact/aligned metrics.
- Define a source-hash-bound, human-labeled semantic evaluation contract in Bench.
- Build generic perturbation and adversarial panels before any metered or LongMemEval run.

Not in this work:

- No `TypedAssertion`, Neo4j schema, identity-version, slot-key, fold, View, recall, or persistence
  migration.
- No deterministic write authority, routing bypass, class promotion, or LLM-call reduction yet.
- No task IDs, LME phrases, fixture-specific aliases, broad synonym table, stemming ontology, or
  post-hoc benchmark tuning.
- No categorical event-history work; events remain the sibling temporal-view project.

## Proposed Design

### 1. Sidecar identity

Add a pure value object with two deliberately separate notions of identity:

```text
CompositionalScalarIdentity
  subject                 canonical self or normalized bound subject
  relation_type           small closed vocabulary
  target_or_scope         open grounded phrase or resolved entity reference
  value_kind              existing typed-scalar kind
  value                   existing normalize_scalar output
  unit                    existing canonical unit
  operation               absolute | delta | expire
  effective_time          proposal.when, or null; never wall-clock invented
  provenance              episode, offsets, ordinal, source_key, derivation receipt

semantic_key = all semantic fields, excluding provenance
claim_key    = semantic_key + source_key
```

The initial relation vocabulary is intentionally small:

- `quantity`
- `balance`
- `measurement`
- `schedule_time`
- `duration`
- `frequency`
- `state`

`state` is the safe fallback for a well-typed proposal that has not yet earned a narrower
structural relation. An unrecognized or ambiguous decomposition abstains from *canonical
comparison*; it never changes extraction or persistence.

### 2. Target/scope derivation without an alias registry

The target is not a canonical attribute name. It is an open phrase grounded by a small structural
grammar, for example:

```text
I have 37 coins                       quantity(target=coins)
my savings balance is $500            balance(target=savings)
I sleep 8 hours per night             duration(target=sleep)
I wake at 07:30                       schedule_time(target=wake)
I run 3 times per week                frequency(target=run)
```

The parser may normalize case, whitespace, possessive framing, numeric formatting, and globally
approved units. It must not translate arbitrary synonyms or map a benchmark phrase to a preferred
attribute. Where a target resolves to an existing entity, the receipt may carry that durable
identity separately from the raw grounded phrase. Ambiguous structure returns no canonical
identity and keeps the raw shadow metrics only.

Both deterministic and LLM proposals pass through the same pure composer using their grounded
span and typed fields. The deterministic result is not copied onto the LLM result, and the LLM is
not treated as gold.

### 3. Shadow comparison

Keep the current metrics unchanged:

- raw exact one-to-one agreement: same source key and full raw interpretation;
- raw aligned one-to-one agreement: verified overlapping source span and full raw interpretation.

Add separate metrics:

- compositional exact: same source key and semantic key;
- compositional aligned: verified span alignment and semantic key;
- compositional unresolved: either side could not be safely composed;
- identity disagreement: both composed, but relation/target differs;
- diagnostic LLM router miss: an eligible deterministic claim has no aligned LLM counterpart.

Telemetry remains bounded and quote-free. Open target/subject text is not copied into ordinary
audit logs; emit stable hashes plus low-cardinality relation/status fields. Authorized offline
Bench reports may join offsets back to their capture.

This restriction applies to the new compositional section. The legacy raw shadow summaries keep
their existing schema-compatible attribute/scope/value and locator fields so historical raw
metrics remain comparable; schema v2 documents those as a separate legacy/raw lane. Stable hashes
are pseudonymous join identifiers rather than secrecy against dictionary attacks.

### 4. Independent labeled measurement

Bench gets a versioned capture-independent semantic panel bound to a canonical episode-source hash,
exact span hashes, and the panel file hash. A positive label records the expected compositional
tuple; a negative label records the expected abstention/risk. Labels are authored from the source
claim, never copied from an LLM answer.

Report these separately:

- labeled semantic coverage and precision;
- correct abstention;
- false positive and false current;
- wrong relation, wrong target, wrong value/unit/operation/time;
- unjoinable/unresolved claims;
- raw and compositional agreement versus LLM under a `diagnostic_vs_llm` section.

The first generic panel must have at least 12 positives and 12 negatives, at least four relation
classes, and at least three perturbation forms per semantic group. Hold out entire groups rather
than individual phrasings. The bounded v1 panel covers number/unit/currency/clock formats plus
questions, hypotheticals, modals, hedges, past-only statements, and lists; duplicate-span rejection
is exercised at the schema-contract layer. The next population expansion must add date-prefix and
word-order drift, one-off transactions, and mixed/unknown units without altering existing scored
cases in response to their results.

### 5. Promotion gates

The first implementation is contract-only and always reports `promotion_status=not_evaluable`.
Before any later class becomes authoritative, pre-register and satisfy:

1. 100% grounding, typing, replay, and one-to-one-join invariants;
2. zero labeled false-positive and false-current admissions;
3. zero labeled positive router misses for the promoted class;
4. per-class canonical precision lower 95% Wilson bound at or above 0.99 with at least 100 labeled
   admissions for that class;
5. non-negative frozen recall after rules and gates are locked;
6. measured extraction latency/token savings, separate from recall spend.

LLM agreement is diagnostic evidence only and cannot satisfy or fail a promotion gate by itself.

## Implementation Sequence

1. **Pure foundation (this bounded chunk)**
   - Add provenance and compositional identity value objects plus stable semantic/claim keys.
   - Build only from existing `TypedScalarProposal` fields with caller-supplied relation/target or
     the safe `state` + `(attribute, scope)` fallback; no structural inference yet.
   - Unit-test arbitrary open targets, value/time/unit normalization, source provenance,
     canonical-self opt-in, determinism, and absence of wall-clock defaults.
2. **Structural composition**
   - Add small relation parsers for quantity, balance, measurement, schedule, duration, and
     frequency.
   - Each parser returns a derivation receipt or an explicit abstention; no alias table.
   - Run generic/property perturbation tests only.
3. **Shadow integration**
   - Add compositional metrics alongside raw metrics in Menhir and the offline Bench report.
   - Preserve flag-off behavior, LLM call counts, persistence decisions, and quote-free telemetry.
4. **Independent panel**
   - Land the source-bound label schema and generic positive/negative panel.
   - Pre-register population gates before any metered capture.
5. **Measure, then decide**
   - Run a bounded non-LME capture, then a larger held-out panel.
   - Only after the gates pass consider class-level deterministic routing. Otherwise retain the
   sidecar for diagnosis and keep the LLM path.

## Implementation Status (2026-08-05)

- Phase 1 landed the pure compositional identity and provenance contract without runtime wiring.
- Phase 2 landed a deliberately narrow, fail-closed structural composer for singular self claims.
  It returns explicit abstention receipts for unsupported, ambiguous, incomplete, collective,
  historical, hypothetical, hedged, or event-like shapes.
- Measurement and duration coverage is intentionally conservative (`I am ... cm tall`, `I weigh
  ... kg`, and recurring nightly sleep duration) until independent labeled evidence supports
  broader grammar. This is a precision choice, not a phrase-registry roadmap.
- No persistence, routing, LLM authority, schema, telemetry, Docker, Neo4j, or benchmark behavior
  changed in Phases 1-2.
- Phase 3 added schema-v2, diagnostic-only compositional comparison beside the unchanged raw shadow
  metrics. The nested payload uses stable hashes and closed status/relation/reason fields; it never
  emits open target/subject text. LLM comparisons remain explicitly diagnostic, and promotion
  remains `not_evaluable` in that lane.
- Phase 4 landed Bench's capture-independent semantic panel in commits `95b2bf3` and `b2de4ee`.
  The v1 fixture has 12 positive and 12 negative holdout cases, four relation groups, exact source
  and span hashes, whole-group isolation, strict mismatch taxonomy, and status-bearing uncertainty.
  It uses the real deterministic extractor and structural composer without an LLM, service, graph,
  or benchmark task ID.
- At Menhir `36fddd1`, the first panel result was 12/12 positive exact joins, 11/12 correct semantic
  identities, zero wrong identities, one honest `struct.relation_unknown` for the generic
  possessive-weight form, 12/12 negative system non-admissions, and zero false admissions/current.
  The 11 admitted positives yield only about a 74.1% aggregate Wilson lower bound, so promotion is
  correctly `not_evaluable` and no deterministic routing authority follows from this result.
- The independent v1 panel supplied the evidence required by the conservative measurement hold.
  Menhir `dd60bdda61e221d501e5791a7caba2cba3a176df` therefore added a post-v1, fail-closed grammar
  expansion for literal self possessives: weight paired only with kg-family units and height paired
  only with cm-family units. Structural derivation provenance advanced to `structural-v2`; the
  fixture, labels, extractor, promotion gate, persistence, routing, and LLM authority did not
  change.
- Replaying the unchanged panel at `structural-v2` produced 12/12 exact joins, 12/12 correct
  semantic identities, zero wrong/unresolved positives, 12/12 negative system non-admissions, and
  zero false admissions/current. This closes the observed generic grammar gap but remains a
  24-case regression panel with `promotion_status=not_evaluable`; it is not population evidence
  and authorizes no deterministic routing.

## Alternatives Considered

- **Expand the closed template/attribute registry.** Rejected as the primary strategy: it turns
  development into phrase enumeration and encourages benchmark tuning.
- **Drop attribute/scope from comparison.** Rejected: it can call unrelated facts equivalent.
- **Treat LLM majority labels as canonical truth.** Rejected: the smoke already showed label
  instability despite value agreement.
- **Persist the compositional shape immediately.** Rejected: there is not yet evidence that its
  relation/target decomposition is stable enough to migrate durable identity.

## Risks

- Over-general structural patterns may connect unrelated values. Fail closed and test one-off
  transactions, lists, nested clauses, and ambiguous scopes.
- Open targets can leak source text into telemetry. Hash/redact them in live audit payloads.
- A relation vocabulary can quietly grow into another ontology. Add a relation only when it
  represents different fold behavior, not a new noun.
- Missing proposal time is not missing event time: keep it null in the sidecar and leave existing
  valid-time resolution untouched.
- Canonical comparison can hide raw extraction drift. Always retain and display raw metrics.

## Invariants

- The LLM gate remains authoritative and reachable for every claim.
- No stored identity key or schema changes in this plan.
- Source offsets and `source_key` remain the provenance anchor.
- One extractor per persisted claim; shadow work never writes.
- Scalar and categorical event paths remain separate.
- Rules and gates are frozen before LME evaluation.

## Validation

- Focused pure unit tests for sidecar identity and structural parsers.
- Existing deterministic extractor and shadow suites remain green.
- Bench schema/comparator tests cover hash binding, one-to-one matching, null denominators,
  exact-join versus system non-admission, and LLM-diagnostic separation.
- No Docker, Neo4j, network, LLM, or LongMemEval activity in the first four chunks.
- A bounded Luna review followed by independent root diff review and focused pytest before every
  commit.

## Docs To Update

- Before promotion work, expand the independent generic holdout without editing existing scored
  cases, pre-register a population gate, and document the decision separately.
- Keep Bench `.agent/README.md`, the combined Menhir `.agent/scripts-index.md`, the Bench data
  model, architecture, changelog, and benchmark note aligned with any later panel schema or
  population-gate revision.

## Decision Log

- **C1** Compositional identity is initially a disposable shadow sidecar, not durable schema.
- **C2** Relation vocabulary is small/closed; target/scope is open/grounded.
- **C3** LLM agreement is diagnostic, never gold.
- **C4** Raw and compositional metrics remain visible together.
- **C5** No alias/template grind and no benchmark-specific canonicalization.
- **C6** Promotion remains per class and statistically pre-registered.
