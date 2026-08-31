---
artifact_schema: 1
artifact_uuid: f9853d8d-e8e4-4e98-bc99-a45fcfa1ed15
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation Phase 2 — runtime orchestration

## Why

The durable lifecycle repository can publish definitions, reconcile targets, issue optimistic work
tokens, atomically materialize, certify hashes, and assess freshness. Nothing in the production
runtime currently publishes the scalar definition, marks work from assertion changes, schedules
pending targets, or invokes `ScalarStateProjectionMaterializer`. Correct primitives without a host
path are not yet a usable foundation.

## Scope

Build one generic, bounded, restart-safe projection host around the promoted contracts:

- startup definition publication and validation;
- assertion-to-definition routing and old/new target reconciliation;
- bounded pending-work execution through adapter-owned materializers;
- historical/backfill discovery;
- recovery, telemetry, and operator diagnostics;
- scalar shadow operation before any production write cutover.

## Proposed design

### 1. Runtime composition manifest and publication

Backend composition produces one deterministic runtime composition manifest before readiness. The
manifest contains, for every definition:

- definition identity and version;
- target identity and fold/evaluation contract identity and version;
- every input, target, and canonical-value codec identity and version;
- the output schema identity and version;
- the materializer identity and version;
- the installed-state hash adapter identity and version; and
- the semantic digest of the complete runtime behavior above.

Manifest serialization has fixed field ordering, normalized identifiers, and no process-local or
clock-derived values. The semantic digest is computed from the canonical serialized manifest, not
from class names alone. Any change to a listed behavior component changes the digest. In particular,
a worker must resolve the exact materializer and installed-state hash adapter digest named by the
published definition; a same-name or same-version adapter with a different digest is incompatible.

Startup activates lifecycle schema, composes the entire local manifest, and atomically publishes or
verifies the entire manifest before any worker or scanner starts. Readers must never observe or
accept a partially published manifest. A lower definition version, a same-version/different-digest
collision, an incomplete publication, or an adapter-digest mismatch refuses readiness. A higher
version atomically installs the new manifest entry, invalidates known targets through the lifecycle
repository, and reports the count. Every durable active definition must be present in local
composition with the exact digest; a process missing one refuses readiness rather than silently
skipping its work. Definition retirement and unregistration are not Phase 2 operations and remain
Phase 4 work.

This manifest publishes runtime behavior. It is deliberately distinct from the Phase 4 package or
extension descriptor digest, even if Phase 4 later includes or refers to the runtime digest.

### 2. Ordered mutation consumption and historical census

The Phase 1 transactional ordered mutation journal/outbox is the sole correctness authority for
assertion changes. The assertion write and its journal record are committed in the same transaction.
The record carries enough before/after identity and ordering information to recover same-target
corrections, old-target retirement plus new-target activation for target moves, removals, and
replays. An immediate post-persistence dispatch may reduce latency, but it is optional and may not be
required for correctness or used to skip durable journal consumption.

Consumption is idempotent and ordered per durable consumer cursor. For each journal record, the
consumer resolves all matching definitions, applies every required dirty or retire transition, and
advances the cursor only after those transitions succeed in the same transaction. A crash before
that commit replays the record. A crash after it observes the advanced cursor and does not duplicate
logical work. Same-target corrections increment or otherwise refresh the target generation; target
moves dirty the new target and retire the old target even when both identities share a definition.

A bounded historical census covers assertions that predate the journal, imports, and newly
published definitions. Each census partition uses a database snapshot paired with an upper journal
watermark `W`; its checkpoint records both the partition position and `W`. Results are applied as
idempotent fenced repairs through the same lifecycle transition path. Presence at the snapshot may
dirty a target. Absence may retire a target only if, in the applying transaction, there is still no
matching authoritative assertion and no relevant mutation after `W`. Otherwise the census result
abstains and the ordered journal owns the concurrent change. A manifest generation change also
invalidates the census token. This fence prevents a stale census from falsely retiring a target that
was created, corrected, moved, or reactivated concurrently. Census restart resumes from its durable
checkpoint without treating a partial scan as a complete absence proof.

### 3. Durable time-driven evaluation

Lifecycle state includes generic durable `next_evaluation_at` authority for activation, expiry, or
any other wall-clock transition. A definition with time-driven behavior must declare how an adapter
computes the next boundary; publication rejects a time-driven definition that cannot provide this
authority. Due boundaries become pending work without requiring an assertion mutation, survive
restart, and are cleared or replaced atomically with successful commit.

