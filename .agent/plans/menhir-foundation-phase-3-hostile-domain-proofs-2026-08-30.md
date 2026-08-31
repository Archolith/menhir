---
artifact_schema: 1
artifact_uuid: aedbbfce-e2d4-40de-b2e1-cef3d9d37e16
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation Phase 3 — hostile-domain proofs

## Why

Pure tests show that investigation vocabulary can register a projection and that investigation and
personality policies can use the admission contract. They do not prove persistence, dirty routing,
materialization, retirement, freshness, recall, or coexistence. A foundation is generic only after
domains with materially different semantics traverse the complete host path without adding their
vocabulary to core.

## Scope

Build two reference extensions in sequence:

1. investigation ownership/hypothesis state as the hostile second domain;
2. personality preference/trait state as the cross-check against investigation-specific leakage.

Reference extensions live outside `src/menhir` and import only the promoted public seams selected by
the host. They may be in an examples/test package during this phase; packaging is stabilized only in
Phase 4.

## Proof A — investigation

The extension owns:

- evidence kinds such as official record, firsthand statement, media report, and anonymous tip;
- authority ordering and purpose-specific lowering rules;
- assertion payloads for source observation, ownership statement, transaction, support, and
  contradiction;
- target derivation for one parcel/hypothesis set;
- a fold that distinguishes `source says X` from `current ownership conclusion`;
- View kind, materializer, retirement behavior, and installed-state hash.

Required scenarios:

- an official deed supports an owner while an anonymous tip is retained as evidence but cannot
  self-promote over the admitted ceiling;
- two conflicting official records produce a deterministic abstention or competing-hypothesis View,
  according to extension policy;
- a later correcting record supersedes the current conclusion without deleting prior observations;
- removal of the last current assertion retires the View and certifies the absent hash;
- a definition-version change re-dirties and rebuilds every known target;
- replay and backfill converge to the same View and receipt set.

## Proof B — personality

The extension deliberately uses a different semantic shape:

- explicit user preference, observed behavior, third-party claim, and model reflection evidence;
- authority interpretation that treats direct user preference differently from factual ownership;
- assertions and folds for preference/trait confidence with abstention when evidence is synthetic or
  contradictory;
- a View surface suitable for bounded recall without exposing internal deliberation.

Investigation and personality must run together in one host composition, sharing lifecycle and
assertion infrastructure while retaining separate vocabularies, definitions, Views, and policies.

## Missing-seam protocol

Freeze the public core surface before each proof. If extension implementation requires a core edit:

1. record the exact blocked operation;
2. show why composition or an existing protocol cannot express it;
3. classify the need as a generic correctness guarantee or domain convenience;
4. add only a generic seam with a scalar regression and both-domain test;
5. restart the zero-core-diff proof from the new surface.

Domain convenience never enters core. A central `if investigation`/`if personality`, vocabulary
constant, schema property, or registration switch fails the phase.

## Validation

- Pure fold laws: determinism, input-order independence, replay idempotence, explicit abstention and
  retirement, contributor completeness.
- Admission: upward authority requests refuse/clamp, purpose-specific lowering works, grants remain
  source-bound and immutable.
- Real Neo4j E2E: evidence → assertion → dirty generation → materialized View → receipt → freshness
  → retrieval for each domain.
- Coexistence: scalar, investigation, and personality share one runtime without registry collision,
  namespace contamination, scheduler starvation, or View supersession across definitions.
- Failure: corrupt payload, codec version mismatch, missing materializer, stale worker, definition
  upgrade, duplicate current View, and extension-specific fold exception all fail visibly.
- Architecture guard: a source census proves no investigation/personality vocabulary or imports were
  added under `src/menhir`.

## Exit gate

Coding/scalar, investigation, and personality all use the same durable admission, assertion,
projection lifecycle, and freshness contracts. Both reference extensions can be removed from host
composition without changing core, and their end-to-end tests require no private Menhir imports or
central switch edits.

## Docs to create or update

- investigation reference-extension README and scenario model
- personality reference-extension README and scenario model
- extension testing guide
- `.agent/architecture.md` boundary section
- `CHANGELOG.md`
