# Menhir Core Promotion Roadmap

Status: active research/promotion plan

This roadmap converts the mutation-kernel research into production-facing extension seams in small, independently testable tranches. The goal is **not** to rewrite Menhir or promote the spike schema wholesale. The goal is to expose the generic architecture that production already largely contains, preserve existing coding/scalar behavior as the compatibility baseline, and prove each abstraction with more than one domain before expanding it.

## Governing rules

1. **Promotion, not rewrite.** Prefer adapters and registries around production machinery that already works.
2. **One seam at a time.** Each tranche should be small enough that a regression can be attributed to one architectural change.
3. **Default behavior must remain unchanged.** Existing built-ins are the default registry/configuration unless a tranche explicitly documents a migration.
4. **Scalar/coding behavior is the regression oracle.** Existing production semantics must remain intact while extension seams are opened around them.
5. **No giant plugin framework prematurely.** Add explicit registries/protocols only where a hostile second domain proves they are needed.
6. **No production merge while the audit/remediation picture is moving.** Research and promotion branches remain isolated until audit findings and remediation are reconciled.
7. **Execution evidence matters.** Every tranche should have focused tests and real CI execution before being considered validated.
8. **Do not mix remediation with abstraction unless required.** Pre-existing correctness defects discovered during promotion should be recorded separately unless the abstraction cannot be implemented safely without fixing them.

## Tranche 1 — View-kind registration

**Status: implemented and validated**

Branch: `research/core-promotion-1`

Clean tested implementation: `f49ac3aebeea4ca862e1b18f3851df7095234d3f`

Goal: make the existing production `ViewKind` vocabulary injectable without changing default behavior.

Implemented properties:

- Existing built-in View kinds remain the default registry.
- Existing callers can continue constructing `ViewRepository` normally.
- The class-level built-in compatibility surface remains available.
- Injected registries are instance-local.
- Registry key and `kind.name` mismatches fail closed.
- A test-only non-coding `ViewKind` can be recorded without editing shared View persistence machinery.
- No schema, scalar, writer, or query semantic changes were required.

Exit criterion: **met**. Focused registration tests executed green in branch-only CI and the clean implementation branch contains only the intended production seam plus tests.

## Tranche 2 — Evidence-kind registration

**Status: implemented and validated**

Branch: `research/core-promotion-2`

Clean tested implementation: `7dbadb76ab116815308d671d492c98b33b3055cd`

Validation workflow run: `31921918167` (`core-promotion-2-validation`), successful on temporary validation SHA `86778149c644e401bd3e0f21b96e75b950c2b510`; the branch was then reset to the clean implementation SHA so the temporary workflow is not part of the tranche.

Goal: replace the closed evidence/source-kind vocabulary with one immutable registration seam while preserving all current built-in compatibility surfaces.

Implemented design:

- Added a single immutable `EvidenceKindRegistry` under `domain/truth/kinds.py`.
- Each `EvidenceKindDefinition` may declare:
  - canonical kind ID,
  - accepted source-label aliases,
  - anchor/self-source classification,
  - optional existing `EvidenceSignal`,
  - optional diversity family.
- Preserved current module-level constants as compatibility projections of the default registry:
  - `ANCHOR_KINDS`
  - `SELF_SOURCE_KINDS`
  - `SOURCE_LABEL_TO_KIND`
  - `KIND_TO_SIGNAL`
  - `DIVERSITY_FAMILY`
- Existing consumers remain unchanged.
- Extension registration is additive and instance-local: `with_definition()` returns a new registry and never mutates `DEFAULT_EVIDENCE_KIND_REGISTRY`.
- Duplicate canonical kinds, ambiguous aliases, aliases colliding with another canonical kind, and contradictory anchor/self-source classification fail closed.
- Existing rule-zero source resolution remains unchanged: unknown/harness source labels collapse to `agent_inference`.
- A non-coding `investigation.deed` evidence kind can be registered with deed aliases and a public-record diversity family without editing the default vocabulary.
- A personality self-source kind can be registered without making it an external anchor.
- Source-confidence thresholds, belief weights, admission policy, and purpose-specific trust semantics deliberately remain outside this registry.

Compatibility note: an earlier planning note incorrectly described `KIND_TO_SIGNAL` as float-valued. Re-reading production before implementation confirmed it already maps to `EvidenceSignal` enum values. No scoring-type remediation was required or performed in this tranche.

Validation corpus:

- `tests/domain/test_evidence_kind_registry.py`
- `tests/domain/test_self_reinforcement.py`
- `tests/domain/test_belief.py`
- `tests/domain/test_belief_currentness.py`
- `tests/domain/test_diversity.py`

Exit criterion: **met**. The validation workflow completed successfully, existing compatibility suites stayed green, and a non-coding evidence kind can be registered externally without mutating the built-in registry.

## Tranche 3 — Authority and admission policy boundary

**Status: next**

Goal: remove the assumption that one global authority hierarchy is semantically sufficient for every domain while retaining a hard core-controlled admission ceiling.

Core responsibilities:

- bind admission grants to actual provenance/source mechanisms;
- prevent untrusted payloads from self-promoting authority;
- preserve immutable admitted authority/provenance;
- enforce that extension interpretation can lower/reinterpret trust but cannot exceed the admission ceiling.

Extension responsibilities:

- define domain-specific admissible source classes;
- define purpose/domain-specific authority interpretation;
- optionally reject or lower a proposed authority;
- define richer trust facets without redefining core provenance.

Required proof cases:

- model inference requesting `user` or `manual` authority remains clamped to its core admission ceiling;
- investigation can distinguish official record, firsthand statement, media/reporting, anonymous tip, and model inference without forcing those concepts into core;
- personality can interpret explicit user preferences differently from third-party factual claims;
- legacy scalar authority resolution remains unchanged.

