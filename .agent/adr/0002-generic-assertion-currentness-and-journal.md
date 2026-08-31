# ADR 0002 — Generic assertion currentness and mutation journal

- **Status:** PROPOSED implementation target. Owner acceptance is required before Phase 1 schema or
  repository implementation begins.
- **Date:** 2026-08-30
- **Deciders:** Menhir owner; drafted from the four independent foundation-plan reviews.
- **Related:** `.agent/plans/menhir-foundation-completion-2026-08-30.md`,
  `.agent/plans/menhir-foundation-phase-1-extension-substrate-2026-08-30.md`, ADR 0001.

## Context

Menhir already has scalar- and event-specific immutable assertions, source-relative currentness,
admission contracts, and projection lifecycle primitives. A generic extension substrate cannot
reuse scalar slot semantics, but it also cannot leave identity, admission, replay, currentness, or
dirty-work discovery to each extension. That would create multiple authorities and make crash
recovery dependent on best-effort callbacks.

This ADR fixes the domain-neutral storage and ordering decisions that Phase 1 must implement. It
does not choose investigation or personality vocabulary, define a public plugin system, migrate
legacy scalar/event records, or activate production projection writers.

## Decision

### 1. Core-owned records

Phase 1 adds these target graph records. Names are part of the target schema and must change through
a later ADR rather than implementation convenience:

| Record | Role | Mutable fields |
|---|---|---|
| `:GenericAssertion` | Immutable admitted assertion envelope and lifecycle operation | none |
| `:GenericAssertionPayload` | Canonical extension-owned payload bytes | none; the node may be removed only by authorized erasure |
| `:AdmissionDecision` | Immutable snapshot of the source, grant, ceiling, policy, and result used for admission | none |
| `:GenericAssertionHead` | Unique source-relative currentness fence | `head_revision` and its one `CURRENT_ASSERTION` edge |
| `:ProjectionInputMutation` | Immutable ordered journal/outbox fact | delivery metadata is stored separately |
| `:MutationStreamHead` | Unique sequence allocator for one namespace/type/purpose stream | `last_sequence` |
| `:AssertionErasureReceipt` | Immutable proof that payload bytes were removed under policy | none |

Relationships are explicit:

```text
(:GenericAssertion)-[:HAS_PAYLOAD]->(:GenericAssertionPayload)
(:GenericAssertion)-[:ADMITTED_BY]->(:AdmissionDecision)
(:GenericAssertion)-[:ASSERTS_FROM]->(:TurnEvidence|:Episodic|other registered source)
(:GenericAssertion)-[:SUPERSEDES]->(:GenericAssertion)
(:GenericAssertionHead)-[:CURRENT_ASSERTION]->(:GenericAssertion)
(:ProjectionInputMutation)-[:RECORDS_ASSERTION]->(:GenericAssertion)
(:AssertionErasureReceipt)-[:ERASED_PAYLOAD_OF]->(:GenericAssertion)
```

An implementation may add indexes or non-semantic bookkeeping properties, but it may not merge
these authority roles or make an extension-owned node authoritative for core replay/currentness.

### 2. Namespace and source identity

Canonical namespace is non-null and participates in every source, grant, assertion, head, hash,
journal-stream, projection-target, uniqueness, and transaction-fence identity. Generic writes never
persist an empty, omitted, or legacy alias spelling.

Legacy aliases are accepted only at a read boundary that resolves them to exactly one canonical
namespace before lookup. Zero, multiple, cyclic, or changing alias resolutions fail closed. Alias
resolution does not rewrite a physical legacy View key.

The first generic source resolver uses unique `(canonical_namespace, turn_key)` identity for
`TurnEvidence`. A registered source kind may use another key only when the host declares an
equivalent database uniqueness constraint. Display IDs such as unscoped `turn_id` are never
authority. Admission requires exactly one durable source row.

### 3. Three assertion identities

The contract separates request idempotency, semantic claim identity, and source-relative currentness:

1. `assertion_key` scopes the caller-supplied stable `assertion_id` by canonical namespace and
   assertion type. It is the replay/collision key and does not derive from content.
