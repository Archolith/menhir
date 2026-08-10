# Realization Coverage for Menhir

## Status

Research proposal — observation-ledger design required before implementation.

This document does not propose replacing Menhir's existing k-sample gate or deterministic folds. It proposes adding structured information about **how many meaningfully different perception paths support a committed interpretation**.

---

## Persistence reframe

The original proposal suggested persisting a realization fingerprint on each perception-produced `TypedAssertion` and grouping current assertions by `(source_key, interpretation_label, realization_fingerprint)`.

Review identified that this conflicts with Menhir's durable identity model. The `TypedAssertion` head is keyed by binding-stable `source_key`, perceiver version, and interpreted semantics. The head intentionally permits only **one current assertion per source claim**:

* same version plus same interpretation deduplicates;
* a newer perceiver version supersedes the prior current assertion;
* a different interpretation at the same or lower version remains non-current for audit.

Therefore:

* Realizations A and B using the same `perceiver_version` and interpretation collapse into one assertion.
* A newer realization version replaces the previous current interpretation.
* Two disagreeing realizations cannot both exist as current assertions for the same source claim.
* Querying only current assertions cannot reconstruct multi-realization support.

The correction is to record realization executions in a separate append-only observation layer that observes the `TypedAssertion` authority system rather than modifying it:

```text
PerceptionRealizationObservation {
    observation_id           // unique row id
    observation_key          // stable idempotency key (see below)
    namespace
    source_key
    realization_fingerprint
    realization_family_id
    perceiver_version
    interpretation_label

    // one observation per gated realization, not per stochastic sample
    realization_run_id       // identifies one k-sample gate execution
    sample_count             // number of stochastic samples in the run
    sample_agreement         // did the k samples agree?
    sample_result_hashes     // hashes of each sample's result payload

    committed_by_local_gate
    proposal_payload_hash
    grounded_span
    observed_at
    observation_lifecycle    // ACTIVE | SUPERSEDED | RETRACTED
    metadata_status          // COMPLETE | INCOMPLETE
}
```

### Observation idempotency key

A retry after a crash could create duplicate observations and inflate sample counts. Each observation carries a stable idempotency key:

```text
observation_key =
    hash(
        namespace,
        source_key,
        realization_fingerprint,
        realization_run_id
    )
```

`realization_run_id` identifies one execution of the k-sample gate. A retry of the same gate execution produces the same `observation_key` and is deduplicated on write. A genuinely new gate execution (new `realization_run_id`) produces a new observation.

### Idempotency collision behavior

If the same `observation_key` is written again:

```text
identical payload (same interpretation_label, sample_result_hashes,
    proposal_payload_hash) → idempotent success; no new row

different payload (different interpretation_label, sample_result_hashes,
    or proposal_payload_hash) → fail closed
    record IdempotencyCollisionViolation(observation_key, existing_row,
        attempted_row)
    do not silently overwrite either row
```

A reused or buggy `realization_run_id` that produces conflicting executions must not be hidden behind one key. The collision is surfaced for investigation.

### One observation per gated realization

The clean model is:

```text
one realization run
    = one configured k-sample gate execution
    = one PerceptionRealizationObservation
```

The observation contains the aggregate sample results (`sample_count`, `sample_agreement`, `sample_result_hashes`), not one row per stochastic sample. Do not create one ledger observation for each stochastic sample unless a separate `PerceptionSample` record type is introduced later for finer-grained analysis.

Graph relationships:

```text
(:PerceptionRealizationObservation)
    -[:INTERPRETS]->
(:TypedAssertion)            // when the observation maps to the current assertion

(:PerceptionRealizationObservation)
    -[:INTERPRETS_CLAIM]->
(:TypedAssertionHead)        // when disagreement prevents mapping to the current assertion
```

The existing `TypedAssertion` head remains the deterministic authority path. The observation ledger records how different procedures interpreted the claim.

**Do not put `realization_fingerprint` into `assertion_key`.** That would fork one source claim into multiple competing assertion identities and undermine the head/supersession invariant.

---

## Identity key

The original proposal defined state and conservation around `claim_key`. Review identified that `claim_key` contains `subject_uuid`, which is not merge-stable: it changes when an assertion is rebound to a surviving entity after a merge.

Every cross-realization grouping in this proposal uses:

```text
RealizationCoverageKey {
    namespace
    source_key
}
```

`source_key` intentionally omits subject identity and is the stable head identity across merges. Within a `RealizationCoverageKey`, interpretations are separated by `interpretation_label`:

```text
interpretations: Map<InterpretationLabel, Set<RealizationObservation>>
```

`claim_key` may remain historical metadata on individual observations but does not anchor realization coverage.

---

## Terminology note

The original proposal used "corroboration" throughout the technical model. Review flagged this as a leakage risk: different extraction procedures are interpreting the **same source evidence**. They do not provide independent evidence that the source statement is true.

They establish:

```text
cross-realization interpretation agreement
```

not:

```text
real-world factual corroboration
```

This proposal renames the technical concept:

```text
CrossRealizationAgreement
```

`corroboration` is reserved for distinct underlying evidence sources, which is a different and future concern.

---

## Plain-language explanation

Suppose Menhir asks the same person the same question five times.

If the person gives the same answer five times, that tells us the person is consistent. It does not tell us the answer is correct.

The same problem exists with LLM extraction.

Menhir can run the same model and prompt several times:

```text
Run 1: Rachel moved to the suburbs.
Run 2: Rachel moved to the suburbs.
Run 3: Rachel moved to the suburbs.
Run 4: Rachel moved to the suburbs.
Run 5: Rachel moved to the suburbs.
```

That is useful. It tells us the extraction is repeatable.

But all five runs may share the same blind spot because they used:

* the same model;
* the same prompt;
* the same context;
* the same entity extractor;
* the same grounding algorithm;
* the same binding logic.

Five matching runs from one process are still evidence from **one process**.

Now imagine Menhir reaches the same interpretation through two genuinely different routes:

```text
Route A:
Combined entity-and-relation extraction

Route B:
Typed scalar extraction directly from the grounded source span
```

Agreement between those routes is more informative because the routes do not fail in exactly the same way.

This does not prove that the answer is true. The two routes may still share a model, training data, or earlier processing stage.

The goal is therefore not to declare methods "independent." Menhir usually cannot prove that.

The goal is to record the shape of support honestly:

```text
Five matching samples
One model family
One prompt
One extraction algorithm
One grounding path
```

versus:

```text
Five matching samples
Two model families
Two extraction algorithms
Two grounding paths
```

Both interpretations may be committed under the same existing rules. The second interpretation simply has broader realization coverage.

In plain words:

> Menhir should distinguish "we asked the same system several times" from "different systems reached the same result."

---

## Problem

Menhir's k-sample typed-scalar gate measures agreement across repeated extraction samples.

That provides evidence of repeatability:

```text
Did the configured perceiver repeatedly interpret this source claim the same way?
```

It does not establish methodological diversity:

```text
Would a materially different perception path reach the same interpretation?
```

These two properties must not be conflated.

A systematic failure may be perfectly repeatable. For example, the same context constructor might omit the sentence containing an update on every sample. Increasing the sample count would not expose the failure.

---

## Design principle

Menhir should preserve two distinct measurements:

```text
sample agreement
```

and:

```text
realization coverage
```

Sample agreement belongs to the existing proposal gate.

Realization coverage describes the distinct processing configurations that produced an interpretation, recorded as observations in a separate ledger.

Neither should be converted into an undefined confidence score.

---

## Terminology

### Sample

One execution of a particular perception configuration.

### Realization

A structured description of the processing path used to produce an interpretation.

### Realization fingerprint

The exact immutable processing configuration of one perception execution. Two executions with different random seeds but otherwise identical configuration share a fingerprint.

### Realization family

A deliberately coarse, stable grouping of realizations that share a meaningful failure domain, such as the same extraction algorithm or model family. Families do not change for every prompt patch or model upgrade. A family is what a dimension-aware actionability policy should target.

### CrossRealizationAgreement

Agreement between committed interpretations produced by observations from **different realization families** (not just different fingerprints). Two patch versions of the same pipeline may have different fingerprints but the same family; they do not constitute cross-realization agreement.

This is metadata. It does not establish that the underlying source claim is factually true, and it does not automatically supersede Menhir's existing authority and evidence-tier rules.

A finer measurement, `multi_fingerprint_agreement`, is also reported in the topology. It is true when two observations from the same family but different fingerprints agree. It is a weaker signal than `CrossRealizationAgreement` (family-level agreement) because same-family observations may share failure paths.

---

## Realization fingerprint and family

Each perception-produced observation should be attributable to a structured realization descriptor.

```text
RealizationDescriptor {
    perceiver_family
    model_provider
    model_family
    model_version
    prompt_version
    context_builder_version
    extraction_algorithm
    grounding_algorithm
    entity_resolution_algorithm
    binding_algorithm
    parser_version
    schema_version
}
```

Two derived identifiers are computed from the descriptor:

```text
realization_fingerprint =
    hash(canonicalize(RealizationDescriptor))
    // exact immutable processing configuration

realization_family_id =
    hash(canonicalize((
        perceiver_family,
        model_family,
        context_builder_family,    // a coarser grouping than context_builder_version
        extraction_algorithm,
        grounding_algorithm_family,
        binding_algorithm_family
    )))
    // intentionally coarse; does not change for every prompt patch or model upgrade
```

The fingerprint should describe behaviorally relevant components, not incidental deployment details such as a host name or process ID.

The distinction matters for actionability. The fingerprint is exact and is used for provenance and deduplication. The family is coarse and is what dimension-aware actionability policies should target. Using fingerprint count for actionability is easy to game accidentally: two patch versions of effectively the same pipeline could satisfy `distinct_fingerprints >= 2` without adding meaningful failure-path diversity.

---

## Example

Consider three extraction configurations, each producing one or more `PerceptionRealizationObservation` rows for the same `(namespace, source_key)`.

