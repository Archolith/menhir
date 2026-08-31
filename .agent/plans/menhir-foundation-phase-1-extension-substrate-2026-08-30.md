---
artifact_schema: 1
artifact_uuid: 29d2fd09-e708-47b0-9c59-77f6ef792df2
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation Phase 1 — extension substrate

## Why

Projection definitions currently accept arbitrary assertion objects, but production persistence is
split across scalar-specific `TypedAssertion` and event-specific `TypedEventAssertion` schemas.
The admission contract is likewise tested but has no authoritative generic write path. Another
domain can demonstrate a fold in memory, but it cannot yet use Menhir as the durable foundation
described by the core-promotion roadmap.

## Scope

Build the smallest durable and composable substrate needed by a non-scalar domain:

- a core-owned immutable assertion envelope with extension-owned typed payload;
- source-bound admission at the authoritative assertion write;
- an authoritative ordered mutation journal/outbox record in that same write transaction;
- deterministic host composition for evidence, admission, codecs, repositories, projection
  definitions, View kinds, and writer ownership;
- additive read adapters for current scalar/event stores rather than a forced migration.

Phase 1 does not schedule or run projection workers, materialize projections, publish installed
state or definition hashes, cut over scalar/event writes, migrate physical View identity, build
dynamic plugin loading, define dependency/descriptor/package registration, or define the final
public package surface. Runtime materialization and hash publication belong to Phase 2.
Dependency, descriptor, package, and general extension registration belong to Phase 4.

## Required architecture decisions

Before the generic schema or repository is implemented, record an accepted ADR that settles all of
the following as one assertion/current-set lifecycle contract:

- assertion identity and semantic current-set identity, including every field in each identity;
- whether a subject may be rebound and, if so, whether rebinding creates a new assertion,
  current-set, or both;
- the separate meanings of interpretation version and payload schema version and which changes
  require a new assertion or current-set;
- behavior when two writers using the same interpretation version disagree;
- tombstone/removal representation, ordering, retention, export, and erasure behavior;
- the unique namespace-bound current head and its transaction fence, including concurrent replace,
  remove, and replay behavior;
- readiness refusal when stored, active, or replayed records require mixed interpretation versions
  that the active reader/projector cannot compare deterministically.

The ADR must define database constraints and transaction predicates, not only object-model names.
Projection-definition retirement is a separate concern: retiring a definition must not rewrite,
remove, or change the identity/currentness of an assertion or its journal history.

## Proposed design

### 1. Canonical namespace and identity fences

Canonical namespace is required on every generic write and is part of all logical identities:

- source and source lookup identity;
- grant identity and source/grant binding;
- assertion ID, semantic identity, and current-set identity;
- payload and envelope content hashes;
- uniqueness constraints, current-head keys, journal streams/sequences, and transaction fences;
- projection input and logical target identity.

No generic write may infer, omit, or persist a legacy default namespace alias. Legacy default
aliases may be accepted only on reads, where they are resolved to one canonical namespace before
lookup and before any equality, authorization, or fence comparison. Alias resolution that is
missing, ambiguous, cyclic, or changes during a fenced operation fails closed. Physical View
identity is not migrated in Phase 1; adapters may map canonical logical targets to existing
physical View identities without claiming those physical identities have become namespace-native.

Source resolution must use a database-constrained, namespace-bound key that is actually unique.
Use canonical `(namespace, turn_key)` unless the source kind has an ADR-approved key with equivalent
durable uniqueness. Resolution must execute against the durable store, require exactly one matching
row, and refuse admission on zero or multiple rows. Display IDs, optional aliases, or a query whose
uniqueness exists only in application code are not valid source resolvers.

### 2. Core envelope and canonical encoding

Use a core-owned durable projection-assertion envelope. Core guarantees namespace-bound identity,
provenance binding, temporal stamps, immutable admission evidence, current-set linkage, canonical
bytes, collision detection, replay, and journal linkage. Extensions own payload meaning through a
registered codec for one `(assertion_type, purpose, payload_schema_version)` tuple.

The domain-neutral envelope contains:

- canonical namespace, stable assertion ID, assertion type, and purpose;
- namespace-bound source identity, source kind, admitted grant identity, and decision snapshot ID;
- subject identity, valid time, learned time, and interpretation version;
- payload schema version, canonical payload bytes, and payload hash;
- ADR-defined semantic/current-set identity and predecessor, replacement, or tombstone fields;
- codec ID/version and active admission policy ID/version;
- canonical assertion-content bytes, deterministic content hash, and journal stream/sequence
  linkage.

Canonical encoding contract:

- Canonical payload and assertion-content bytes are RFC 8785 JSON encoded as strict UTF-8. The
  assertion-content document has exactly these logical inputs: canonical namespace, assertion ID,
  assertion type/purpose, namespace-bound source lookup key and resolved source ID, requested grant
  ID, subject, valid/learned time, interpretation version, payload schema and codec identity,
  requested authority, ADR-defined current-set operation/predecessor/tombstone inputs, and the
  canonical payload value. It excludes admitted authority, decision IDs/results, current-head and
  journal results, `content_hash`, and database-local IDs. Field names are fixed by the envelope
  schema; optional values are either present with their defined value or absent as the schema
  specifies. Producers do not add semantically empty defaults.
- The envelope content hash is lowercase hex SHA-256 over the ASCII domain separator
  `menhir.assertion-envelope.v1`, one zero byte, and the canonical assertion-content bytes. The
  payload hash is SHA-256 over `menhir.assertion-payload.v1`, then zero-byte-delimited canonical
  namespace, assertion type, purpose, decimal payload schema version, and canonical payload bytes.
  Zero bytes are forbidden in all identity components. Canonical namespace therefore participates
  directly in both hashes, and equal payload text under different codec ownership cannot collide.
- An accepted assertion-content document is at most 256 KiB and its payload is at most 128 KiB,
  measured on canonical UTF-8 bytes. The root JSON value has depth 1 and each nested object/array
  adds 1; maximum depth is 32. Maximum recursive total object members plus array elements is 4096,
  maximum members in one object is 1024, and every string value or member name is at most 16 KiB in
  UTF-8. Integer values are restricted to `[-9007199254740991, 9007199254740991]`; codecs use
  canonical strings for wider integers or exact decimals. Limits are checked before unbounded
  allocation and again on codec output.
- Identity strings and member names must be Unicode NFC and contain no zero byte or control code
  point. Temporal fields use UTC RFC 3339 with uppercase `T`/`Z` and exactly six fractional digits.
- Invalid UTF-8, a byte-order mark, duplicate object member names, non-canonical number or escape
  forms, non-finite numbers, out-of-range integers, lone surrogates, depth/member/byte overflow,
  unknown envelope fields, and codec/schema mismatches fail closed before durable admission.
- Each tuple has exactly one host-registered codec owner. A codec validates the payload schema,
  canonical namespace context, allowed scalar ranges, and canonical round-trip. Decode followed by
  encode must reproduce the accepted bytes exactly; a codec may not silently normalize or repair an
  invalid submitted form.
- Persistence preserves the exact accepted canonical payload bytes, payload hash, canonical
  assertion-content bytes, and content hash. Reads, replay, export, retention processing, and
  erasure audit use those stored bytes/hashes rather than decode-and-re-encode approximations. If
  an erasure rule permits payload destruction, the ADR must specify the immutable tombstone,
  retained hashes and journal evidence, authorization, and proof that erased bytes cannot be
  returned.

The exact graph labels and relationships are documented in `.agent/data_models.md`. Existing scalar
and event nodes remain authoritative for their paths and are not rewritten into this envelope.

### 3. One authoritative `admit_and_record` protocol

The host owns one `admit_and_record` operation implemented through the repository's `execute_write`
transaction boundary. Extensions do not load grants, call admission, write assertions, or append
journal entries independently.

An immutable host mapping keyed by `(assertion_type, purpose)` selects all of the following as one
configuration entry:

- the namespace-bound source resolver and its unique key shape;
- the grant authority and exact source/grant binding fields;
- authority ordering and comparability semantics;
- admission policy ID/version and policy configuration version;
- interpretation-version compatibility rule;
- payload codec owner/schema versions, repository owner, and journal routing identity.

Startup refuses duplicate, missing, partially owned, or internally incompatible entries. Request
data may select a registered type/purpose but may not supply or override any resolver, authority,
policy, codec, version, repository, or routing component.