2. `claim_key` identifies the extension-defined semantic claim family. Core derives it from
   canonical namespace, assertion type, purpose, subject identity, and the codec-validated canonical
   `claim_discriminator`. Examples may resemble a scalar slot or hypothesis set, but core stores no
   domain vocabulary.
3. `head_key` adds the namespace-bound source identity to `claim_key`. Currentness is therefore per
   source and claim. Independent sources can remain current simultaneously and the extension fold
   can compare them.

All three use a versioned, domain-separated SHA-256 derivation over length-delimited canonical UTF-8
components. Human-readable IDs remain stored for audit; database uniqueness is enforced on the
derived keys.

Subject identity is part of `claim_key` and cannot be mutated in place. A rebind operation locks the
old and new heads in sorted key order, writes a tombstone for the old head and a new assertion for
the new head, and records both target transitions in one journal entry and transaction. A plain
update that changes subject identity is refused.

### 4. Versions, disagreement, and currentness

`payload_schema_version` describes payload encoding and decoding. `interpretation_version` orders
semantic replacements within one `head_key`. They are independent integers and neither substitutes
for projection definition version.

For a non-replay assertion:

- a greater interpretation version becomes the new current head and supersedes the prior current
  assertion;
- an equal version with identical assertion ID/content is exact replay;
- an equal version with different content is persisted with disposition
  `SAME_VERSION_CONFLICT`, does not move the head, and cannot become a current projection input;
- a lower version is persisted with disposition `STALE_RECORDED`, does not move the head, and cannot
  become a current projection input.

Persisting conflict/stale evidence is useful for audit, but it does not grant two current values to
one source-relative head. The journal records the disposition; only a head-changing entry dirties
or retires a projection target.

### 5. Removal and projection retirement

Removal is an immutable `GenericAssertion` with operation `REMOVE`, no payload node, and an
interpretation version greater than the current head. The head points to the tombstone. A current
tombstone means that source contributes no live assertion to the claim. The prior assertion and
`SUPERSEDES` chain remain durable.

Removal of an assertion contribution is not projection-definition retirement. Assertion removal
changes source-relative current input and emits ordinary dirty/retire journal work. Definition
retirement follows the separate Phase 4 generation, target-drain, durable-tombstone, omission, and
reinstall protocol and never rewrites assertion currentness.

### 6. Atomic admission and replay

One host-owned mapping keyed by `(assertion_type, purpose)` selects the source resolver, grant
authority, authority semantics, policy/configuration version, codec owner, repository owner, and
journal routing. A request may select a registered type and purpose but cannot override those
bindings.

`admit_and_record` executes in one `execute_write` transaction. It canonicalizes and bounds the
request, resolves exactly one namespace-bound source and its grant, fences every mutable admission
input and affected assertion head, records the requested authority, clamps it to the ingress
ceiling, and then permits mapped policy only to reject, reinterpret, or lower the candidate. Policy
may never raise authority. Missing/mismatched/incomparable/policy-rejected admission commits no
decision, assertion, head change, or journal entry.

On success, the transaction writes the immutable `AdmissionDecision`, assertion and optional
payload, head disposition/change, and one sequenced journal entry. A concurrent source/grant/policy
input, head, or sequence change retries from current durable state or returns a typed conflict.

Exact replay is the same canonical namespace, assertion key, canonical content bytes, and content
hash. It returns the original decision, assertion disposition/head result, and journal linkage
without reevaluating current grant, revocation, codec, or policy state. The same assertion key with
different bytes or hash is a collision and is refused. Later revocation or software upgrades never
rewrite an earlier decision; a new semantic assertion evaluates against current state.

### 7. Payload encoding and erasure

The Phase 1 plan owns the canonical RFC 8785 JSON, strict UTF-8, NFC identity, bounds, codec
ownership, and domain-separated hash contract. The accepted canonical bytes and hashes are stored,
not recreated from decoded objects.

