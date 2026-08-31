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
- deterministic host composition of the existing evidence, admission, projection, View, and
  materialization registries;
- additive adapters for current scalar/event stores rather than a forced migration.

This phase does not schedule projection workers, cut over scalar writes, build dynamic plugin
loading, or define the final public package surface.

## Proposed design

### 1. Storage decision and envelope

Begin with a short ADR comparing a minimal core assertion envelope against fully extension-owned
repositories. Preferred direction: a core-owned durable projection-assertion envelope because core
must guarantee identity, provenance binding, temporal stamps, namespace isolation, immutability,
and replay while extensions own the payload meaning.

The envelope must contain only domain-neutral fields:

- stable assertion ID and assertion type;
- source/evidence identity and admitted grant IDs;
- namespace, subject identity, valid time, and learned time;
- immutable extension payload plus payload schema/version;
- lifecycle/current-set identity needed for supersession or removal;
- deterministic content hash for replay and collision refusal.

The exact graph label and relationship names are decided in the ADR and documented in
`.agent/data_models.md`. Existing scalar and event assertion nodes remain authoritative for their
current paths; adapters expose them to the common projection interfaces.

### 2. Authoritative admission write

The assertion repository loads durable source provenance, evaluates `decide_admission`, and writes
the assertion plus immutable admission decision in one transaction or refuses visibly. Payload
authority is a request only. A missing source, mismatched grant/source binding, incomparable ceiling,
or attempted upward promotion fails before assertion persistence.

### 3. Trusted host composition

Add one explicit startup composition object that receives immutable registries/mappings for:

- evidence kinds;
- admission policy resolvers;
- projection definitions;
- View kinds;
- assertion codecs/sources;
- materialization and installed-state hash adapters.

Startup validation rejects duplicate IDs, missing output View kinds, unowned projection definitions,
materializer/hash mismatches, and unsupported dependency versions. Composition is host-controlled
trusted code; there is no package discovery, remote code loading, or per-request registration.

### 4. Compatibility adapters

Wrap the existing scalar projection definition and stores behind the same read contract without
changing their durable schema or write path. Add one deliberately small non-scalar fixture codec to
prove the envelope, but leave the full investigation semantics to Phase 3.

## Alternatives considered

- Reuse `TypedAssertion` for every domain: rejected because its attribute/scope/value-kind/operation
  schema encodes scalar meaning.
- Require every extension to create a private repository: possible, but it would duplicate source
  binding, namespace isolation, replay, and immutable admission guarantees in every domain.
- Build a general plugin manager now: rejected until two hostile domains prove what composition
  metadata is actually necessary.

## Risks and controls

- A supposedly generic payload becomes an unbounded blob: impose an encoded byte limit and reject
  non-canonical or non-serializable payloads before persistence.
- Dual assertion stores drift: adapters have explicit ownership and parity tests; no path writes
  both stores implicitly.
- Admission becomes check-then-write: use one transaction and test source/grant changes at the final
  write boundary.
- Namespace defaults fork identity: use canonical logical targets and alias-aware reads while
  preserving supported physical identities.

## Validation

- Unit: envelope validation, canonical hash, codec failure, collision handling, registry ownership.
- Admission: untrusted upward request is clamped/refused; extension lowering succeeds; missing and
  mismatched source grants fail closed.
- Persistence: exact replay is idempotent; same ID/different content refuses; source and assertion
  survive View retirement.
- Namespace: two tenants with the same domain subject cannot share assertions or targets; default
  aliases do not bypass the fence.
- Compatibility: current scalar/event suites remain unchanged and no default registry gains test
  fixture vocabulary.
- Real Neo4j: create, replay, collision refusal, and source-bound admission all execute against the
  actual engine and constraints.

## Exit gate

A non-scalar assertion can be admitted from durable evidence, persisted, read back, replayed, and
resolved to a registered projection target using only domain-neutral core fields. Existing scalar
and event behavior remains byte/semantics compatible, and no investigation/personality vocabulary
appears under `src/menhir`.

## Docs to update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/memory-governance.md`
- the storage/admission ADR
- `CHANGELOG.md`
