# Projection Coverage Audit for Menhir Views

## Status

Research proposal — requires fold-parity revision before implementation.

This document extends the invariants already established by ScalarStateView binding repair and the durable `projection_pending` marker.

---

## Audit reframe

This proposal was originally framed as a single audit over assertion-to-View existence. Review identified that framing as insufficient because ScalarStateView is not a one-assertion-to-one-View projection.

Menhir's actual projection is:

```text
1. Load all current, fully bound assertions for an entity.
2. Group them into slots.
3. Select the latest absolute anchor.
4. Apply only deltas after that anchor.
5. Write one View per resulting slot.
6. Record the exact assertion IDs that actually contributed.
```

A bound assertion with a matching View is therefore not necessarily `MATERIALIZED` in any meaningful sense. The View may contain the wrong value, use the wrong anchor, miss a required delta, carry a stale `scalar_contributors` set, or have the correct key with stale contents.

The audit is reframed as two connected audits:

### Audit A — Assertion lifecycle coverage

Checks the lifecycle and binding state of every durable assertion:

```text
binding_pending
projection_pending
binding mismatch
supersession
namespace fidelity
repair discoverability
```

This is the audit the rest of this document (sections "Purpose" through "Crash safety") specifies, with the orthogonal-field correction applied.

### Audit B — Fold projection parity

Checks that the materialized Views equal the deterministic fold of the current materializable assertions:

```text
deterministic_fold(current_materializable_assertions)
==
current_materialized_views
```

Compared per entity and namespace:

```text
desired slots
desired values
desired valid_at
desired contributor IDs
desired effective tier
desired episode provenance
desired abstentions
```

against the actual Views.

Audit B is specified in its own section near the end of this document. The two audits are connected: Audit A establishes which assertions are eligible to feed the fold; Audit B checks that the fold of those assertions matches what is materialized.

---

## Purpose

Every durable assertion that is eligible for consideration by a View must remain accounted for, and every materialized View must equal the deterministic fold of the assertions that should feed it.

A committed assertion must never silently disappear between:

```text
durable assertion storage
```

and:

```text
materialized View state
```

An assertion may legitimately fail to appear in a View, but Menhir must record why.

Examples of legitimate reasons include:

* the subject is not uniquely bound;
* projection is committed but incomplete;
* the assertion was superseded;
* the assertion was vetoed or declared ineligible;
* the assertion is advisory only;
* projection encountered an explicit error.

The invalid state is:

```text
A current committed assertion exists,
does not appear in a View,
has no pending marker,
has no exclusion status,
and has no error status.
```

That is an unaccounted claim.

---

## Governing invariant

For every current source claim, the audit must produce a coherent multi-dimensional state. The original proposal forced exactly one state from a single enum. Review identified that the proposed states mix orthogonal dimensions and are not mutually exclusive (for example, `binding_pending` assertions are described as advisories elsewhere in the current repository, so one assertion can be both `BINDING_PENDING` and `ADVISORY`).

The audit uses orthogonal fields instead of a single enum:

```text
AssertionLifecycle =
    CURRENT
  | SUPERSEDED

BindingStatus =
    BOUND
  | BINDING_PENDING
  | BINDING_MISMATCH

ProjectionStatus =
    NOT_REQUIRED
  | PROJECTION_PENDING
  | PROJECTED
  | PROJECTION_ERROR

EligibilityRole =
    MATERIALIZABLE        // eligible to feed the deterministic fold
  | BINDING_ADVISORY      // binding_pending; cannot be materialized
  | RETRACTED             // source retracted
  | OPERATOR_VETOED       // explicit operator veto

FoldRole =                 // defined only when eligibility_role = MATERIALIZABLE
    CONTRIBUTOR                  // participates in the deterministic fold for its slot
  | NON_CONTRIBUTING_MEMBER      // current and bound, but not selected by the fold
                                 // (e.g. an earlier absolute anchor superseded by a later one)
  | SLOT_ABSTENTION_MEMBER       // the fold abstained; abstention_kind ∈
                                 //   { NO_ANCHOR, AMBIGUOUS_ANCHOR, DELTA_ON_RANGE }

AuthorityTier =
    AGENT
  | TRUSTED_TOOL
  | MANUAL
  | USER
```

`FoldRole` is undefined when `eligibility_role != MATERIALIZABLE`. The original single `FoldRole` enum conflated binding advisories, operator vetoes, source retractions, and actual fold abstentions into one value (`ABSTAINED_SLOT_MEMBER`). Those are different conditions: binding failure is eligibility, ambiguous anchors are fold abstention. They are now separated.

The audit validates combinations rather than collapsing them into one enum.

Valid combinations include:

```text
CURRENT + BINDING_PENDING + NOT_REQUIRED + BINDING_ADVISORY + (fold_role undefined)
CURRENT + BOUND + PROJECTED + MATERIALIZABLE + CONTRIBUTOR
CURRENT + BOUND + PROJECTED + MATERIALIZABLE + NON_CONTRIBUTING_MEMBER
CURRENT + BOUND + NOT_REQUIRED + MATERIALIZABLE + SLOT_ABSTENTION_MEMBER
CURRENT + BOUND + NOT_REQUIRED + RETRACTED + (fold_role undefined)
CURRENT + BOUND + NOT_REQUIRED + OPERATOR_VETOED + (fold_role undefined)
SUPERSEDED + NOT_REQUIRED + (eligibility undefined) + (fold_role undefined)
```

Likely invalid combinations include:

```text
SUPERSEDED + PROJECTION_PENDING                                          // superseded assertions do not project
CURRENT + BINDING_PENDING + PROJECTED                                    // binding-pending assertions create no authoritative View
CURRENT + BINDING_MISMATCH + PROJECTED                                   // binding-mismatch assertions create no authoritative View
CURRENT + BOUND + PROJECTED + BINDING_ADVISORY                           // eligibility and projection contradict
CURRENT + BOUND + NOT_REQUIRED + MATERIALIZABLE + CONTRIBUTOR            // materializable contributors should project
CURRENT + BOUND + PROJECTED + MATERIALIZABLE + (fold_role undefined)     // projection requires a fold role
CURRENT + BOUND + PROJECTED + MATERIALIZABLE + SLOT_ABSTENTION_MEMBER     // abstained slots produce no desired View
```

Invalid combinations are audit failures and produce violations, not silent reclassification.

---

## Conservation equation

At the assertion level:

```text
all_durable_assertions
=
    { a : a.lifecycle = CURRENT }
  + { a : a.lifecycle = SUPERSEDED }
```