Within one `execute_write` transaction, the protocol must:

1. Canonicalize the namespace and validate IDs, request shape, canonical bytes, codec ownership, and
   hard limits without persisting anything.
2. Resolve the durable source by its namespace-bound unique key and require exactly one row.
3. Load the durable grant, source/grant binding, revocation state, ingress ceiling, policy inputs,
   current-set head, and journal sequence head. Establish write-conflict fences over every loaded
   state that can affect the decision or resulting current set.
4. Evaluate the mapped authority semantics and policy. Record the requested authority as submitted.
   First clamp any upward request to the ingress ceiling and record the clamp. The mapped policy may
   then reject, reinterpret, or lower that ceiling-bounded candidate but may never raise it. Among
   otherwise well-formed fresh requests, admission refuses persistence only for missing
   source/grant, namespace or binding mismatch, incomparable authority, or mapped policy rejection
   (including a revoked grant). An upward request by itself is not a rejection.
5. Allocate/update the ADR-defined unique current head and the namespace-bound monotonic journal
   sequence, then atomically write the immutable decision snapshot, immutable assertion envelope,
   current-set/head mutation, and authoritative journal/outbox entry.
6. Commit only if all source, grant, revocation, policy-input, current-head, and sequence fences
   still match. A concurrent change causes transaction retry from fresh durable state or a visible
   conflict; it may never commit a decision calculated from stale state.

The immutable decision snapshot records the resolved source and grant IDs/versions, requested and
admitted authority, ingress ceiling, clamp reason, comparability result, policy/configuration and
interpretation versions, decision reason, and the durable fence tokens observed. It is evidence of
the historical decision and is never recomputed in place.

Replay and collision semantics are exact:

- Exact replay means the same canonical namespace, assertion ID, and content hash. It returns the
  original assertion, decision snapshot, current-set result, and journal linkage idempotently,
  without creating a second decision or journal entry and without re-evaluating current grant,
  revocation, codec, or policy state.
- The same namespace/assertion ID with different canonical bytes or content hash is an identity
  collision and refuses visibly. A matching hash with non-matching stored bytes is also a collision,
  not a replay.
- A different assertion ID or ADR-defined semantic assertion identity is a new semantic assertion.
  It evaluates against current durable source, grant, revocation, codec, policy, interpretation, and
  current-head state even when its payload resembles a previous assertion.
- Later grant revocation, policy/configuration upgrade, codec upgrade, or interpretation upgrade
  never mutates the old assertion or decision snapshot and does not break exact replay. It applies
  to every new semantic assertion. Unsupported or mixed versions refuse readiness/admission as the
  ADR specifies; they do not silently fall back to an older interpretation.

### 4. Authoritative mutation journal/outbox

The ordered mutation journal/outbox is not a later integration task. Its producer is part of the
same Phase 1 assertion transaction and its durable entry is the sole authority for downstream work.
An immediate in-process callback, queue publish, or best-effort dispatch may reduce latency but is
never evidence that a mutation exists and is never a substitute for journal consumption.

Each entry contains at least:

- canonical namespace, namespace-bound journal stream, and strictly increasing sequence;
- assertion ID/content hash, decision snapshot ID, operation, and committed time;
- assertion type, purpose, payload schema version, interpretation version, codec identity, and
  projection-input routing identity;
- the eligible projection-definition routing IDs/versions fixed for that mutation, or an immutable
  definition-set identity from which that exact set can be recovered;
- old and new semantic/current-set target and head identities, including tombstone/removal state,
  or sufficient immutable mutation data to reproduce that transition without rereading mutable
  source/grant/policy state;
- dispatch/consumption metadata kept separate from the immutable mutation facts.

Database constraints enforce one entry per assertion mutation, unique `(namespace, stream,
sequence)`, and a unique fenced sequence head. Exact assertion replay reuses the original entry.
Concurrent assertions against one current set must serialize through the current-head and sequence
fences so consumers observe the committed order.

### 5. Trusted host composition and writer ownership

