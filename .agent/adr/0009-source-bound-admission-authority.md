# ADR 0009 — Source-Bound Admission Authority

- **Status:** ACCEPTED as the core contract (2026-09-21). Domain adoption, grant issuance, and
  system-wide rollout remain open owner decisions.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/memory-governance.md`, `.agent/data_models.md`,
  `src/menhir/domain/admission.py`, `src/menhir/infrastructure/admission_source.py`

## Context

A claim can ask to be treated as authoritative, but that request is part of the untrusted claim.
Neither plausible content nor a high requested label proves the sources can support it. Multi-source
claims make this harder: one strong source must not hide a weaker source, and a domain-specific
policy must not quietly replace the comparison rules used at the admission boundary.

Menhir already contains a generic source-bound admission core and a TurnEvidence source adapter,
but the contract was not recorded as an architecture decision. The core is not yet wired through
every writer, and the broader write-side constitution described in memory governance remains
unfinished.

## Decision drivers

- Untrusted payloads must not mint their own authority.
- Every durable source behind a claim must participate in its admission ceiling.
- Different domains need different authority vocabularies and may not have one total order.
- Domain policy may reject or constrain a claim, but cannot promote it beyond its evidence.
- Missing, duplicate, mismatched, unknown, or incomparable inputs must fail closed before policy.
- Admission decisions need deterministic, reconstructible receipts that do not copy source content.

## Decision

Menhir separates **requested authority**, **source-granted authority**, and **domain policy**:

```text
untrusted request
      |
complete source set + exact trusted grants -> weakest ingress ceiling
      |
domain policy may reject or lower
      |
effective authority + immutable receipt
```

The following invariants apply to every adopter of the generic admission boundary:

1. **Requested authority is untrusted.** A request stronger than the ingress ceiling is clamped and
   recorded as an attempted promotion; it never raises the result.
2. **Grants bind exact durable sources.** Admission requires a non-empty, duplicate-free source set,
   one grant for each exact `(source_id, source_kind)` key, no extra grants, and unique grant IDs.
3. **The weakest source caps the claim.** For a comparable multi-source set, the weakest grant is
   the ingress ceiling. One stronger source cannot launder the others.
4. **Authority semantics are trusted domain configuration.** The generic core owns no universal
   hierarchy. A domain supplies its registered comparison semantics; neither claim payload nor a
   per-decision policy response may replace them.
5. **Incomparability fails closed.** Incomparable source ceilings, requested authority, or an
   applicable policy ceiling produce rejection rather than an arbitrary ordering.
6. **Policy can only constrain.** A domain policy may reject the claim or lower its effective
   authority. An attempted policy promotion above the ingress ceiling is recorded and ignored.
7. **Admission emits a deterministic receipt.** The receipt contains stable ordered provenance,
   grants, policy identity/version, ceilings, promotion indicators, outcome, and reason. It is
   immutable and does not need the source content to reconstruct the decision boundary.
8. **Source loading remains fail-closed.** Adapters derive source identity and kind from stored
   records. Missing stored provenance does not fall back to caller-supplied identity.

## Considered alternatives

### Trust the requested authority when the content looks plausible

Rejected. Content plausibility is not provenance and lets a claimant manufacture rank.

### Put one global LOW-to-HIGH hierarchy in the generic core

Rejected. Investigation, personal preference, typed scalar, and future domains use different
vocabularies; some labels may be intentionally incomparable.

### Let each policy supply its own comparator

Rejected. A policy could redefine the ordering for one decision and promote evidence while still
appearing to obey the admission API.

### Admit using only the strongest source

Rejected. Mixed-source claims would inherit authority that part of their foundation cannot support.

### Down-rank malformed evidence instead of rejecting it

Rejected. A numeric penalty does not repair missing or contradictory foundation.

## Consequences

- Domains can reuse one fail-closed boundary without sharing labels or a global order.
- Multi-source claims are intentionally limited by their weakest foundation.
- Policies retain domain judgment while being unable to create authority.
- Adopters must preserve the complete source set and obtain trusted grants before calling policy.
- Rejection receipts make promotion attempts and malformed foundations visible for audit.
- Existing writers are not automatically governed merely because the core exists; each integration
  needs an explicit rollout and compatibility decision.

## Open owner decisions

1. **Adoption order:** which production writers and claim domains should move behind this boundary,
   and whether any legacy path must remain temporarily exempt.
2. **Domain semantics registry:** which authority labels and partial/total ordering belong to each
   domain, who owns changes, and how versions are identified in receipts.
3. **Grant lifecycle:** which component may issue, persist, revoke, or supersede
   `SourceAuthorityGrant` records for each source class.
4. **Receipt persistence:** whether admission receipts join a future unified decision ledger or
   remain in domain-owned stores, including retention and query requirements.
5. **Legacy contract convergence:** whether existing user-tier, typed-scalar, and event-history
   admission mechanisms should adapt to this primitive or remain separate bounded contracts.

Until those decisions are made, this ADR approves the core invariants, not a claim that all Menhir
writes are source-bound today.

## Evidence in the repository

- `src/menhir/domain/admission.py`
- `src/menhir/infrastructure/admission_source.py`
- `tests/test_admission_contract.py`
- `tests/test_admission_semantics_ownership.py`
- `.agent/memory-governance.md`

## Non-goals

This ADR does not define a universal authority vocabulary, authorize a writer migration, complete
the broader `ADMITTED`-predicate roadmap, or make admission receipts ranking signals.