Payload bytes live on `GenericAssertionPayload` so authorized erasure does not mutate the immutable
assertion envelope. Erasure deletes exactly that payload node and writes an
`AssertionErasureReceipt` containing assertion key, payload hash, byte length, policy/authorization
identity, transaction time, and reason. Source, decision, envelope metadata, content/payload hashes,
head chain, and journal facts remain. Reads and export return a typed erased-payload result and never
fall back to cached or re-encoded content. A current assertion whose required payload is erased
makes dependent projection state unavailable/corrupt until lifecycle invalidation or rebuild from
remaining evidence completes.

### 8. Ordered mutation journal

Each stream is keyed by `(canonical_namespace, assertion_type, purpose)`. Its
`MutationStreamHead` allocates a strictly increasing sequence inside `admit_and_record`. A
`ProjectionInputMutation` is unique by stream and sequence and contains:

- assertion key/content hash, admission-decision key, operation, disposition, and commit time;
- source, claim, old/new head, old/new subject/target routing identities, including tombstone state;
- payload schema, interpretation, codec, and immutable definition-set/routing identity;
- enough immutable before/after data to route the committed transition without rereading mutable
  source, grant, or policy state.

Exact replay reuses the original mutation. Conflict/stale records receive a journal entry with no
head transition so the audit sequence remains complete, but consumers do not dirty projections for
them. Immediate dispatch is optional latency only.

Phase 2 consumers keep their own durable cursor. They advance it only in the same transaction that
idempotently applies every dirty/retire transition for the entry. Historical census uses a snapshot
plus upper journal watermark and rechecks absence before retirement. Journal delivery metadata,
attempts, claims, and quarantine never mutate the immutable journal fact.

### 9. Ownership during compatibility

Until Phase 4 changes authority through an attested writer-fence rollout:

| Assertion family | Admission/currentness writer | Generic read adapter | Generic write allowed |
|---|---|---|---|
| scalar `TypedAssertion` | legacy scalar repository/service | yes, read-only | no |
| `TypedEventAssertion` | legacy event repository/service | yes, read-only | no |
| registered generic types | generic `admit_and_record` repository | native | yes |

Every registered assertion type has one machine-readable writer, reader, currentness authority, and
admission model. Startup refuses missing, duplicate, or overlapping ownership. No transaction
dual-writes a legacy and generic assertion store. The project must not claim universal generic
admission while a promoted family still has a legacy authority.

### 10. Mixed-version readiness

The host manifest declares supported envelope, payload-schema, interpretation, codec, identity,
hash, journal, and policy versions. Readiness refuses when any active current head, undispatched
journal entry, or pending lifecycle work requires an unsupported or ambiguously comparable version.
Unsupported historical non-current assertions may remain stored and exportable from original bytes;
they do not become current through fallback decoding. Exact replay may return an opaque original
receipt without reinterpretation when its stored integrity can still be verified.

## Consequences

- Multiple sources can contribute concurrently without multiple current assertions from one source
  claiming the same semantic head.
- Corrections, removals, and rebinding are ordered, replayable, and crash-recoverable.
- Best-effort callbacks cannot become a second work authority.
- Payload erasure is explicit and auditable without pretending erased content is still usable.
- Legacy scalar/event behavior remains unchanged until the separately fenced cutover.
- The generic schema is more explicit than a single assertion node, but each record has one authority
  role and can be constrained/tested independently.

## Rejected alternatives

- One global current assertion per claim: rejects legitimate independent-source disagreement.
- Content-derived assertion ID: cannot distinguish exact replay from caller-ID collision.
- Extension-owned currentness repositories: duplicates namespace, replay, and concurrency rules.
- Post-commit callback as dirty authority: loses same-target changes on crash.
- In-place subject rebinding or assertion payload mutation: destroys immutable provenance.
- Physical deletion of the whole assertion on erasure: destroys admission and ordering evidence.

## Acceptance before implementation

Owner review must explicitly accept or amend: the three-key identity model, source-relative head,
same-version/stale dispositions, atomic rebind, tombstone semantics, payload-node erasure, stream
partition key, and mixed-version refusal. Phase 1 implementation then proves the concurrent,
namespace, replay, collision, codec-boundary, retention/export/erasure, and real-Neo4j constraint
cases named in its plan.