The worker fixes one `as_of` instant when a token attempt begins. Input loading, evaluation,
materialization, installed-state hashing, certification, and computation of the next boundary all use
that same instant. A retry receives a new attempt and may choose a new `as_of`; an individual attempt
must not observe a moving clock.

### 4. Generic worker and certification surfaces

The worker claims eligible work, resolves the token's shared-current manifest entry and exact adapter
digests, and calls `ProjectionLifecycleRepository.commit`. The materializer receives only the
supplied transaction, token, and fixed `as_of`. It may install, refresh, abstain, or retire exactly
one target and may never commit independently or mutate another target.

Each adapter declares its certification surface as:

- required properties and relationships that define installed state;
- required provenance needed to attribute that state to the definition and input generation;
- the canonicalization and hash algorithm for that required surface; and
- explicitly named advisory properties or relationships that do not contribute to certification.

Required state, required provenance, lifecycle receipt, and certified hash are written and verified
in the same transaction. The installed-state hash adapter rereads or otherwise verifies the exact
required surface after materialization. Any mutation, hash, provenance, or certification failure
rolls back the target attempt. Advisory data is never silently treated as required and cannot make a
target fresh; if an adapter writes advisory data in the transaction, it shares the same rollback.
Stale or already-completed tokens are explicit concurrency outcomes rather than silent success.

Freshness audit returns an independent typed assessment for every requested target using exactly the
terms `fresh`, `stale`, `unavailable`, or `corrupt`, plus structured diagnostic details. `stale`
means a valid assessment found generation, definition, time-boundary, or hash drift. `unavailable`
means the required source or installed state could not be read. `corrupt` means the installed
required surface, provenance, receipt, or stored hash is malformed or internally inconsistent.
Corruption in one target must not abort or erase assessments for unrelated targets.

### 5. Fair bounded scheduler and operations

Add a default-off maintenance task using the canonical scheduler startup and shutdown lifecycle. The
scheduler is bounded by both a per-transaction server timeout and an overall scheduler-run time
budget. It stops claiming new work when the overall budget cannot accommodate another attempt.

Scheduling is fair across definitions, using deterministic round-robin or an equivalent bounded
per-definition quota rather than a single global oldest-first stream. Where concurrent workers can
select the same row, durable claims and expiring leases prevent simultaneous ownership and permit
recovery after worker death. Durable work metadata includes `attempt_count`, `next_attempt_at`, the
last structured error, claim/lease identity and expiry where claims are used, and quarantine state.
Failures use bounded backoff. After the configured attempt or elapsed-age threshold, poison work
remains visible as quarantined diagnostic work but is excluded from the hot eligible set, so it
cannot starve healthy work in the same or another definition. Operators can explicitly retry it.

Diagnostics expose manifest digest and definition versions, queue depth and oldest eligible age per
definition, due time-boundary work, completions, stale-token races, lease recovery, retries,
quarantines, server and scheduler-budget timeouts, last errors, census cursor/watermark progress, and
fresh/stale/unavailable/corrupt assessment counts. Telemetry must identify a definition and target
without embedding assertion payloads or unbounded error text.

### 6. Scalar shadow boundary

Before enabling generic writes, publish and reconcile `typed_scalar.current_state` in read-only
shadow mode. Shadow evaluates desired state and projection coverage, reads the existing scalar View,
and compares its installed hash with the desired canonical hash. It writes no replacement View,
required provenance, lifecycle receipt, certified hash, or lifecycle certificate. Therefore shadow
may report projection coverage and hash parity, but it cannot report clean Realization Coverage or
claim exact installed freshness.

Exact freshness in Phase 2 is proven only by the generic test adapter, whose lifecycle owns its test
installed state. Scalar lifecycle certification and promotion to write mode belong to Phase 4 after
lifecycle owns scalar writes and the defined parity window is satisfied.

### 7. Provisional extension boundary

Before Phase 3 begins, freeze either a provisional extension facade or an explicit public import
allowlist for definition, fold, codec, adapter, and diagnostics contracts intended for extensions.
An AST-based architecture test rejects extension imports outside that boundary. The allowlist is
versioned with the plan and may be narrow; Phase 4 stabilizes the facade and its compatibility
policy. Phase 2 must not imply that internal repository or scheduler types are already public API.

## Risks and controls

- Lost immediate hooks: the ordered journal consumer guarantees replay; the fenced census covers
  pre-journal and newly introduced definition history.
- Duplicate workers: generation and definition fences remain at the atomic commit boundary.
- A slow or poison extension monopolizes maintenance: per-definition fairness, server timeout,
  scheduler budget, bounded backoff, leases, and quarantine keep healthy work eligible.