Phase 1 adds one explicit host composition object with immutable registries/mappings for evidence
kinds, admission mappings/policies, codec ownership, repository ownership, projection definitions,
View-kind ownership, namespace aliases, and the writer census. Startup rejects duplicate IDs,
unresolved aliases, missing output View kinds, unowned or multiply owned generic types, missing
constraints, and projection inputs that have no valid authoritative writer.

The writer census is a checked, machine-readable host artifact with at least the following entries
until Phase 4:

```yaml
writer_census_schema: 1
claims:
  - assertion_family: scalar
    authority_owner: legacy_scalar_repository
    currentness_owner: legacy_scalar_repository
    generic_write_allowed: false
    drain_state: not_started
  - assertion_family: event
    authority_owner: legacy_event_repository
    currentness_owner: legacy_event_repository
    generic_write_allowed: false
    drain_state: not_started
  - assertion_family: generic_registered_types
    authority_owner: generic_assertion_repository
    currentness_owner: generic_assertion_repository
    generic_write_allowed: true
    drain_state: exclusive
```

Validation requires exactly one authority owner and one currentness owner per registered assertion
type, forbids a type from appearing in both a legacy and generic writable family, and proves that
every generic `(assertion_type, purpose)` has only the generic write path. Scalar/event adapters are
read-only from the generic path. Scalar and event remain explicitly legacy authority/currentness
owners through Phase 1; no generic transaction may dual-write their stores. A claim that admission
or currentness is universal must wait until the legacy writers are drained and the census changes
under the Phase 4 cutover contract.

Materializer ownership and installed-state/definition hash publication are intentionally absent
from Phase 1 composition and arrive with runtime projection execution in Phase 2. Dependency,
descriptor, package, discovery, and public extension registration are Phase 4 concerns.

### 6. Compatibility adapters and fixture

Wrap existing scalar and event projection sources behind the common read contract without changing
their durable schema, authority/currentness ownership, write path, or physical View identity. Add
one deliberately small non-scalar fixture with a real registered codec, admission mapping,
repository ownership entry, constraints, writer-census entry, projection input route, and View kind.
The fixture proves the entire generic contract but does not add investigation/personality vocabulary
to `src/menhir` or claim production domain semantics.

## Alternatives considered

- Reuse `TypedAssertion` for every domain: rejected because its attribute/scope/value-kind/operation
  schema encodes scalar meaning.
- Require every extension to create a private repository: possible, but it would duplicate source
  binding, namespace isolation, replay, immutable admission, and journal guarantees in every domain.
- Build a general plugin manager now: rejected until two hostile domains prove what composition
  metadata is actually necessary and until Phase 4 owns dependency/package registration.

## Risks and controls

- A supposedly generic payload becomes an unbounded blob: enforce canonical encoding plus byte,
  depth, string, member, and element limits before persistence.
- Dual assertion stores drift: enforce the writer census, read-only adapters, and no-overlap startup
  validation; no path writes both stores implicitly.
- Admission becomes check-then-write: use one transaction, fence every mutable decision input, and
  test source/grant/current-head changes at the final write boundary.
- Namespace defaults fork identity: require canonical namespaces in every logical identity and hash;
  permit aliases only on fenced reads while preserving supported physical View identities.
- Journal dispatch is mistaken for authority: make the transactional journal record authoritative
  and make immediate dispatch disposable and replayable.
- Version upgrades reinterpret history: preserve original bytes and decision versions, define exact
  replay, and refuse unsupported mixed-version readiness.

## Validation

All tests below exercise production registry/repository paths rather than fixture-only bypasses.

- Namespace and source resolution: canonical and legacy-default aliases read the same logical
  record; a new write persists only the canonical namespace; namespace participates in source,
  grant, assertion, current-set, hash, uniqueness, sequence, and fence keys; zero/multiple source
  rows refuse; deliberately duplicated `(namespace, turn_key)` data or constraint creation fails
  visibly; equal turn keys in different namespaces do not collide.
- Admission semantics: an upward request records requested authority and commits at the ingress
  ceiling; lower/equal requests commit unchanged; missing source/grant, mismatched namespace or
  binding, incomparable authority, and policy rejection (including revocation) persist neither
  assertion, decision, current-head mutation, nor journal entry.
- Transaction fencing: concurrent source replacement, grant rebind/revocation, ceiling change,
  policy-input change, current-head replacement/removal, and journal sequence allocation either
  retry from fresh state or conflict; no stale decision or duplicate sequence commits.