### Configuration A1

```text
model_family: qwen
prompt_version: typed-scalar-v4
context_builder: relevant-episodes-v2
extraction_algorithm: separate-entity-edge
grounding_algorithm: unique-span-v1
binding_algorithm: linked-name-match-v1
```

### Configuration A2

Identical to A1 except for a random seed.

Under the one-observation-per-gated-realization model, A1 and A2 are two separate gate executions (two `realization_run_id`s), each producing one observation. Both observations share the same `realization_fingerprint` and `realization_family_id`.

### Configuration B

```text
model_family: gemma
prompt_version: typed-scalar-v5
context_builder: source-claim-window-v1
extraction_algorithm: combined-proposition
grounding_algorithm: unique-span-v1
binding_algorithm: linked-name-match-v1
```

B is a distinct realization fingerprint and (depending on the family grouping rules) a distinct `realization_family_id` from A. B is one gate execution producing one observation.

A source claim supported by observations from A1, A2, and B therefore has:

```text
observations_total = 3      // three PerceptionRealizationObservation rows
distinct_fingerprints = 2   // A1 and A2 share a fingerprint; B is distinct
distinct_realization_families = 2   // assuming A and B fall into different families
distinct_model_families = 2
distinct_extraction_algorithms = 2
distinct_grounding_algorithms = 1
distinct_binding_algorithms = 1
```

Each observation also carries its own `sample_count` (the k stochastic samples within that gate execution). The coverage-level `observations_total` counts gated realizations, not individual stochastic samples.

This is more informative than calling it "high confidence."

---

## Proposed state

Realization coverage is derived from the observation ledger, not stored as a new authoritative View.

```text
RealizationCoverage {
    coverage_key: (namespace, source_key)

    interpretations: Map<InterpretationLabel, InterpretationCoverage>

    base_status         // SINGLE_FAMILY | CROSS_REALIZATION_AGREEMENT
                        // | CROSS_FAMILY_DISAGREEMENT
    interpretation_disagreement // bool; true if >1 active complete interpretation exists
    multi_fingerprint_agreement // bool; true if ≥2 fingerprints from one family agree on one interpretation
    metadata_incomplete // bool; true if any active observation has metadata_status = INCOMPLETE
}

InterpretationCoverage {
    interpretation_label
    observations: List<PerceptionRealizationObservation>

    observations_total       // count of gated realizations supporting this interpretation
    observations_agreeing    // count whose sample_agreement = true

    realization_fingerprints
    realization_family_ids
    model_families
    prompt_versions
    context_builders
    extraction_algorithms
    grounding_algorithms
    binding_algorithms

    support_topology
}
```

Suggested base statuses:

```text
SINGLE_FAMILY                         // one realization family (possibly multiple fingerprints)
CROSS_REALIZATION_AGREEMENT           // ≥2 distinct families agree on one interpretation
CROSS_FAMILY_DISAGREEMENT             // different families support different interpretations
```

Plus an orthogonal `interpretation_disagreement` flag (bool): true when more than one active complete interpretation exists. Same-family disagreement sets this flag but does not set `CROSS_FAMILY_DISAGREEMENT`, avoiding overstating methodological diversity when the disagreement is within a single failure domain.

Plus an orthogonal `multi_fingerprint_agreement` flag (bool): true when ≥2 fingerprints from one family agree on one interpretation. This is a weaker signal than `CROSS_REALIZATION_AGREEMENT` and is reported in the topology but does not drive the base_status.

Plus an orthogonal `metadata_incomplete` flag (bool). When true, it overlays the base status and indicates that at least one active observation has `metadata_status = INCOMPLETE`. Observations with incomplete metadata are retained in the ledger but excluded from fingerprint/family counts until their descriptor is completed.

---

## Admission rule

All valid observations are admitted to the ledger. The admission rule does not reject observations based on fingerprint duplication:

```text
ADMITTED_TO_LEDGER(observation) :=
    observation.committed_by_local_gate = true
    AND (
        no row exists with observation_key
        OR existing row has identical immutable payload
    )
    // immutable payload = (interpretation_label, sample_result_hashes,
    //   proposal_payload_hash, realization_fingerprint, realization_run_id)
```

An identical retry is idempotent: it is a valid write that produces no new row. A genuinely new gate execution (new `realization_run_id`) produces a new `observation_key` and a new row.

If the same `observation_key` exists with a different immutable payload, the write fails closed:

```text
IdempotencyCollisionViolation(observation_key, existing_row, attempted_row)
```

Neither row is overwritten. The collision is surfaced for investigation.

Superseded and retracted observations remain valid admitted ledger records. They are not removed from the ledger when their lifecycle transitions. The predicate that determines whether an observation *supports current coverage* is separate:

```text
ACTIVE_SUPPORTS(observation, interpretation) :=
    observation.metadata_status = COMPLETE
    AND observation.source_key == interpretation.source_key
    AND observation.namespace == interpretation.namespace
    AND observation.interpretation_label == interpretation.interpretation_label
    AND observation.observation_lifecycle = ACTIVE
```