At the current-authority level, every current assertion must be classifiable on every field:

```text
all_current_committed_assertions
=
    { a : CURRENT, BOUND,       PROJECTED,          MATERIALIZABLE, CONTRIBUTOR              }
  + { a : CURRENT, BOUND,       PROJECTED,          MATERIALIZABLE, NON_CONTRIBUTING_MEMBER  }
  + { a : CURRENT, BOUND,       NOT_REQUIRED,       MATERIALIZABLE, SLOT_ABSTENTION_MEMBER   }
  + { a : CURRENT, BOUND,       PROJECTION_PENDING, MATERIALIZABLE, *                        }
  + { a : CURRENT, BOUND,       PROJECTION_ERROR,   MATERIALIZABLE, *                        }
  + { a : CURRENT, BINDING_PENDING,  NOT_REQUIRED,  BINDING_ADVISORY, undefined              }
  + { a : CURRENT, BINDING_MISMATCH, NOT_REQUIRED,  BINDING_ADVISORY, undefined              }
  + { a : CURRENT, BOUND,       NOT_REQUIRED,       RETRACTED,       undefined                }
  + { a : CURRENT, BOUND,       NOT_REQUIRED,       OPERATOR_VETOED, undefined                }
```

A committed current assertion that cannot be placed in exactly one row of the above table is a correctness failure.

An assertion placed in two rows simultaneously is also a correctness failure.

Note: the original proposal's `EXPLICITLY_NON_MATERIALIZABLE` is now represented by `eligibility_role ∈ { RETRACTED, OPERATOR_VETOED }` (with a reason field distinguishing the cause), and `VETOED` is similarly split across `RETRACTED` and `OPERATOR_VETOED`. See "Spec gaps identified" below.

---

## Why this matters

A View is a derived projection over durable evidence.

Because the assertion log and the View are stored separately, failures can occur between the two operations.

For example:

```text
1. Assertion is committed.
2. Subject binding succeeds and projection_pending is atomically set.
3. Process crashes before mark_projection_complete.
4. View rebuild never completes.
```

Menhir's `projection_pending` marker addresses this specific case by making the incomplete projection durable and repairable.

The Projection Coverage Audit generalizes the principle along two axes:

* Audit A: every assertion must reach a coherent lifecycle state (`binding_pending`, `projection_pending`, `superseded`, `projected`, etc.).
* Audit B: every materialized View must equal the deterministic fold of the assertions that should feed it (value, contributors, tier, provenance).

> Every gap between durable evidence and derived state must be named, discoverable, and repairable.

---

## Relationship to current ScalarStateView behavior

Existing relevant states include:

### Bound and projected

The assertion is uniquely bound, current, and represented by the rebuilt ScalarStateView. Under the orthogonal-field model this is:

```text
lifecycle=CURRENT, binding_status=BOUND,
projection_status=PROJECTED, fold_role=CONTRIBUTOR or NON_CONTRIBUTING_MEMBER
```

### `binding_pending`

The assertion is durable but cannot yet be uniquely associated with an entity. It remains advisory and creates no authoritative View. Under the orthogonal-field model this is:

```text
lifecycle=CURRENT, binding_status=BINDING_PENDING,
projection_status=NOT_REQUIRED,
eligibility_role=BINDING_ADVISORY, fold_role=undefined
```

This resolves the mutual-exclusivity violation in the original single-enum proposal: `binding_pending` assertions are advisories, so `BINDING_PENDING` and `ADVISORY` were not exclusive. Under the orthogonal-field model, advisory is an `eligibility_role` (`BINDING_ADVISORY`), not a competing lifecycle state or a fold role.

### `projection_pending`

The assertion is bound, but projection completion has not yet been confirmed. The repair process can rebuild the View and then clear the marker. Under the orthogonal-field model this is:

```text
lifecycle=CURRENT, binding_status=BOUND,
projection_status=PROJECTION_PENDING, fold_role=*
```

### Superseded

The assertion remains part of the event log but no longer contributes to current state. Under the orthogonal-field model this is:

```text
lifecycle=SUPERSEDED, binding_status=*,
projection_status=NOT_REQUIRED,
eligibility_role=undefined, fold_role=undefined
```

Both eligibility and fold role are undefined for superseded assertions. The deterministic fold only processes `current_materializable_assertions`, which excludes superseded assertions entirely.

These mechanisms already form most of the accounting model. The audit makes their completeness explicit and adds two things the current states do not cover:

* the `fold_role` distinction between `CONTRIBUTOR` and `NON_CONTRIBUTING_MEMBER` (both are current, bound, and materializable, but only one is selected by the deterministic fold);
* Audit B, which checks that the View's `scalar_contributors` matches the deterministic fold's `desired.contributors`.

---

## Proposed domain model

```text
ProjectionAccountingRecord {
    assertion_id
    assertion_key
    source_key
    subject_uuid
    namespace

    lifecycle           // AssertionLifecycle
    binding_status      // BindingStatus
    projection_status   // ProjectionStatus
    eligibility_role    // EligibilityRole
    fold_role           // FoldRole or undefined when eligibility_role != MATERIALIZABLE
    abstention_kind     // NO_ANCHOR | AMBIGUOUS_ANCHOR | DELTA_ON_RANGE | undefined
    authority_tier      // AuthorityTier

    expected_view_key
    matching_view_ids

    fold_contributor    // bool: assertion appears in desired.contributors of the deterministic fold
                        // (not the actual View's scalar_contributors; that is checked by Audit B)
    reasons             // List<String>: why the projection_status / eligibility_role was chosen
    repairable          // bool
}
```

`UNACCOUNTED` and `MULTIPLY_ACCOUNTED` are not lifecycle states. They are audit-level failure classifications produced when an assertion cannot be placed in exactly one row of the conservation table, or when the projection produces an inconsistent combination.

```text
AuditFailureClassification =
    UNACCOUNTED                // no valid combination applies
  | MULTIPLY_ACCOUNTED         // more than one View materializes the same slot
  | INVALID_COMBINATION        // orthogonal fields form a disallowed combination
  | NAMESPACE_MISMATCH         // assertion.namespace != matching_view.namespace
  | CORRUPT_OR_BYPASSED_WRITE_PATH  // bound current without marker, view, or error
```

---

## Classification rules

Rules are deterministic and evaluated per orthogonal field. The fields are evaluated in the order below. The key ordering principle: **eligibility and fold role are derived from assertions and the deterministic fold only; projection status and parity are derived afterward from the actual Views.** A corrupt View must not affect fold-role assignment.

