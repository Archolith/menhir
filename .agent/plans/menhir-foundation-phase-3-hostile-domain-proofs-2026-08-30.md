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
domains with materially different semantic algebras traverse the complete production host path
without adding their vocabulary to core.

## Scope and phase boundary

Build two reference extensions in sequence:

1. investigation ownership and competing-hypothesis state as the hostile second domain;
2. personality preference and behavioral-belief state as the cross-check against
   investigation-specific leakage.

Reference extensions live outside `src/menhir`. They may remain in an examples or test package in
this phase. Phase 3 proves extension behavior through the Phase 2 frozen provisional public facade;
it does not stabilize package names, public API compatibility, extension distribution, durable
definition retirement, or operational decommission. Those remain Phase 4 work.

## Frozen extension boundary

At the start of Phase 3, record the Phase 2 baseline commit, the tree or blob identity of
`src/menhir`, the provisional public facade, and the exact import allowlist available to extensions.
Both reference extensions use that same frozen baseline.

The boundary gate consists of all of the following:

- Parse every extension Python file with the Python AST and reject every `Import` or `ImportFrom`
  whose resolved module is not on the Phase 2 allowlist. Private-module imports, relative escapes,
  and import-by-string helpers are forbidden.
- Compare `src/menhir` to the recorded baseline commit and require a zero diff while each hostile
  domain is developed and validated. A working-tree-only comparison is insufficient.
- Maintain a domain-token manifest for investigation and personality. The manifest lists exact
  domain nouns, identifiers, discriminator values, aliases, and normalized variants, plus the
  extension paths where each token is allowed. A manifest-driven lexical and AST identifier/literal
  census must prove those tokens do not occur under `src/menhir`; a vague vocabulary grep is not an
  acceptable architecture guard.

If an extension exposes a missing generic seam, stop the domain proof and:

1. record the exact blocked operation and the facade member that is missing;
2. show why composition or an existing protocol cannot express the operation;
3. classify the need as a generic correctness guarantee rather than domain convenience;
4. land the core seam separately with a scalar regression and tests for both hostile domains;
5. update and re-freeze the provisional facade, import allowlist, baseline commit, and
   `src/menhir` identity; and
6. restart both zero-core-diff proofs from the new baseline.

A central investigation/personality branch, vocabulary constant, schema property, registration
switch, or domain-specific codec fails the phase. Packaging and API stabilization still wait for
Phase 4 even when a generic seam is added.

## Proof A - investigation algebra

Investigation materializes a structured competing-hypothesis set, not a scalar winner. Its View
contains each live ownership hypothesis, its conclusion status, supporting assertion IDs,
contradicting assertion IDs, source and admission-decision lineage, and the authority frontier used
by the fold. Every support and contradiction must remain traceable to its immutable assertion.

Authority is a non-total, purpose-sensitive relation. Official record, firsthand statement, media
report, and anonymous tip may be ordered for a particular purpose, but the extension must support
incomparable authorities. The fold must not manufacture a universal numeric rank or select an
arbitrary maximum. An assertion admitted below the conclusion ceiling can remain evidence without
self-promoting to the ownership conclusion. Incomparable credible claims remain explicit competing
hypotheses or cause abstention according to the declared investigation policy.

Required investigation scenarios:

- Correction: deed `D1` supports owner A. Later source `D2` explicitly corrects `D1` and supports
  owner B. The rebuilt View selects B while preserving `D1`, its admission decision, assertion, and
  correction lineage; A is not silently deleted from history.
- Conflict: two current official records support A and B and are incomparable for the ownership
  purpose. The View deterministically exposes both hypotheses with separate support and
  contradiction lineage and does not claim a single owner.
- Removal: retirement of the last current assertion for a hypothesis removes that hypothesis from
  current target membership. Retirement of the final live hypothesis retires the View and produces
  the canonical absent hash without deleting any source or assertion.
- Replay: replaying the exact same admitted sequence produces the same competing-hypothesis View,
  target membership, definition version, freshness certificate, and exact historical receipts.
  Backfill in a different valid sequence may have different historical receipts but must converge
  to the same current state tuple.
- Version change: a definition-version bump re-dirties every known investigation target. Rebuild
  uses the new algebra, records the new version, and makes no old-version target appear fresh.

## Proof B - personality algebra

Personality deliberately uses a different algebra. It materializes bounded beliefs about a
subject's preferences or traits from explicit user preferences, observed behavior, third-party
claims, and model-generated reflections.

For the same subject and preference key, the newest live explicit user preference has declared
precedence over aggregated behavioral inference. In the absence of a live explicit preference,
admitted behavioral observations aggregate through a deterministic confidence function with
separate supporting and counterevidence contributors. Third-party claims and model reflections are
synthetic evidence: they may annotate or counter a belief but cannot, by themselves, produce a
positive conclusion. A synthetic-only contributor set must abstain. Contradictory behavior below
the configured confidence boundary must also abstain rather than imitate investigation's
competing-owner representation.