Only `ACTIVE` observations support current coverage. `SUPERSEDED` and `RETRACTED` observations are retained for provenance, disagreement analysis, and regression detection, but they do not contribute to current coverage status or distinct-fingerprint/family counts.

Repeated observations from the same fingerprint are all retained in the ledger. They increase `observations_total` and `observations_agreeing` for that interpretation. Deduplication happens only when computing the distinct counts:

```text
distinct_fingerprints   = | { o.realization_fingerprint : o in supporting_observations } |
distinct_realization_families = | { o.realization_family_id : o in supporting_observations } |
```

The support rule does not exclude repeated executions. A later observation from the same fingerprint may supersede an earlier one from that fingerprint (via `observation_lifecycle` transition `ACTIVE → SUPERSEDED`), but both rows remain in the ledger.

Observations are admitted to the ledger regardless of whether they agree with the current `TypedAssertion`. An observation that disagrees with the current assertion is recorded with `observation_lifecycle = ACTIVE` and a relationship `:INTERPRETS_CLAIM` to the head. It is not silently discarded.

---

## Disagreement rule

When two observations produce different `interpretation_label` for the same `(namespace, source_key)`:

```text
interpretation_disagreement = true

if the disagreeing observations are from different realization families:
    base_status = CROSS_FAMILY_DISAGREEMENT
else:
    base_status remains as computed (SINGLE_FAMILY or CROSS_REALIZATION_AGREEMENT)
    // same-family disagreement is visible via interpretation_disagreement
    // without overstating methodological diversity
```

Menhir must not average incompatible interpretations.

The disagreement record should preserve:

```text
namespace
source_key
interpretation_a
observation_set_a     // list of PerceptionRealizationObservation
interpretation_b
observation_set_b
grounded spans
perceiver versions
realization fingerprints
realization family ids
```

Existing deterministic authority rules continue to decide which interpretation, if any, may materialize as the current `TypedAssertion`. The observation ledger records all interpretations; the assertion head records the authoritative one.

Realization disagreement can:

* prevent a CrossRealizationAgreement upgrade;
* trigger offline investigation;
* identify a regression associated with one realization family;
* supply examples to the extraction bench.

It should not permit an LLM to select the winner.

---

## Independence claims

Menhir should not claim that two realizations are independent.

A different prompt is not necessarily independent.

A different model may not be independent if both models share:

* training sources;
* a common context constructor;
* the same entity binding;
* the same parser;
* the same grounding failure;
* the same upstream source omission.

The system should report observable diversity:

```text
two model families
one context builder
two extraction algorithms
one binding algorithm
```

It should not reduce this structure to:

```text
independence = 0.8
```

unless a future, explicitly defined dependency model gives that number defensible semantics.

The `realization_family_id` is a deliberately coarse grouping, not an independence claim. Two distinct families may still share a failure path that the family grouping does not capture. The topology, not the family count, is what a careful consumer inspects.

---

## Interaction with the k-sample gate

The current gate remains responsible for committing a proposal under one configured perception procedure.

```text
k samples
    ↓
source-claim-first consistency gate
    ↓
committed TypedScalarDecision
    ↓
PerceptionRealizationObservation written to the ledger
```

Realization coverage operates across observations in the ledger:

```text
observation from realization A
observation from realization B
observation from realization C
    ↓
coverage status derived from active observations
```

These stages answer different questions.

### Gate question

Did repeated samples from this procedure agree?

### Coverage question

How many materially distinct procedures support the same interpretation?

---

## Authority semantics

Initial implementation should be conservative.

```text
SINGLE_FAMILY:
    Existing assertion and View behavior remains unchanged.

CROSS_REALIZATION_AGREEMENT:
    Add cross-realization agreement metadata.
    Do not automatically create a stronger evidence tier.

CROSS_FAMILY_DISAGREEMENT:
    Preserve the disagreement.
    Do not create agreement metadata.
    Optionally block high-stakes actionability.

interpretation_disagreement = true (with base_status != CROSS_FAMILY_DISAGREEMENT):
    Same-family disagreement is visible but does not overstate
    methodological diversity. Do not create agreement metadata.
    Surface for investigation; do not block actionability unless
    a consumer policy explicitly requires it.

metadata_incomplete = true:
    The base_status is computed from complete observations only.
    Incomplete observations are retained but do not count toward
    distinct fingerprints or family ids. The flag surfaces that
    a potential additional realization exists but cannot be confirmed.
```

A later design may introduce action policies that require more than one realization family, but that should be separate from the core truth fold.

### Dimension-aware actionability

Actionability policies must not use `distinct_fingerprints` alone. A version upgrade that changes the prompt or model version produces a new fingerprint without adding meaningful failure-path diversity.

Policies should target `realization_family_id` or specific dimensions:

```text
require:
    distinct_realization_families >= 2
    AND distinct_extraction_algorithms >= 2
    AND distinct_context_builder_families >= 2
```