Field evaluation order:

```text
1. lifecycle
2. binding status
3. eligibility role
4. fold role              (from the deterministic fold, not from the View)
5. projection status      (from the fold's desired View and the actual View)
6. authority tier
7. combination validation
8. audit-level failures   (Audit A) and parity violations (Audit B)
```

### Lifecycle

```text
IF coalesce(assertion.superseded, false)
THEN lifecycle = SUPERSEDED
ELSE lifecycle = CURRENT
```

Menhir creates assertions with `superseded=true`, including same-version disagreements that never become current. `superseded_by` is only added when a previously current assertion is actively replaced, so testing `superseded_by IS NOT NULL` would misclassify non-current assertions that were never the head.

### Binding status

```text
IF coalesce(assertion.binding_pending, false)
THEN binding_status = BINDING_PENDING

ELSE IF assertion.subject_uuid mismatches the head's bound owner
    AND the head is not currently being rebound
THEN binding_status = BINDING_MISMATCH

ELSE
    binding_status = BOUND
```

Current Menhir uses an `unbound:<source_key>` sentinel for pending assertions, not necessarily `NULL` subject UUID. The primary condition is the `binding_pending` flag; identity consistency can be audited separately as `BINDING_MISMATCH`.

### Eligibility role

```text
IF lifecycle = SUPERSEDED
THEN eligibility_role = undefined

ELSE IF binding_status = BINDING_PENDING OR BINDING_MISMATCH
THEN eligibility_role = BINDING_ADVISORY

ELSE IF assertion.source ∈ retracted_sources
THEN eligibility_role = RETRACTED
    reason = "source_retracted"

ELSE IF assertion.explicitly_vetoed
THEN eligibility_role = OPERATOR_VETOED
    reason = "explicit_operator_veto"

ELSE
    eligibility_role = MATERIALIZABLE
```

The audit does not filter by evidence tier. Current ScalarStateView admits fully bound current assertions into the fold and computes the effective tier from the selected contributors. A future actionability policy could impose a minimum tier, but that is not an assertion eligibility rule for this audit. The earlier draft's `evidence_tier < view.minimum_evidence_tier → OPERATOR_VETOED` clause has been removed because it contradicted the fold's admission semantics.

### Fold role

Fold role is derived **exclusively from the deterministic fold**, never from the actual View. Computing the fold against the actual View's `scalar_contributors` would be circular: a corrupt View with wrong contributors would cause Audit A to assign the wrong fold role.

```text
desired := deterministic_fold(current_materializable_assertions(entity, namespace))

IF eligibility_role != MATERIALIZABLE
THEN fold_role = undefined
    // superseded, binding-advisory, retracted, and vetoed assertions
    // have no fold role

ELSE IF assertion.id ∈ desired.contributors(slot_of(assertion))
THEN fold_role = CONTRIBUTOR

ELSE IF assertion.id ∈ desired.abstentions(slot_of(assertion))
THEN fold_role = SLOT_ABSTENTION_MEMBER
     abstention_kind = desired.abstention_reason(assertion.id)
     // abstention_kind ∈ { NO_ANCHOR, AMBIGUOUS_ANCHOR, DELTA_ON_RANGE }

ELSE IF assertion.id ∈ desired.slot_members(slot_of(assertion))
    AND assertion.id ∉ desired.contributors(slot_of(assertion))
    AND assertion.id ∉ desired.abstentions(slot_of(assertion))
THEN fold_role = NON_CONTRIBUTING_MEMBER
    // e.g. an earlier absolute anchor superseded by a later one in the same slot

ELSE
    fold_role = undefined
    reason = "materializable_assertion_not_in_any_fold_slot"
    // indicates a fold bug or a slot-membership inconsistency
```

The fold's output provides:
- `contributors`: assertion IDs the fold selected for each slot
- `abstentions`: assertion IDs the fold deliberately did not use, with abstention reasons
- `slot_members`: all materializable assertions that fell into each slot

The actual View is not consulted at this stage.

### Audit-enriched fold output

The current fold API returns folded states with `contributor_ids` per slot, and abstentions identify the slot and reason — not individual assertion IDs. It does not expose a formal `slot_members` collection.

The auditor enriches the fold result from the same grouped input:

```text
slot_members        = input assertions grouped by slot
abstention_members  = every member of an abstained slot
                     (each tagged with abstention_kind from the fold's abstention reason)
contributors        = FoldedScalarState.contributor_ids
non_contributors    = slot_members - contributors - abstention_members
```

This enrichment is computed by the auditor, not by the fold. The fold API does not need to change. The audit-enriched result is what `fold_role` classification operates on.

### Projection status

Projection status is derived after fold role. It compares the fold's desired output against the actual View state.

```text
IF lifecycle = SUPERSEDED
THEN projection_status = NOT_REQUIRED

ELSE IF binding_status = BINDING_PENDING OR BINDING_MISMATCH
THEN projection_status = NOT_REQUIRED

ELSE IF eligibility_role ∈ { RETRACTED, OPERATOR_VETOED }
THEN projection_status = NOT_REQUIRED

ELSE IF fold_role = SLOT_ABSTENTION_MEMBER
THEN projection_status = NOT_REQUIRED
    // the fold abstained; no desired View should exist for this slot.
    IF matching View exists:
        AuditFailureClassification += INVALID_COMBINATION
        // Audit B will also report ORPHANED_VIEW for this slot

ELSE IF fold_role = undefined
    AND eligibility_role = MATERIALIZABLE
THEN projection_status = PROJECTION_ERROR
    reason = "materializable_assertion_not_in_any_fold_slot"

ELSE IF assertion.projection_pending_marker IS SET
    AND matching View exists
    AND mark_projection_complete was not called
THEN projection_status = PROJECTION_PENDING
    // View exists but the marker was not cleared; rebuild acknowledgement is incomplete

ELSE IF assertion.projection_pending_marker IS SET
    AND no matching View exists
THEN projection_status = PROJECTION_PENDING

ELSE IF durable_projection_error_exists(assertion)
THEN projection_status = PROJECTION_ERROR

ELSE IF fold produced a desired View for this slot
    AND exactly one matching current View exists
    AND that View was built from an acknowledged projection of this assertion
THEN projection_status = PROJECTED

ELSE IF fold produced a desired View for this slot
    AND no matching View exists
    AND no projection_pending marker
    AND no durable_projection_error
THEN AuditFailureClassification = CORRUPT_OR_BYPASSED_WRITE_PATH

ELSE
    projection_status = PROJECTION_ERROR
    reason = "bound_current_without_pending_marker_or_view"
```