- Replay and upgrades: exact replay before and after grant revocation, policy upgrade, codec
  upgrade, and interpretation upgrade returns the original snapshot and journal linkage once; same
  ID with different bytes/hash refuses; a new semantic assertion uses current versions/state;
  unsupported mixed versions refuse readiness/admission.
- Current-set races: concurrent replace/replace, replace/remove, remove/remove, and exact replay
  prove the ADR-defined unique head, monotonic sequence, tombstone semantics, and deterministic
  winner/conflict behavior.
- Codec and envelope: canonical round-trip and known SHA-256 vectors; namespace changes both hashes;
  byte-order marks, invalid UTF-8, duplicate keys, non-canonical numbers/escapes, unknown fields,
  schema/codec mismatch, lone surrogates, and each byte/depth/member/string/element limit at
  boundary and boundary-plus-one fail closed without partial persistence; stored/exported bytes
  match input.
- Writer census and composition: machine-readable census parses; missing, duplicate, overlapping,
  legacy-plus-generic, and unowned writer claims refuse startup; scalar/event generic writes refuse;
  the non-scalar fixture can write only through `admit_and_record`; universal-admission readiness
  remains false while any legacy writer is not drained.
- Journal/outbox: assertion, decision, current-head mutation, and one sequenced entry commit or roll
  back together; routes and old/new targets are immutable; concurrent entries are ordered; replay
  does not append; dropped/immediate dispatch can be reconstructed solely from durable entries.
- Real Neo4j constraints: install and inspect actual uniqueness/head/sequence constraints, then run
  canonical create, alias-aware read, duplicate source refusal, concurrent fenced admission, exact
  replay, ID/content collision, and current-head races against the actual engine.
- Retention, export, and erasure: retention cannot orphan decision/journal/current-set evidence;
  export reproduces canonical namespace, bytes, hashes, versions, source/grant snapshot, and ordered
  journal linkage; ADR-authorized erasure removes or renders inaccessible exactly the allowed bytes,
  leaves the required tombstone/hash/audit evidence, and prevents replay/export from leaking erased
  payload while preserving collision and ordering guarantees.
- Compatibility: current scalar/event behavior remains unchanged, adapters cannot write, physical
  View identity is not migrated, projection retirement leaves assertions/journal history intact,
  and no default registry gains test-fixture vocabulary.

## Exit gate

Phase 1 exits only when all of the following are true:

- the assertion/current-set lifecycle ADR is accepted and covers identity, rebinding, interpretation
  versus payload schema, same-version disagreement, tombstones/removal, unique head/fence,
  mixed-version refusal, retention/export/erasure, and projection-retirement separation;
- canonical namespace is mandatory and proven in every specified logical identity, hash, uniqueness,
  sequence, and fence, with alias-aware legacy reads and exactly-one namespace-bound source lookup;
- the single host-owned `admit_and_record` `execute_write` protocol atomically commits its immutable
  decision, assertion, current-head mutation, and ordered journal entry, with the defined clamp,
  refusal, conflict, replay, collision, new-assertion, revocation, and upgrade behavior;
- canonical encoding, codec ownership, hard resource limits, domain-separated hashes, byte
  preservation, constraints, and fail-closed handling are implemented and covered at real storage
  boundaries;
- the machine-readable writer census proves scalar/event legacy exclusivity and generic-type generic
  exclusivity, and no universal-admission claim is made before legacy drain;
- one non-scalar fixture is admitted from durable evidence, persisted, read, exactly replayed, and
  routed to a registered projection target using only domain-neutral core fields;
- Phase 1 composition contains only evidence/admission/codec/repository/projection-definition/
  View-kind ownership and writer census, with Phase 2 and Phase 4 responsibilities absent;
- the concrete namespace, concurrency, replay/upgrade, current-set, codec-limit, census, constraint,
  journal, retention, export, erasure, and scalar/event compatibility tests pass under central
  verification.

## Docs to update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/memory-governance.md`
- the assertion/current-set lifecycle and storage/admission ADR
- the writer-ownership manifest and ordered mutation-journal contract
- `CHANGELOG.md`