Required personality scenarios:

- Correction: explicit preference `P1` says "dislikes crowds." A later explicit correction `P2`
  says "enjoys crowded concerts." `P2` takes precedence while `P1`, both admission decisions, both
  assertions, and their correction relation remain durable.
- Conflict and abstention: balanced admitted behavior supports and counters "prefers mornings,"
  leaving confidence below the conclusion boundary, so recall abstains. A model reflection and a
  third-party claim without direct or behavioral evidence also produce synthetic-only abstention.
- Removal: retiring the current explicit preference reveals the result of the still-live behavioral
  aggregate. Retiring its final admissible contributor retires the belief View and certifies the
  canonical absent state without deleting old evidence.
- Replay: replaying the exact same sequence reproduces the same precedence result, aggregate
  confidence, contributors, current state tuple, and exact historical receipts. A differently
  ordered backfill may differ historically but must converge to the same current state tuple.
- Version change: a confidence-policy or fold-definition bump re-dirties every known personality
  target, recomputes the aggregate under the new version, and prevents old-version freshness from
  satisfying recall.

Investigation and personality must run together in one host composition. They share admission,
assertion, lifecycle, scheduling, receipt, and freshness infrastructure while retaining separate
algebras, policies, definitions, Views, and domain tokens.

## Durable provenance and bounded recall

Each accepted conclusion must be recoverable from real Neo4j through this complete chain:

`durable source -> immutable grant/admission decision -> immutable assertion ->
contributor or counterevidence relation -> current View`

Production admission must persist and read back the source-bound immutable decision. Materializers
must attach classified contributor and counterevidence relations to the current View rather than
copying untraceable payload fragments. A correction creates a new source, decision, assertion, and
explicit correction relation. Retirement creates durable lifecycle state. Neither operation mutates
or deletes the earlier source, decision, assertion, or provenance relations.

The actual bounded recall API, not a repository helper or direct graph query, must retrieve each
domain. It must label returned investigation and personality conclusions as beliefs rather than
facts and preserve competing status where policy permits a non-abstained competing-hypothesis
belief. It must exclude retired Views, abstained conclusions, cross-tenant data, internal
deliberation or model rationale, stale definition versions, and Views without a valid freshness
certificate. Diagnostic APIs may expose why a belief abstained, but bounded recall must not return
the abstained conclusion as a belief or leak the internal deliberation payload.

## Symmetric production proof matrix

Every row is required for both domains through production host seams and real Neo4j. Pure fold
fixtures may supplement these tests but cannot satisfy a row.

| Contract | Investigation proof | Personality proof |
| --- | --- | --- |
| Production admission write/readback | Admit source-bound ownership evidence, then read back the immutable grant/decision and assertion | Admit explicit, behavioral, third-party, and synthetic evidence, then read back the immutable grant/decision and assertion |
| Dirty routing | Route only the affected parcel/hypothesis-set target and generation | Route only the affected subject/preference-key target and generation |
| Correction | Add a correcting record and preserve the corrected source/assertion lineage | Add a correcting explicit preference and preserve the corrected source/assertion lineage |
| Conflict or abstention | Materialize competing hypotheses or declared abstention with support and contradiction edges | Apply explicit-preference precedence; otherwise aggregate behavior and abstain for low-confidence or synthetic-only evidence |
| Retirement | Retire the final live ownership contributor, remove current membership, and certify absence | Retire explicit and behavioral contributors, remove current membership when none remain, and certify absence |
| Definition bump | Dirty and rebuild every known investigation target at the new definition version | Dirty and rebuild every known personality target at the new definition version |
| Replay/backfill | Prove state-tuple convergence and receipt rules for exact replay and differently sequenced backfill | Prove state-tuple convergence and receipt rules for exact replay and differently sequenced backfill |
| Actual recall API | Return tenant-scoped ownership beliefs with competition status, while excluding abstained, retired, or internal data | Return tenant-scoped preference beliefs, omitting abstained conclusions and internal rationale |
| Restart/reinstall | Restart with the definition installed, then reinstall after omission and converge from durable evidence | Restart with the definition installed, then reinstall after omission and converge from durable evidence |

## Hostile coexistence and key ownership

The coexistence suite must deliberately collide raw namespace strings, subject IDs, discriminator
values, and key shapes across scalar, investigation, and personality definitions and View kinds.
Raw equality must not imply shared ownership.

Startup must either reject a registration whose canonical persistence key has ambiguous ownership,
with diagnostics naming every claimant and colliding key component, or persistence must include a
durable definition/View-kind owner that isolates the colliding records. Silent last-writer-wins,
registry shadowing, cross-definition supersession, and reliance on friendly test identifiers fail
the phase.