A claim that satisfies `distinct_fingerprints >= 2` but not the dimension-aware policy is not hidden. It is returned with its actual topology. The consumer decides whether to proceed.

---

## Provenance requirements

Every observation in the ledger must be traceable to:

```text
observation_id
namespace
source_key
episode_uuid
grounded span
interpretation label
perceiver version
realization descriptor
realization fingerprint
realization family id
sample gate result
related assertion id (via :INTERPRETS or :INTERPRETS_CLAIM)
```

Changing a realization descriptor does not rewrite old observations. A new configuration produces a new fingerprint, a new family id (if the family grouping changes), and new observations.

Observations are append-only. An observation is never edited in place. Lifecycle transitions (`ACTIVE → SUPERSEDED`, `ACTIVE → RETRACTED`) and metadata_status transitions (`INCOMPLETE → COMPLETE`) are recorded as append-only events on the observation, but the original observation row is preserved.

---

## Suggested implementation sequence

### Phase 1: Observation ledger

Add the `PerceptionRealizationObservation` node type and the `:INTERPRETS` / `:INTERPRETS_CLAIM` relationships. Persist one observation per perception execution, with the full `RealizationDescriptor`, fingerprint, and family id.

The existing `TypedAssertion` head and the k-sample gate are unchanged. Observations are written alongside assertions, not into them.

### Phase 2: Coverage query

Implement a deterministic query that groups active observations by:

```text
(namespace, source_key, interpretation_label, realization_fingerprint)
```

Return the support topology per `(namespace, source_key)`.

### Phase 3: Disagreement detection

Detect when distinct realization fingerprints produce observations with different `interpretation_label` for the same `(namespace, source_key)`.

Record or report the disagreement without changing assertion authority.

### Phase 4: Bench integration

Use disagreement cases to build targeted extraction tests and compare perceiver versions.

### Phase 5: Optional actionability policy

Allow a consumer to request a dimension-aware policy:

```text
require:
    distinct_realization_families >= 2
    AND distinct_extraction_algorithms >= 2
```

This affects whether an answer is safe for that consumer to act upon. It does not rewrite the underlying View or assertion head.

---

## Minimal experiment

Create two realization configurations.

### Realization A

The existing typed-scalar extractor.

### Realization B

A combined proposition extractor or a second model family using an independently versioned context constructor.

Run both over a small corpus containing:

* straightforward scalar statements;
* temporal updates;
* generic locations such as "the suburbs";
* ambiguous subjects;
* one intentionally misleading sentence.

For each gated realization, write one `PerceptionRealizationObservation` row (with a unique `realization_run_id` and `observation_key`) pointing at the resulting `TypedAssertion` (via `:INTERPRETS`) or at the head (via `:INTERPRETS_CLAIM`) when the interpretation disagrees with the current assertion.

Verify:

1. Repeated gate executions of A increase `observations_total` but not `distinct_fingerprints`.
2. Agreement between A and B produces `base_status = CROSS_REALIZATION_AGREEMENT`.
3. Cross-family disagreement produces `base_status = CROSS_FAMILY_DISAGREEMENT`.
3a. Same-family disagreement produces `interpretation_disagreement = true` but `base_status` remains `SINGLE_FAMILY`.
4. No disagreement is silently averaged.
5. Existing ScalarStateView results remain byte-identical when realization coverage is disabled.
6. Removing B's observations (transition to `observation_lifecycle = RETRACTED`) downgrades the coverage status without deleting historical provenance.
7. A version upgrade of A that changes only the prompt produces a new fingerprint but not a new `realization_family_id`.
8. A retry of the same gate execution (same `realization_run_id`) produces the same `observation_key` and is deduplicated.

---

## Main correctness risks

### False diversity

Two superficially different configurations may share the same important failure path.

Mitigation: preserve the full support topology rather than declaring independence. Use `realization_family_id` for actionability, not fingerprint count. Allow dimension-aware policies that target specific descriptor fields.

### Metadata drift

A behaviorally important component may change without its version being updated.

Mitigation: generate descriptors from versioned code and configuration where possible. Set `metadata_status = INCOMPLETE` rather than guessing missing fields.

### Authority leakage

Consumers may mistakenly interpret multiple realizations as proof of truth.

Mitigation: keep cross-realization agreement metadata separate from evidence tier and deterministic fold authority. The terminology (`CrossRealizationAgreement`, not `corroboration`) reinforces that the agreement is over interpretation, not over source truth.

### Cost explosion

Running many perception configurations on every episode may be expensive.

Mitigation: begin with offline bench runs, targeted high-value claims, or scheduled cross-realization agreement runs rather than synchronous universal execution.

### Observation-ledger drift from assertion head

If observations are written but the corresponding `TypedAssertion` is not (or is superseded independently), the ledger may reference assertions that no longer exist or no longer represent the current interpretation.

Mitigation: observations reference the head via `:INTERPRETS_CLAIM` when there is no current matching assertion, and reference the specific assertion via `:INTERPRETS` only when the observation's interpretation matches the current head interpretation. A periodic reconciliation job should detect observations whose `:INTERPRETS` target has been superseded and downgrade them to `:INTERPRETS_CLAIM`.