- Definition drift across replicas: readiness binds every process to the complete runtime manifest
  and exact adapter digests before workers run.
- Adapter side effects escape rollback: the certification surface and required provenance are
  transaction-owned; fault injection after each mutation stage must prove rollback.
- Census races with live writes: snapshot/watermark and absence recheck fences make stale retirement
  abstain while the ordered journal handles the concurrent mutation.
- Wall-clock behavior silently drifts: durable `next_evaluation_at` and one fixed `as_of` per attempt
  make activation and expiry explicit and replayable.

## Validation

- Manifest publication: first atomic publication, exact replay, version upgrade, lower-version
  refusal, behavior drift without version change, exact adapter-digest mismatch, local omission of a
  durable definition, and crash/fault injection at every partial-publication boundary. No worker or
  scanner starts against a partial or mismatched manifest.
- Ordered routing: create, same-target correction, supersession, target move, removal, unrelated
  assertion type, duplicate delivery, and out-of-order delivery refusal. Fault injection covers
  crash before journal append, after assertion-plus-journal commit, after dirty/retire application
  but before cursor commit, and after cursor commit.
- Census recovery: missed historical assertion, partial partition, restart with the same cursor and
  watermark, definition-generation change mid-scan, create/correct/move/remove after snapshot, and a
  concurrent reactivation that must not be falsely retired.
- Time behavior: future activation and expiry become pending from durable `next_evaluation_at`,
  survive restart, use one `as_of` throughout an attempt, and reject a time-driven definition with no
  next-boundary contract.
- Fairness and failure: multiple definitions with unequal queue sizes, two workers and lease expiry,
  repeated poison target, bounded backoff, quarantine and explicit retry, per-transaction server
  timeout, overall scheduler budget exhaustion, and proof that healthy work continues.
- Certification: required property and relationship mutation, required provenance write, receipt,
  hash reread/verification, advisory-only drift, stale generation, completion replay, and fault
  injection after every required stage. Every required-state or provenance failure rolls back.
- Assessment isolation: a corpus containing fresh, stale, unavailable, and corrupt targets returns
  typed per-target diagnostics in one audit; corruption does not abort unrelated assessments.
- Real Neo4j: execute atomic manifest publication, journal consumption and cursor advancement,
  fenced census reconciliation, competing claims, required-surface rollback, receipt, hash
  verification, and typed assessment against the actual transaction engine.
- Shadow: representative scalar projection and hash comparisons produce no View writes, lifecycle
  receipt, certified hash, or certificate and never claim clean Realization Coverage. The generic
  test adapter, not scalar shadow, proves exact fresh assessment.
- Extension boundary: AST fixtures accept every allowlisted import and reject internal repository,
  scheduler, and non-facade imports before Phase 3.
- Telemetry: deterministic assertions cover per-definition queue age/depth, cursor and watermark,
  retries, lease recovery, quarantine, both timeout classes, manifest mismatch, and all four
  assessment counts without payload or unbounded-error leakage.

## Exit gate

Phase 2 exits only when every Validation item above is automated and passing in the central
verification environment and all of the following are true:

- readiness atomically publishes or verifies the complete deterministic runtime manifest, requires
  exact adapter digests and all durable definitions locally, and starts no worker on mismatch;
- the ordered journal/outbox is the demonstrated sole mutation correctness path, with atomic cursor
  advancement and recovery of same-target corrections and target moves;
- the snapshot/watermark census demonstrably cannot falsely retire concurrent changes;
- durable time-boundary work, fixed per-attempt `as_of`, fair claims, bounded retries, quarantine,
  server timeout, and scheduler budget all survive restart and preserve healthy progress;
- one generic test adapter proves transactional required-surface/provenance rollback, canonical
  hash verification, and exact `fresh` assessment without scalar vocabulary;
- scalar remains read-only shadow, emits no lifecycle certificate, and makes no clean Realization
  Coverage or exact-freshness claim;
- typed per-target fresh/stale/unavailable/corrupt diagnostics isolate corruption and required
  telemetry is operator-visible; and
- the provisional extension facade or import allowlist is frozen and AST-enforced before Phase 3.

Failure or deferral of any bullet keeps Phase 2 open. Definition retirement/unregistration, scalar
write ownership and certification, and stable package/extension descriptors remain explicit Phase 4
work and cannot be used to waive a Phase 2 gate.

## Docs to update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/workflows/operations_runbook.md`
- `.agent/workflows/backend-first-mcp.md`
- `.agent/default-off-features.md`
- scheduler protocol/task documentation
- `CHANGELOG.md`
