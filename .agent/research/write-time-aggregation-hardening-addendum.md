# Write-time aggregation hardening addendum

**Follow-up to:** `../memory-aggregation-under-uncertainty.md`

This note preserves the reference design. It narrows several safety claims and records the evidence
required before an additional gate is treated as an improvement.

## 1. Write-set monotonicity is structural; precision improvement is empirical

An abstain-only veto chain is **write-set monotonic**: adding a veto can only remove candidate
materializations. It cannot create a write or rescue a candidate rejected upstream.

That does not prove that the new veto improves precision. A poorly targeted veto can reject mostly
correct candidates. Admit a new veto only after shadow-mode evidence shows that its blocked set is
less correct than the materialized baseline. Record its block rate, prevented authoritative errors,
false blocks, and net authoritative-fact precision delta.

## 2. Make the guarantee conditional and auditable

"Never write a wrong aggregate" is the target operational contract, not an unconditional theorem.
Perception, semantic coreference judgment, and branch enumeration are fallible. The defensible claim
is:

> The system must never materialize a claim stronger than its implemented certification rules
> justify, conditional on the correctness of deterministic inputs and the stated assumptions of each
> gate.

This does not weaken the abstention posture. It locates a failure in a deterministic input, a
certification assumption, or a gate implementation instead of hiding it behind an absolute promise.

## 3. Record corroboration independence as lineage

The draw/mechanism-independence distinction is useful only when a validator's lineage is inspectable.
Each candidate, corroboration, and audit receipt should identify:

- source episode and source-event identifiers;
- model family and version, prompt/template version, and extraction or audit method;
- whether the validator saw a prior candidate or aggregate value;
- derivation and input-set identifiers; and
- the fold time horizon.

A second pass sharing model, prompt, evidence selection, and prior-candidate exposure is another
draw, not an independent check for systematic bias.

## 4. Keep provisional point values review-only by default

An answering model can flatten a hedge into an assertion. Agent-facing fallback should therefore
default to an evidence set or a certified interval/bound. Retrieve a provisional point value only
when the answer-composition contract has been tested to keep evidence primary and prohibit promotion.
Otherwise retain the value for review, but not as answerable state.

## 5. Define invalidation and temporal semantics

For every aggregate, record event time, assertion time, ingestion time, derivation time, the complete
input set, and fold version. A correction, deletion, re-date, or later duplicate link invalidates the
affected derivation and schedules a blind re-gate; it must not silently patch a materialized value.
This makes replay, out-of-order episodes, and repair reproducible and prevents stale aggregates from
becoming hidden premises for later vetoes.

## 6. Evaluate the safety contract directly

Use a held-out adversarial harness covering repeated biased extraction, correlated re-narration,
genuine same-value recurrence, anchor-plus-delta, out-of-order episodes, and non-exhaustive ambiguity
branches. Compare the veto architecture with score-based materialization and blind re-derivation.
Report authoritative false positives first, followed by abstention, evidence-set/interval coverage,
cost, and per-gate effects. Keep corpus-specific knowledge entirely in the harness.