---

## Coverage status is derived, not stored as a lifecycle

The original proposal required append-only lifecycle transition events for agreement and disagreement. Review identified this as premature: with an append-only observation ledger, current coverage can be deterministically derived without a second event log.

```text
active observations
    grouped by (namespace, source_key)
    grouped by interpretation_label
    grouped by realization_family_id
```

`METADATA_INCOMPLETE` is not a lifecycle state. It is an orthogonal flag. The original proposal conflated it with `observation_lifecycle`, using it as both a status and an overlay. The corrected model separates the two:

```text
ObservationLifecycle =
    ACTIVE
  | SUPERSEDED
  | RETRACTED

metadata_status =
    COMPLETE
  | INCOMPLETE
```

`metadata_status` is a separate field on the observation. An observation can be `ACTIVE + INCOMPLETE` (actively supporting an interpretation but excluded from fingerprint/family counts until its descriptor is completed) or `SUPERSEDED + COMPLETE` (superseded by a newer observation from the same fingerprint, with complete metadata retained for provenance).

The coverage status for a `(namespace, source_key)` is a pure function of the active observations:

```text
interpretation_disagreement =
    count(distinct interpretation_labels among active complete observations) > 1

cross_family_disagreement =
    different realization families support different interpretations

base_status =
    if cross_family_disagreement:
        CROSS_FAMILY_DISAGREEMENT
    elif count(distinct realization_family_ids among active complete observations) >= 2:
        CROSS_REALIZATION_AGREEMENT       // family-level agreement
    else:
        SINGLE_FAMILY                     // one family (possibly multiple fingerprints)

multi_fingerprint_agreement =
    exists interpretation I such that
        at least two fingerprints
        from one family
        actively support I
    // weaker than CrossRealizationAgreement; reported in topology but
    // does not drive base_status

metadata_incomplete =
    any active observation has metadata_status = INCOMPLETE

coverage_status =
    (base_status, interpretation_disagreement, multi_fingerprint_agreement, metadata_incomplete)
```

`interpretation_disagreement` is an orthogonal flag. Same-family disagreement (two fingerprints from one family disagreeing) sets `interpretation_disagreement = true` but does not set `base_status = CROSS_FAMILY_DISAGREEMENT`. This avoids overstating methodological diversity when the disagreement is within a single failure domain.

Observations with `metadata_status = INCOMPLETE` are excluded from the `base_status` computation (they do not count toward distinct interpretation labels or family ids) but are retained in the ledger. Their incompleteness is surfaced as the `metadata_incomplete` flag on the coverage status.

Historical agreement and disagreement can be reconstructed from observation lifecycle transitions (`ACTIVE → SUPERSEDED`, `ACTIVE → RETRACTED`) and from `TypedAssertion` supersession events. A separate lifecycle-event log should only be added if a concrete query is identified that cannot be answered from observations plus assertion supersession.

The only events recorded on observations are transitions on the observation's own fields:

```text
ObservationLifecycle:
    ACTIVE → SUPERSEDED        // an observation was written with incorrect metadata
                               // or payload linkage and a replacement observation
                               // intentionally supersedes it. Normal repeated gate
                               // executions and model upgrades do NOT supersede;
                               // they are new observations that remain independently ACTIVE.
    ACTIVE → RETRACTED         // the source or perceiver was discredited

metadata_status:
    INCOMPLETE → COMPLETE      // descriptor was backfilled from versioned configuration
    COMPLETE → INCOMPLETE      // (rare) a previously-complete descriptor is found to be missing a field
```

These are append-only transitions on observation rows, not a parallel event log for coverage status.

---

## Conservation of method-class coverage

```text
supporting_families(coverage_key, interpretation_label)
=
    { observation.realization_family_id :
        observation in active_observations(coverage_key, interpretation_label) }
```

A family id disappearing from `supporting_families` without an observation lifecycle transition (`ACTIVE → SUPERSEDED` or `ACTIVE → RETRACTED`) is a correctness failure.

```text
all_observations(coverage_key)
=
    observations_active(coverage_key)
  + observations_superseded(coverage_key)
  + observations_retracted(coverage_key)
```

`metadata_status` is orthogonal to `observation_lifecycle`. An observation can be `ACTIVE + INCOMPLETE`, `SUPERSEDED + COMPLETE`, etc. The lifecycle conservation equation does not include a `metadata_incomplete` category because incompleteness is a flag, not a lifecycle state.

Within active observations, completeness further partitions:

```text
observations_active(coverage_key)
=
    active_complete_observations(coverage_key)
  + active_incomplete_observations(coverage_key)

active_complete_observations(coverage_key)
=
    observations_supporting(coverage_key, interpretation_a)
  + observations_supporting(coverage_key, interpretation_b)
  + ...
  + observations_disputed(coverage_key)
```

An observation appearing in none of these categories is a correctness failure.