For each canonical `(tenant, definition owner, View kind, target)` key, concurrent workers must
finish with exact current cardinality: zero after valid retirement, otherwise exactly one. The
persistence boundary must reject a second current View atomically. Tests must cover two workers
racing the same key and a deliberately seeded duplicate-current state; startup/readiness or the
write must reject the ambiguity instead of choosing one record.

## Composition omission and reinstall

Omitting either extension on a fresh host with no active durable definition succeeds and leaves the
other domains usable. Omitting an extension while its durable definition is active must fail
readiness with a diagnostic naming the missing definition. Phase 3 must not interpret omission as
retirement, delete evidence, or claim decommission complete. Durable definition retirement and
operational decommission are Phase 4 protocols.

All source, decision, assertion, lifecycle, View-history, and receipt evidence remains durable while
the definition is omitted. Reinstalling the same compatible definition re-dirties its known targets
as needed and converges to the prior current state without contaminating other domains. A changed
definition follows the definition-bump path and converges at the new version.

## Convergence, freshness, and receipts

Convergence is equality of the canonical current state tuple, not general equality of execution
history. For every known target the tuple contains:

- the canonical present hash, or the canonical absent hash when no current View exists;
- exact membership in the definition's current target set;
- the installed definition version; and
- a freshness certificate bound to that target, version, hash, and completed generation.

The canonical absent hash must be explicit and versioned; missing data is not by itself proof of
absence. Recall is allowed only when all tuple components agree.

Receipts remain immutable, with exactly one receipt record per `work_key` and generation. A failed
or rolled-back attempt creates no success receipt. Exact historical receipt equality is required
only when replay executes the exact same admitted event sequence and generations. Backfill,
repair, reinstall, or a differently ordered valid sequence may produce different receipt history;
they pass when receipt uniqueness holds and the canonical current state tuple converges.

## Failure and repair proofs

Cover corrupt payload, codec-version mismatch, missing materializer, stale worker, definition bump
during work, duplicate current View, extension fold exception, and ambiguous key ownership. For
each applicable failure, an exception alone is insufficient. The test must prove:

- atomic rollback: no partial current View, contributor edge, target-membership change, or freshness
  mutation survives the failed unit of work;
- isolation: the other domain, tenant, target, and completed generation remain unchanged;
- no false success receipt or freshness certificate is created;
- diagnostics identify the definition, tenant, target/work key, generation, failure category, and
  actionable conflicting version, owner, codec, or materializer detail without leaking internal
  deliberation; and
- after the fault is repaired, retry or re-dirty processing converges to the expected canonical
  current state tuple with exactly one current View when present.

The stale-worker and concurrent-writer cases must additionally prove that a losing write cannot
overwrite a newer generation. Duplicate-current and ambiguous-owner cases must remain unready until
the conflicting state or registration is repaired.

## Validation

- Pure algebra laws: deterministic output, input-order independence where the domain declares
  commutativity, exact-sequence replay idempotence, explicit competition/abstention, retirement,
  and complete contributor/counterevidence lineage.
- Admission: upward authority requests are recorded and clamped to the ingress ceiling;
  purpose-specific lowering works; missing, mismatched, incomparable, or policy-rejected admission
  fails closed; and grants remain immutable and source-bound.
- Real Neo4j end to end: execute every row of the symmetric matrix and recover the complete durable
  provenance chain for every recalled belief.
- Coexistence: scalar, investigation, and personality share one runtime under deliberate identity
  collisions without contamination, scheduler starvation, ambiguous ownership, or cross-definition
  View supersession.
- Boundary: AST import checks pass, the commit-pinned `src/menhir` zero-diff gate passes, and the
  domain-token manifest census reports no domain leakage into core.
- Failure: every injected fault proves rollback, isolation, absence of false receipts/freshness,
  useful diagnostics, and convergence after repair.

## Exit gate

Coding/scalar, investigation, and personality use the same durable admission, assertion,
projection-lifecycle, receipt, and freshness contracts while implementing materially different
semantic algebras. Both hostile domains pass the full symmetric matrix through real Neo4j and the
actual recall API. Their provenance is complete, their collisions are rejected or durably isolated,
their concurrent current cardinality is exact, and their failure tests prove repair convergence.

The extensions require no private Menhir imports, core vocabulary, central switches, or unrecorded
core edits. Fresh-host omission succeeds; omission of an active durable definition fails readiness;
evidence survives omission; and reinstall converges. Phase 3 makes no claim that package/API
stabilization, durable definition retirement, or operational decommission is complete.

## Docs to create or update

- investigation reference-extension README and scenario model
- personality reference-extension README and scenario model
- extension testing guide, including the symmetric matrix and failure assertion pattern
- Phase 2 facade/import allowlist freeze record and `src/menhir` baseline identity
- domain-token manifest and census instructions
- `.agent/architecture.md` boundary section
- `CHANGELOG.md`