### Authority tier

```text
authority_tier = assertion.evidence_tier
    // AGENT | TRUSTED_TOOL | MANUAL | USER
```

The audit does not filter by evidence tier. The fold decides the View's effective tier from its contributors; the audit only records what was used.

### Combination validation

After all fields are set, the combination is checked:

```text
invalid_combinations =
    (SUPERSEDED, *, PROJECTION_PENDING, *, *)
    (CURRENT, BINDING_PENDING, PROJECTED, *, *)
    (CURRENT, BINDING_MISMATCH, PROJECTED, *, *)
    (CURRENT, BOUND, PROJECTED, BINDING_ADVISORY, *)
    (CURRENT, BOUND, PROJECTED, RETRACTED, *)
    (CURRENT, BOUND, PROJECTED, OPERATOR_VETOED, *)
    (CURRENT, BOUND, NOT_REQUIRED, MATERIALIZABLE, CONTRIBUTOR)
    (CURRENT, BOUND, PROJECTED, MATERIALIZABLE, undefined)
    (CURRENT, BOUND, PROJECTED, MATERIALIZABLE, SLOT_ABSTENTION_MEMBER)
    // abstained slots produce no desired View; a PROJECTED View for
    // an abstained slot is an Audit B ORPHANED_VIEW violation and an
    // Audit A INVALID_COMBINATION

IF combination ∈ invalid_combinations
THEN AuditFailureClassification = INVALID_COMBINATION
```

### Audit-level failures

```text
IF lifecycle = CURRENT
    AND binding_status = BOUND
    AND eligibility_role = MATERIALIZABLE
    AND fold_role ∈ { CONTRIBUTOR, NON_CONTRIBUTING_MEMBER }
    AND fold produced a desired View for this slot
    AND no matching View exists
    AND no projection_pending marker
    AND no durable_projection_error
THEN AuditFailureClassification = CORRUPT_OR_BYPASSED_WRITE_PATH

IF more than one current View materializes the same slot
THEN AuditFailureClassification = MULTIPLY_ACCOUNTED
    // applied to the slot, not to individual assertions

IF matching_view.namespace != assertion.namespace
THEN AuditFailureClassification = NAMESPACE_MISMATCH
```

Audit B (fold projection parity) runs after Audit A. It compares the fold's desired output against the actual Views and reports `ParityViolation`s. See "Audit B — Fold projection parity" below.

---

## Audit result

```text
ProjectionCoverageReport {
    namespace
    scanned_assertions

    // counts per (lifecycle, binding_status, projection_status, fold_role) tuple
    lifecycle_counts: Map<AssertionLifecycle, Int>
    binding_counts: Map<BindingStatus, Int>
    projection_counts: Map<ProjectionStatus, Int>
    fold_role_counts: Map<FoldRole, Int>

    // audit-level failure counts
    unaccounted
    multiply_accounted
    invalid_combinations
    namespace_mismatches

    violations: List<ProjectionCoverageViolation>
}
```

Violation:

```text
ProjectionCoverageViolation {
    assertion_id
    source_key
    namespace
    expected_view_key
    observed_view_ids
    violation_kind      // UNACCOUNTED | MULTIPLY_ACCOUNTED | INVALID_COMBINATION
                        // | NAMESPACE_MISMATCH | CORRUPT_OR_BYPASSED_WRITE_PATH
    fields              // the (lifecycle, binding_status, projection_status, fold_role) tuple
    diagnostic
    recommended_repair
}
```

---

## Core audit algorithm

```python
def audit_assertion_lifecycle(
    assertions: Iterable[AssertionSnapshot],
    views: Iterable[ViewSnapshot],
) -> ProjectionCoverageReport:
    view_index = index_views_by_expected_key(views)
    records = []

    for assertion in assertions:
        matches = view_index.get(assertion.expected_view_key, [])
        record = classify_assertion_fields(assertion, matches)
        record = validate_combination(record)
        records.append(record)

    return build_coverage_report(records)
```

The production implementation should operate against repository queries rather than loading the full store into memory, but the pure form gives tests a deterministic oracle.

`classify_assertion_fields` sets the orthogonal fields per the classification rules. `validate_combination` checks the tuple against the valid-combination table and, if invalid, sets an `AuditFailureClassification`. Audit B (fold projection parity) is a separate function that compares the deterministic fold of current materializable assertions against the actual Views.

---

## Required invariants

### Coherent combination

```text
For every durable assertion:
    the (lifecycle, binding_status, projection_status, fold_role, authority_tier)
    tuple appears in the valid-combination table
```

### Binding-pending exclusion

```text
binding_status = BINDING_PENDING
    implies projection_status = NOT_REQUIRED
    implies no authoritative View was produced from this assertion
```

### Projection completion

```text
lifecycle = CURRENT
    AND binding_status = BOUND
    AND projection_status = PROJECTED
    AND fold_role = CONTRIBUTOR
    implies exactly one matching current View
    AND assertion.id ∈ view.scalar_contributors
```

### Fold-parity (Audit B)

```text
For each (entity, namespace):
    deterministic_fold(current_materializable_assertions)
    ==
    current_materialized_views
```

Compared on:

```text
slot keys
slot values
valid_at
scalar_contributors
effective_tier
episode_provenance
abstentions
```

See "Audit B — Fold projection parity" below.

### Namespace fidelity

```text
assertion.namespace == materialized_view.namespace
```

A View in the default namespace does not satisfy an assertion belonging to another namespace.

### Slot uniqueness

For each scalar slot:

```text
(entity, attribute, scope, value_kind, unit, namespace)
```

there must be at most one authoritative current View.

### Supersession exclusion

A superseded assertion must not independently create current authority.

### Repair discoverability

Every repairable incomplete state must appear in a bounded repair query.

```text
binding_status = BINDING_PENDING
projection_status = PROJECTION_PENDING
projection_status = PROJECTION_ERROR where retryable
```

---

## Repair behavior

### Binding pending

`binding_status = BINDING_PENDING`

Attempt entity resolution against the episode's current linked entities.

```text
unique match:
    adopt binding
    set projection_pending  (atomic with binding adoption)
    rebuild View

zero or multiple matches:
    remain BINDING_PENDING
```

### Projection pending

`projection_status = PROJECTION_PENDING`

Do not re-run perception or entity resolution.

```text
rebuild View from durable assertion log
clear projection_pending only after successful rebuild
```

### Projection error

`projection_status = PROJECTION_ERROR`