An observation appearing in more than one category (for example, both `supporting` an interpretation and `superseded`) is also a correctness failure.

The coverage key is `(namespace, source_key)`. `claim_key` is historical metadata on the observation, not the grouping key, because `claim_key` contains `subject_uuid` and is not merge-stable.

---

## Composition and consumption rules

### Can a downstream View consume realization coverage?

Realization coverage is metadata derived from the observation ledger, not a View. A downstream consumer may read the coverage status and topology, but the coverage does not feed a deterministic fold.

This preserves the separation between the truth fold (which decides what is committed) and the coverage layer (which describes how broadly the committed result is supported).

### How does a consumer request multi-realization support?

The consumer passes a dimension-aware policy to the recall or actionability layer:

```text
require:
    distinct_realization_families >= N
    AND distinct_extraction_algorithms >= M
    AND distinct_context_builder_families >= K
```

The layer checks:

```text
IF observation_ledger satisfies the policy
   AND base_status == CROSS_REALIZATION_AGREEMENT
   AND metadata_incomplete == false
THEN requirement satisfied
ELSE requirement not satisfied
```

A claim that does not meet the requirement is not hidden. It is returned with its actual coverage status and topology. The consumer decides whether to proceed.

### How is double-counting prevented?

Each observation carries its realization fingerprint. Two observations from the same fingerprint for the same `(namespace, source_key)` and `interpretation_label` are one realization, not two. Repeated samples do not produce new fingerprints.

Family-id deduplication is separate: two distinct fingerprints that fall into the same `realization_family_id` contribute one family to the family count, not two. This prevents a version upgrade from inflating the actionability-relevant count.

### How is topology propagated to downstream consumers?

The full support topology is passed, not a reduced score. A consumer receives:

```text
observations_total
distinct_fingerprints
distinct_realization_families
distinct_model_families
distinct_extraction_algorithms
distinct_grounding_algorithms
distinct_binding_algorithms
base_status
interpretation_disagreement
multi_fingerprint_agreement
metadata_incomplete
```

The consumer does not receive an `independence` score. Reducing the topology to a single number would destroy the information that makes the coverage useful.

### Can coverage from one source claim feed coverage for another?

No. Realization coverage is per `(namespace, source_key)`. A downstream claim that depends on multiple upstream source claims does not inherit their coverage. Each source claim carries its own topology.

If a consumer needs combined coverage across multiple source claims, it must request coverage for each claim separately and apply its own combination policy. The coverage layer does not compose.

---

## Adversarial histories

### H1. False diversity

Two realizations have different fingerprints but share a failure path.

```text
Realization A: model_family=qwen, extraction=separate-entity-edge
Realization B: model_family=gemma, extraction=combined-proposition
```

Both use the same context builder, which omits a critical sentence. Both extract the same wrong interpretation.

Coverage behavior:

```text
base_status = CROSS_REALIZATION_AGREEMENT
interpretation_disagreement = false
distinct_fingerprints = 2
distinct_realization_families = 2   // assuming the family grouping separates them
distinct_model_families = 2
distinct_extraction_algorithms = 2
distinct_context_builder_families = 1
```

The coverage is honestly reported. The shared context builder family is visible in the topology. The consumer can see that the diversity is not complete.

The coverage does not claim independence. It reports the shape. A consumer policy that requires `distinct_context_builder_families >= 2` would reject this claim even though `distinct_fingerprints >= 2` and `distinct_realization_families >= 2`.

### H2. Model upgrade produces new fingerprint, same family

Sequence:

```text
1. Realization A (fingerprint f1, family F) commits interpretation I.
   Observation O1 written, observation_lifecycle=ACTIVE, metadata_status=COMPLETE.
2. Realization A is upgraded. New fingerprint is f2, same family F.
3. Realization A (f2) commits interpretation I again.
   Observation O2 written, observation_lifecycle=ACTIVE, metadata_status=COMPLETE.
```

Coverage behavior:

The ledger retains O1 (ACTIVE) and O2 (ACTIVE). Normal repeated gate executions and model upgrades do not supersede; they are new observations that remain independently active.

```text
distinct_fingerprints (active) = 2   // f1 and f2
distinct_realization_families (active) = 1   // both in family F
```

The family count did not increase because the upgrade stayed within the same family. Both observations contribute to `observations_total`. The `multi_fingerprint_agreement` flag is true (two fingerprints from one family agree).

If the upgrade introduced a regression visible on other source claims, the disagreement between f1 and f2 on those claims is visible in the ledger with `interpretation_disagreement = true` but `base_status` remaining `SINGLE_FAMILY` (same-family disagreement).

### H3. Disagreement that later resolves

Sequence:

```text
1. Realization A (family F_A) commits interpretation I1.
   Observation O_A written, observation_lifecycle=ACTIVE, :INTERPRETS the current assertion.
2. Realization B (family F_B, F_B != F_A) commits interpretation I2, where I2 != I1.
   Observation O_B written, observation_lifecycle=ACTIVE, :INTERPRETS_CLAIM the head
   (because I2 does not match the current assertion's interpretation).
3. base_status = CROSS_FAMILY_DISAGREEMENT
   interpretation_disagreement = true
4. Realization B is found to have a bug. Its observations are transitioned to RETRACTED.
5. Realization C (family F_C, F_C != F_A and F_C != F_B) commits I1.
   Observation O_C written, observation_lifecycle=ACTIVE, :INTERPRETS the current assertion.
```

Coverage behavior:

The disagreement record is the ledger itself; it is not deleted when B is retracted. O_B remains in the ledger with `observation_lifecycle = RETRACTED`.

After step 5:

```text
base_status = CROSS_REALIZATION_AGREEMENT
interpretation_disagreement = false
supporting_families = {F_A, F_C}
```

The historical disagreement (A vs B) remains queryable via O_B's `RETRACTED` lifecycle. The current coverage reflects A and C. B's retracted observations are preserved in provenance but do not contribute to current coverage.

### H4. Metadata incomplete

Sequence:

```text
1. Realization A commits interpretation I with a full descriptor.
   Observation O_A written, observation_lifecycle=ACTIVE, metadata_status=COMPLETE.
2. Realization B commits interpretation I, but its descriptor is missing
   the context_builder_version field.
   Observation O_B written, observation_lifecycle=ACTIVE, metadata_status=INCOMPLETE.
```

Coverage behavior:

```text
O_A: ACTIVE, COMPLETE. Fingerprint computed, family id computed.
O_B: ACTIVE, INCOMPLETE. Excluded from fingerprint/family counts.
```

B does not increase the realization count until its metadata is completed. The coverage status is:

```text
base_status         = SINGLE_FAMILY          (computed from O_A only)
multi_fingerprint_agreement = false          (only one fingerprint)
metadata_incomplete = true                  (O_B is active but incomplete)
```

Repair: backfill the missing descriptor field from versioned configuration, transitioning O_B's `metadata_status` from `INCOMPLETE` to `COMPLETE`. If the field cannot be backfilled, O_B remains incomplete indefinitely. The coverage honestly reports that a potential second realization exists but cannot be confirmed.

### H5. Disagreement on one source claim, agreement on another

Sequence:

```text
1. Realization A (family F_A) and Realization B (family F_B, F_B != F_A) both commit I1 for source claim X.
2. Realization A commits I2 for source claim Y.
3. Realization B commits I3 for source claim Y, where I3 != I2.
```

Coverage behavior:

Coverage is per `(namespace, source_key)`. The two source claims are independent.

```text
Source claim X: base_status = CROSS_REALIZATION_AGREEMENT, families = {F_A, F_B}
Source claim Y: base_status = CROSS_FAMILY_DISAGREEMENT,
                interpretation_disagreement = true,
                interpretations = {I2: {F_A}, I3: {F_B}}
```

A disagreement on one source claim does not contaminate the coverage of another. A consumer policy that requires cross-realization agreement for source claim Y would reject it, while accepting source claim X.

### H6. Merge invalidates claim_key but not source_key

Sequence:

```text
1. Source claim X is bound to entity E1. claim_key contains E1's subject_uuid.
   Observations O1 and O2 reference X via source_key.
2. E1 is merged into surviving entity E2. The assertion is rebound.
   claim_key changes (subject_uuid now E2's). source_key is unchanged.
```

Coverage behavior:

The coverage key is `(namespace, source_key)`. The merge does not change the coverage key. Observations O1 and O2 remain grouped under the same coverage key.

`claim_key` on the observations is historical metadata. It is not used for grouping. The merge does not invalidate the coverage.

This is why the coverage key is `(namespace, source_key)` and not `(namespace, claim_key)`.

---

## Decision

Research proposal — observation-ledger design required before implementation.

Realization Coverage is worth pursuing as a research and observability layer, but the original proposal's persistence model conflicted with Menhir's one-current-interpretation-per-source-claim identity model. The correction is to record realization executions in a separate append-only observation ledger that observes the `TypedAssertion` authority system rather than modifying it.

The first implementation should:

* add the `PerceptionRealizationObservation` node type and the `:INTERPRETS` / `:INTERPRETS_CLAIM` relationships;
* record realization fingerprints and family ids on observations, not on assertions;
* group coverage by `(namespace, source_key)`, not by `claim_key`;
* distinguish samples from realizations via fingerprint deduplication;
* distinguish fingerprint diversity from family diversity via `realization_family_id`;
* expose the full support topology;
* preserve disagreement in the ledger;
* derive coverage status from active observations rather than storing a parallel event log;
* leave existing `ScalarStateView` and `TypedAssertion` authority unchanged.

Actionability policies must be dimension-aware (targeting `realization_family_id` or specific descriptor fields), not fingerprint-count-based. The term `corroboration` is avoided; `CrossRealizationAgreement` is used instead, to prevent the implication that agreement across interpretations of the same source establishes source truth.

The central invariant is:

> Repeating one method measures repeatability. Agreement across distinct methods measures broader support. Menhir must record the difference without pretending it proves independence.