Exit criterion: two materially different domains use the same core admission contract without domain vocabulary appearing in core.

## Tranche 4 — Projection-definition registration

Goal: move from externally registered input/output vocabulary to externally registered belief/materialization semantics.

Introduce a production `ProjectionDefinition`-style contract in terms of existing repositories/services rather than copying the spike implementation wholesale.

Extension owns:

- accepted input assertion types;
- target/slot derivation;
- fold semantics;
- output View type;
- domain-specific abstention/retirement conditions.

Core owns:

- definition identity/version;
- scheduling;
- dirty discovery;
- lifecycle state;
- persistence coordination;
- stale-writer protection.

Required proof:

- multiple assertion types can feed one View;
- a non-coding extension can register its projection without editing a central switch/table;
- existing scalar projection behavior is unchanged.

## Tranche 5 — Incremental projection lifecycle

Goal: promote the generic lifecycle guarantees proven in the spike into production infrastructure.

Candidate pieces, promoted only as needed:

- dirty generations;
- optimistic generation tokens;
- stale-worker rejection;
- idempotent assertion replay;
- historical/backfill discovery;
- projection definition versions;
- removed-target reconciliation;
- `View | Abstention | Retirement` lifecycle semantics;
- derivation/projection hashes where needed for correctness.

Do **not** import the spike schema as-is. Map each guarantee onto existing production persistence/services first.

Exit criterion: crash/retry/replay/version-change tests show that stale workers cannot overwrite newer semantic state and missed work can be rediscovered.

## Tranche 6 — Hostile second-domain proof: investigation

Goal: prove the promoted API can support a domain with substantially different semantics from coding/scalars.

Candidate investigation concepts:

- source observation / source says claim;
- interpreted entity identity;
- ownership/transaction/event assertion;
- supports/contradicts relationship;
- current ownership conclusion;
- competing hypothesis set.

Critical invariant:

> **“A source says X” is not the same thing as “Menhir currently believes X.”**

Evidence/source records remain durable even when interpretation, identity resolution, or current belief changes.

The investigation extension should use **only public promoted extension points**. Any required core edit is evidence for a missing generic seam and should be evaluated before being added.

Exit criterion: an investigation fixture can ingest domain-specific evidence and materialize a domain-specific current conclusion without adding investigation vocabulary to core.

## Tranche 7 — Personality cross-check

Goal: verify that investigation was not merely a new hard-coded special case.

Re-run the personality hostile-domain experiment through the same production extension interfaces.

Required proof:

- personality and investigation coexist;
- both define their own evidence/assertion/projection semantics;
- neither requires its vocabulary in core;
- coding/scalar defaults continue to work unchanged.

Exit criterion: at least coding/scalar, investigation, and personality operate through the same generic lifecycle contracts while retaining different semantics.

## Tranche 8 — Extension packaging and developer surface

Only after the semantic contracts have survived multiple domains, define the developer-facing extension surface.

Potential pieces:

- explicit extension manifest;
- initialization/registration lifecycle;
- registry collision handling;
- definition version declarations;
- dependency declarations;
- extension documentation/testing helpers.

Avoid runtime/plugin machinery unless actual deployment requirements demand it. A few explicit registries and protocols may be sufficient.

Exit criterion: an extension can be implemented outside core with a small, documented public interface and deterministic startup validation.

## Tranche 9 — Production integration and consolidation

Goal: remove redundant closed/internal paths only after generic replacements are proven.

Tasks may include:

- migrate coding-specific registrations onto the public extension contracts;
- remove duplicated closed tables that no longer serve compatibility needs;
- consolidate lifecycle/versioning implementations;
- run broad Neo4j and regression suites;
- compare behavior before/after promotion;
- reconcile every production change against active audit/remediation findings.

No merge to `main` until this reconciliation is complete.

## Tranche 10 — Higher-order capabilities

These are **not blockers for the core abstraction**. Consider only after the promotion path is stable.

Possible work:

- belief diffs;
- memory/blame provenance;
- “why does Menhir believe this?” traversal;
- refold/replay after policy changes;
- counterfactual belief branches;
- historical semantic-definition comparison;
- content-addressed projection/belief state.

## Architectural checkpoint

The mutation-kernel experiments suggest the generic semantic center is relatively small:

```text
Evidence
   ↓
immutable Assertion
   ↓
current-set / supersession
   ↓
extension-owned Fold
   ↓
View | Abstention | Retirement
```

The bulk of the difficult work is not domain semantics. It is lifecycle correctness around that small center:

```text
admission / provenance
        ↓
versioned semantic definition
        ↓
dirty discovery + generations
        ↓
stale-writer fencing
        ↓
projection / derivation
        ↓
freshness certification
        ↓
retrieval / bounded context
```

That distinction should guide promotion decisions: **keep domain meaning outside core; keep correctness guarantees inside core.**

## Current milestone sequence

```text
1. Open ViewKind registration                         DONE
2. Open evidence/source-kind registration             DONE
3. Promote authority/admission boundary               NEXT
4. Register projection definitions
5. Promote lifecycle/versioning/fencing
6. Prove investigation through public extension API
7. Prove personality through the same API
8. Define packaging/developer surface
9. Consolidate and reconcile with audit/remediation
10. Explore higher-order belief/provenance features
```

## Definition of success

Menhir Core should not know what a Person, Parcel, OWNS relationship, personality trait, source-code Symbol, or investigative hypothesis *means*.

Core should know how to provide durable evidence/provenance, immutable assertions, temporal/lifecycle envelopes, registered semantic definitions, concurrency/version guarantees, rebuildable projections, freshness, and safe bounded retrieval.

A domain extension should supply the vocabulary and the meaning.