Retry according to a bounded, fair schedule.

The error record must remain until the rebuild succeeds or an operator marks it non-retryable.

### Unaccounted

`AuditFailureClassification = UNACCOUNTED`

Treat as a high-severity invariant violation.

Possible responses:

1. Attempt a deterministic rebuild.
2. Re-run the audit.
3. If still unaccounted, retain the violation and alert.
4. Do not silently classify it as advisory.

### Corrupt or bypassed write path

`AuditFailureClassification = CORRUPT_OR_BYPASSED_WRITE_PATH`

A bound current assertion with no View, no `projection_pending` marker, and no error record should not occur if the atomic write path is intact. Treat as a repository regression, not a routine interruption.

Possible responses:

1. Do not silently add the marker and continue.
2. Attempt a deterministic rebuild to restore service.
3. Report that the invariant-preserving write path was bypassed or malfunctioning.
4. Alert for investigation of the write path.

### Multiply accounted

`AuditFailureClassification = MULTIPLY_ACCOUNTED`

Fail closed.

Do not choose one View arbitrarily.

Record all conflicting View IDs and require deterministic reconciliation or rebuild.

### Invalid combination

`AuditFailureClassification = INVALID_COMBINATION`

The orthogonal fields form a disallowed combination (e.g. `SUPERSEDED + PROJECTION_PENDING`). Do not pick one field as authoritative.

Record the full tuple and flag for investigation. The combination itself is the diagnostic; repair depends on which field is wrong.

### Fold-parity violations (Audit B)

`ParityViolation(kind = ...)`

Repair by rebuilding the affected View from the deterministic fold of current materializable assertions:

```text
for each ParityViolation:
    rebuild View for slot from deterministic_fold
    re-run Audit B for that slot
    if parity holds, clear the violation
    if parity still fails, escalate to operator
```

A rebuild that does not achieve parity indicates either a fold bug, a repository corruption, or a write-path bypass. These are not auto-repairable.

---

## Fairness and bounded work

The audit and repair system must preserve the current repair principles:

* one global bound where global work is requested;
* explicit namespace allowlists;
* no multiplication of the limit by namespace count;
* unattempted rows first;
* least-recently attempted rows next;
* stable tie-breaking;
* per-row failure isolation;
* eventual coverage of the backlog.

A repeatedly failing early row must not starve later repairable rows.

Repairable rows are those with:

```text
binding_status = BINDING_PENDING
OR projection_status = PROJECTION_PENDING
OR projection_status = PROJECTION_ERROR where retryable
OR AuditFailureClassification = UNACCOUNTED
OR AuditFailureClassification = MULTIPLY_ACCOUNTED
OR ParityViolation of any kind
```

`CORRUPT_OR_BYPASSED_WRITE_PATH` and `INVALID_COMBINATION` are not auto-repairable rows. They are reported and alerted, but they do not consume repair budget.

---

## Crash safety

The audit itself should be read-only.

Repairs should use durable transitions.

For example:

```text
(CURRENT, BINDING_PENDING, NOT_REQUIRED, BINDING_ADVISORY, undefined)
    ↓ atomic binding adoption (sets projection_pending in the same statement)
(CURRENT, BOUND, PROJECTION_PENDING, MATERIALIZABLE, undefined)
    ↓ successful rebuild + mark_projection_complete
(CURRENT, BOUND, PROJECTED, MATERIALIZABLE, CONTRIBUTOR or NON_CONTRIBUTING_MEMBER)
```

A crash at any point leaves the assertion in a discoverable state.

No repair should clear a pending marker before the work it protects has completed.

---

## Observability

Recommended counters:

```text
projection_audit_scanned_total
projection_lifecycle_current_total
projection_lifecycle_superseded_total
projection_binding_bound_total
projection_binding_pending_total
projection_binding_mismatch_total
projection_projection_projected_total
projection_projection_pending_total
projection_projection_error_total
projection_projection_not_required_total
projection_fold_contributor_total
projection_fold_non_contributing_total
projection_fold_slot_abstention_total
projection_eligibility_materializable_total
projection_eligibility_binding_advisory_total
projection_eligibility_retracted_total
projection_eligibility_operator_vetoed_total
projection_unaccounted_total
projection_multiply_accounted_total
projection_invalid_combination_total
projection_namespace_mismatch_total
projection_corrupt_write_path_total
projection_parity_value_mismatch_total
projection_parity_contributor_mismatch_total
projection_parity_orphaned_view_total
projection_parity_missing_view_total
```

Recommended logs should include:

```text
assertion_id
source_key
subject_uuid
namespace
expected_view_key
fields              // the orthogonal-field tuple
violation_kind      // if any
repair action
```

Raw content from the remembered episode should not be required in normal audit logs.

---

## Offline tests

### Bound assertion with matching View

Expected:

```text
lifecycle=CURRENT, binding_status=BOUND,
projection_status=PROJECTED,
eligibility_role=MATERIALIZABLE, fold_role=CONTRIBUTOR
```

### Binding-pending assertion without View

Expected:

```text
lifecycle=CURRENT, binding_status=BINDING_PENDING,
projection_status=NOT_REQUIRED,
eligibility_role=BINDING_ADVISORY, fold_role=undefined
```

### Binding-pending assertion with authoritative View

Expected:

```text
INVALID_COMBINATION
    (CURRENT, BINDING_PENDING, PROJECTED, *, *) is disallowed
```

### Projection-pending assertion without View

Expected:

```text
lifecycle=CURRENT, binding_status=BOUND,
projection_status=PROJECTION_PENDING,
eligibility_role=MATERIALIZABLE, fold_role=undefined
```

### Projection-pending assertion with View

Still expected:

```text
lifecycle=CURRENT, binding_status=BOUND,
projection_status=PROJECTION_PENDING,
eligibility_role=MATERIALIZABLE, fold_role=CONTRIBUTOR or NON_CONTRIBUTING_MEMBER
```

until successful rebuild acknowledgement clears the marker.

### Bound current assertion without View or pending marker

Expected:

```text
AuditFailureClassification = CORRUPT_OR_BYPASSED_WRITE_PATH
    (the atomic write path should have set projection_pending)
```

If the durable write path is confirmed intact and the marker is genuinely absent, this is `UNACCOUNTED`.

### Superseded assertion without View

Expected:

```text
lifecycle=SUPERSEDED, binding_status=*,
projection_status=NOT_REQUIRED, fold_role=NON_CONTRIBUTING_MEMBER
```

### Assertion in tenant A with matching-key View in default namespace

Expected:

```text
AuditFailureClassification = NAMESPACE_MISMATCH
```

plus namespace mismatch diagnostic.

### Two current Views for the same scalar slot

Expected:

```text
AuditFailureClassification = MULTIPLY_ACCOUNTED
    (applied to the slot, not to individual assertions)
```

### One row causes an exception

Expected:

* the row becomes an audit error;
* later rows are still classified;
* the full audit does not abort.

### Fold-parity (Audit B) tests

#### View with correct key but wrong value

Expected:

```text
ParityViolation(kind = VALUE_MISMATCH)
```

#### View with correct value but wrong scalar_contributors

Expected:

```text
ParityViolation(kind = CONTRIBUTOR_SET_MISMATCH)
```

#### View whose slot no longer appears in the deterministic fold

Expected:

```text
ParityViolation(kind = ORPHANED_VIEW)
```

#### Slot that appears in the fold but has no View

Expected:

```text
ParityViolation(kind = MISSING_VIEW)
```

---

## Live Neo4j tests

A final production gate should test:

1. Commit a bound assertion; the atomic write sets `projection_pending`. Interrupt before `mark_projection_complete`.
2. Confirm the audit classifies it as `PROJECTION_PENDING` (with `fold_role` assigned by the deterministic fold; the View's existence affects only `projection_status`).
3. Run repair.
4. Confirm the View is rebuilt in the assertion's namespace.
5. Confirm the pending marker clears only afterward.
6. Remove the matching View manually.
7. Confirm Audit B reports a `MISSING_VIEW` parity violation for the slot.
8. Rebuild and confirm the violation disappears.
9. Create a duplicate current View and confirm `MULTIPLY_ACCOUNTED` on the slot.
10. Confirm no automatic arbitrary winner is selected.
11. Manually corrupt a View's `scalar_contributors` (remove a contributing assertion id). Confirm Audit B reports `CONTRIBUTOR_SET_MISMATCH` even though the value is unchanged.
12. Manually set a View's value to a wrong number while keeping `scalar_contributors` correct. Confirm Audit B reports `VALUE_MISMATCH`.
13. Insert a bound current assertion with no View, no `projection_pending` marker, and no error record. Confirm the audit classifies this as `CORRUPT_OR_BYPASSED_WRITE_PATH`, not as a routine `UNACCOUNTED` row.

---

## Scope boundaries

This proposal does not:

* calculate confidence;
* judge whether the assertion is semantically true;
* replace the deterministic scalar fold;
* introduce an LLM repair step;
* delete superseded assertions;
* allow advisory assertions to create authority;
* alter source identity;
* cross namespaces during repair.

It answers two narrower questions:

> Audit A: Has every durable assertion reached an explicit and correct lifecycle state?
>
> Audit B: Does every materialized View equal the deterministic fold of the assertions that should feed it?

---

## Implementation recommendation

Build this first as a read-only service and test oracle.

Suggested seam for Audit A (assertion lifecycle coverage):

```text
ScalarStateService.audit_assertion_lifecycle(...)
```

or:

```text
TypedAssertionRepository.assertion_lifecycle_rows(...)
ProjectionLifecycleAuditor.audit(...)
```

Suggested seam for Audit B (fold projection parity):

```text
ScalarStateService.audit_fold_parity(...)
```

or:

```text
ScalarStateRepository.current_materializable_assertions(entity, namespace)
DeterministicScalarFold.fold(...)    // reference implementation
FoldParityAuditor.audit(...)
```

The repository should retrieve the required durable state. Pure auditors should classify it. Keeping classification and parity logic outside Cypher makes the rules easier to test.

Initial production use can be:

* maintenance diagnostics;
* CI or live-Neo4j integration tests;
* operator health output;
* metrics and alerts.

Automatic repair can continue through the existing repair paths.

---

## Adversarial histories

The offline tests cover single-state classification. The following histories stress the audit across transitions and boundary conditions that single-state tests do not reach.

### H1. Crash after the atomic projection-pending write, before projection completion

The original draft of this history described a crash between binding success and the writing of the `projection_pending` marker. Review identified that as inaccurate for the current repository: `projection_pending` is set in the same Cypher statement that performs fresh binding or pending-to-bound adoption. That write path is atomic by design, precisely to eliminate the gap the original history described.

A state in which an assertion is bound and current but has neither a matching View, a `projection_pending` marker, nor a durable projection error is therefore not an expected routine interruption. It is:

```text
AuditFailureClassification = CORRUPT_OR_BYPASSED_WRITE_PATH
```

A deterministic rebuild may restore service, but the audit must also report that an invariant-preserving write path was bypassed or malfunctioned. Automatically adding the marker and continuing could conceal a serious repository regression.

The valid crash history is:

```text
1. Assertion is bound and projection_pending is atomically set.
2. View rebuild succeeds.
3. Process crashes before mark_projection_complete is called.
```

In that state both the View and `projection_pending=true` may exist. The audit classifies:

```text
lifecycle          = CURRENT
binding_status     = BOUND
eligibility_role   = MATERIALIZABLE
fold_role          = (role assigned by deterministic_fold(...))
                     // CONTRIBUTOR, NON_CONTRIBUTING_MEMBER, or SLOT_ABSTENTION_MEMBER
                     // derived from the fold, NOT from the View's scalar_contributors
projection_status  = PROJECTION_PENDING
reason             = "view_present_but_completion_not_marked"
```

Repair safely rebuilds again and then clears the marker. No data is lost; no invariant is violated.

A different valid crash history is:

```text
1. Assertion is bound and projection_pending is atomically set.
2. Process crashes before View rebuild begins.
```

Audit classifies:

```text
lifecycle          = CURRENT
binding_status     = BOUND
eligibility_role   = MATERIALIZABLE
fold_role          = (role assigned by deterministic_fold(...))
                     // the fold runs on current_materializable_assertions,
                     // which includes this assertion regardless of View existence
projection_status  = PROJECTION_PENDING
```

The deterministic fold does not require a View to exist. It processes current materializable assertions directly. The View's existence only affects `projection_status`, not `fold_role`.

Repair rebuilds the View and then clears the marker.

### H2. Source retraction

Sequence:

```text
1. Source S makes assertion A. A is committed and projected.
2. Source S is discredited. All assertions from S are retracted.
```

Audit behavior:

A retracted assertion should not remain `PROJECTED`. Under the orthogonal-field model, retraction is an eligibility role, not a fold role:

```text
eligibility_role = RETRACTED
reason           = SOURCE_RETRACTED
projection_status = NOT_REQUIRED
fold_role         = undefined
```

The matching View, if any, should be invalidated through the existing supersession or rebuild path, not through the audit itself. Audit B will detect the resulting `CONTRIBUTOR_SET_MISMATCH` or `ORPHANED_VIEW` if the View is not rebuilt.

### H3. Orphaned View

Sequence:

```text
1. Assertion A is committed and projected. View V exists for its slot
   and V.scalar_contributors includes A.id.
2. Assertion A is superseded by assertion B.
3. View V is not rebuilt. View V now references a superseded contributor.
```

Audit behavior (Audit A):

Assertion A is `SUPERSEDED + NOT_REQUIRED + (eligibility undefined) + (fold_role undefined)` (correct).
Assertion B is `CURRENT + BOUND + PROJECTED + MATERIALIZABLE + CONTRIBUTOR` or `CURRENT + BOUND + PROJECTION_PENDING + MATERIALIZABLE + (fold_role undefined)` (correct).

Audit behavior (Audit B):

View V is an orphan: a current View whose `scalar_contributors` no longer matches the deterministic fold of current materializable assertions for its slot. Audit B detects this by recomputing the fold and comparing contributor sets:

```text
desired_scalar_contributors(slot) != V.scalar_contributors
```

The violation is recorded against the View, not against any individual assertion. See "Audit B — Fold projection parity" below.

This case is the inverse of `UNACCOUNTED`: a View with no current backing contributor, rather than a current assertion with no View. Audit B closes that gap.

### H4. Temporal coverage gap

Sequence:

```text
1. Assertion A asserts X = 5 at t=0. A is projected.
2. No further assertions about X are made.
3. At t=100, the View still reports X = 5.
```

Audit behavior:

Assertion A is `CURRENT + BOUND + PROJECTED + CONTRIBUTOR` (correct). The audit reports no violation.

The audit does not cover temporal coverage. A value that has not been re-asserted for a long window is still `PROJECTED` by the current classification rules.

This is a scope boundary, not a defect. Temporal coverage is a separate audit:

```text
For each current View:
    IF time_since_last_supporting_assertion > coverage_window(attribute)
    THEN STALE_COVERAGE
```

That audit is out of scope for this proposal. It is noted here so that operators do not assume the projection coverage audit catches staleness.

### H5. Binding pending that later resolves

Sequence:

```text
1. Assertion A is committed but cannot bind to a unique entity.
   classification = BINDING_PENDING.
2. Entity resolution is repaired. A binds uniquely.
3. View rebuild is scheduled.
4. View rebuild completes.
```

Audit behavior across transitions:

```text
t1: (CURRENT, BINDING_PENDING, NOT_REQUIRED, BINDING_ADVISORY, undefined)
t2: (CURRENT, BOUND, PROJECTION_PENDING, MATERIALIZABLE, undefined)
    // binding adopted, marker atomically set
t3: (CURRENT, BOUND, PROJECTED, MATERIALIZABLE, CONTRIBUTOR or NON_CONTRIBUTING_MEMBER)
    // rebuild acknowledged, marker cleared
```

Each transition is a durable lifecycle event. A crash between t2 and t3 leaves the assertion in `PROJECTION_PENDING`, which is discoverable by the audit on restart.

### H6. Namespace leak through default-namespace View

Sequence:

```text
1. Assertion A is committed in namespace tenant_A.
2. A View V exists with a matching expected_view_key but in the default namespace.
3. No View exists in namespace tenant_A.
```

Audit behavior:

```text
AuditFailureClassification = NAMESPACE_MISMATCH
diagnostic = namespace_mismatch
observed_view_ids = [V.view_id]
```

The audit does not satisfy the assertion with a View from a different namespace. The namespace mismatch is recorded in the violation diagnostic so that repair can rebuild in the correct namespace rather than reclassifying the assertion.

---

## Audit B — Fold projection parity

Audit A checks that every assertion reaches a coherent lifecycle state. Audit B checks that the materialized Views equal the deterministic fold of the current materializable assertions.

Audit B is necessary because Audit A's `PROJECTED + CONTRIBUTOR` classification only confirms that a View exists and lists the assertion as a contributor. It does not confirm that the View's value, anchor, deltas, or contributor set match what the deterministic fold would produce from the same inputs.

### Comparison key

Audit B compares per `(entity, namespace)`:

```text
desired = deterministic_fold(current_materializable_assertions(entity, namespace))
actual  = current_materialized_views(entity, namespace)
```

`current_materializable_assertions` must be defined independently of the View being audited. The original draft of this proposal used `fold_role ∈ {CONTRIBUTOR, NON_CONTRIBUTING_MEMBER}` as an admission condition, but `fold_role` is itself determined by comparing the assertion to the View or fold. A missing or corrupt View would therefore affect which assertions are selected to compute the desired View, which is circular.

The correct input mirrors the existing repository fold entry point:

```text
current_materializable_assertions(entity, namespace) =
    { a : a.superseded = false
      AND a.binding_pending = false
      AND a.namespace = namespace
      AND eligibility_role(a) = MATERIALIZABLE }
```

`eligibility_role` is determined by the retraction/veto registry (see "Eligibility role" under Classification rules), not by a persisted `materialization_policy` field on the assertion. The earlier draft's `materialization_policy != ADVISORY_ONLY` filter has been removed because it does not correspond to a field in the current repository; the fold entry point filters by supersession and binding state, and the audit additionally checks the retraction/veto registry for eligibility.

No reference to `fold_role`, `scalar_contributors`, or any View state.

### Compared fields

For each slot in the desired fold:

```text
slot_key                // (entity, attribute, scope, value_kind, unit, namespace)
value                   // folded value
valid_at                // folded valid_at
scalar_contributors     // exact assertion IDs the fold selected
effective_tier          // fold-derived tier
episode_provenance      // episode UUIDs the contributors came from
```

Abstentions are intentionally excluded from strict View parity in the first implementation (see "Abstention handling" below).

### Parity check

```text
for each slot s in desired:
    V := actual.view_for_slot(s.slot_key)
    if V is None:
        record ParityViolation(kind = MISSING_VIEW, slot = s.slot_key)
    else:
        if V.value != s.value:
            record ParityViolation(kind = VALUE_MISMATCH, slot = s.slot_key,
                                   desired = s.value, actual = V.value)
        if V.valid_at != s.valid_at:
            record ParityViolation(kind = VALID_AT_MISMATCH, ...)
        if set(V.scalar_contributors) != set(s.scalar_contributors):
            record ParityViolation(kind = CONTRIBUTOR_SET_MISMATCH,
                                   desired = s.scalar_contributors,
                                   actual = V.scalar_contributors)
        if V.effective_tier != s.effective_tier:
            record ParityViolation(kind = TIER_MISMATCH, ...)
        if set(V.episode_provenance) != set(s.episode_provenance):
            record ParityViolation(kind = PROVENANCE_MISMATCH, ...)

for each View V in actual:
    if V.slot_key not in desired.slots:
        record ParityViolation(kind = ORPHANED_VIEW, view = V.view_id)
```

### Abstention handling

Current `ScalarStateView` writes:

```text
scalar_contributors
scalar_effective_tier
scalar_anchor_value
scalar_delta_total
episode UUIDs
```

Fold abstentions are returned by the fold separately and are **not persisted on the View**. Strict parity over abstentions cannot currently be checked.

Two options:

1. Remove abstentions from strict View parity. Compare them only as an audit diagnostic: if the fold's abstentions differ from a previously recorded set, emit a diagnostic, not a violation.
2. Add durable abstention metadata to View projection in a later change.

For the first implementation, abstentions are an audit diagnostic, not a required View field. The `ABSTENTION_MISMATCH` parity kind is removed; abstention mismatches are recorded as:

```text
AbstentionDiagnostic {
    slot_key
    desired_abstentions
    observed_abstentions   // if recorded elsewhere; otherwise None
}
```

These do not fail Audit B parity, but they are surfaced for operator inspection.

### Why the contributor-set comparison matters

Consider:

```text
A1: Alice owns 2 bikes, valid January 1   (absolute anchor)
A2: Alice owns 4 bikes, valid February 1  (absolute anchor, later)
A3: Alice owns +1 bike, valid March 1     (delta on A2)
```

The deterministic fold produces:

```text
value = 5
scalar_contributors = {A2, A3}
abstentions = {A1}
```

A materialized View with `value = 5` but `scalar_contributors = {A1, A2, A3}` passes a value-only check but fails the contributor-set check. The View used the wrong anchor (A1 instead of A2) and arrived at the same value by accident. Audit B catches this.

A materialized View with `value = 4` and `scalar_contributors = {A2}` passes the contributor-set check for A2 but fails the value check (it missed the A3 delta). Audit B catches this.

### Determinism requirement

Audit B must use the same fold implementation that the projection path uses, or a reference implementation that is provably equivalent. If the audit uses a different fold, parity violations may reflect fold-implementation drift rather than projection drift, which is a different (and important) class of bug but not the one Audit B is designed to catch.

### Failure modes specific to Audit B

- The fold implementation used by the audit diverges from the projection path's fold. Every View appears mismatched.
- The fold depends on time or wall-clock state. Two runs of the audit produce different desired states.
- The fold depends on a stale entity index. The desired state includes assertions that no longer bind to this entity.
- The View's `scalar_contributors` field is not durably written or is written lazily. The audit reports false `CONTRIBUTOR_SET_MISMATCH` violations.

### Repair

Audit B violations are not repaired by re-running perception. They are repaired by rebuilding the affected View from the current materializable assertions:

```text
for each ParityViolation:
    rebuild View for slot from deterministic_fold
    re-run Audit B for that slot
    if parity holds, clear the violation
    if parity still fails, escalate to operator
```

A rebuild that does not achieve parity indicates either a fold bug, a repository corruption, or a write-path bypass. These are not auto-repairable.

---

## Spec gaps identified

### VETOED representation

The original proposal listed `VETOED` as a classification enum value with no rule. Under the orthogonal-field correction, veto is not a separate state. It is represented as an eligibility role:

```text
eligibility_role ∈ { RETRACTED, OPERATOR_VETOED }
reason           ∈ { SOURCE_RETRACTED, EXPLICIT_OPERATOR_VETO }
```

`RETRACTED` covers source retraction. `OPERATOR_VETOED` covers explicit operator veto. Evidence-tier ineligibility is intentionally **not** an eligibility role: current ScalarStateView admits fully bound current assertions into the fold regardless of tier and computes the effective tier from the selected contributors. A future actionability policy could impose a minimum tier, but that is a consumer-side concern, not an assertion eligibility rule for this audit.

The reason field distinguishes the two veto causes because their repair paths differ:

```text
SOURCE_RETRACTED         → no repair; the veto is permanent
EXPLICIT_OPERATOR_VETO   → operator action only
```

The audit does not filter by evidence tier on its own. The fold decides the View's effective tier from its contributors; the audit only records that an assertion was ineligible and why.

This split resolves the conflation flagged in review: the original `ABSTAINED_SLOT_MEMBER` value was used for binding advisories, operator vetoes, source retractions, and actual fold abstentions. Those are now separated — binding advisories are `eligibility_role = BINDING_ADVISORY`, retractions and vetoes are `RETRACTED` and `OPERATOR_VETOED`, and actual fold abstentions are `fold_role = SLOT_ABSTENTION_MEMBER` with an `abstention_kind ∈ { NO_ANCHOR, AMBIGUOUS_ANCHOR, DELTA_ON_RANGE }`.

### Orphaned-View detection

The original proposal iterated assertions only. Under the two-audit reframe, orphaned-View detection is handled by Audit B:

```text
for each current View V:
    if V.slot_key not in deterministic_fold(current_materializable_assertions).slots:
        record ParityViolation(kind = ORPHANED_VIEW, view = V.view_id)
```

This closes the inverse of `UNACCOUNTED`. A current View that no longer corresponds to any desired slot is detected and reported.

An orphaned View should not be silently deleted. It should be recorded as a `ParityViolation` with `recommended_repair = rebuild_or_supersede`, and the existing supersession path should resolve it.

---

## Decision

Research proposal — requires fold-parity revision before implementation.

The original single-enum, assertion-to-View-existence framing is insufficient because ScalarStateView is a fold over many assertions, not a one-assertion-to-one-View projection. The two-audit reframe corrects this:

* Audit A keeps the assertion-lifecycle coverage invariant.
* Audit B adds the fold-parity invariant.

Both are needed. Audit A alone misses Views with the correct key but stale contents. Audit B alone misses binding-pending and projection-pending assertions that have not yet entered the fold.

The orthogonal-field correction resolves the mutual-exclusivity violation that the original single enum introduced (`binding_pending` assertions are advisories, so `BINDING_PENDING` and `ADVISORY` were not exclusive).

Once Audit B is implemented against a reference fold and the valid-combination table is encoded in tests, this proposal is ready for near-term implementation as a correctness and observability layer.
