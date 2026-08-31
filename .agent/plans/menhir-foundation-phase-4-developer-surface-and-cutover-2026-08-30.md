---
artifact_schema: 1
artifact_uuid: 6d97ada4-6753-4dd1-9746-e2ef9e8b5702
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation Phase 4 — developer surface and cutover

## Why

After two hostile domains prove the contracts, Menhir needs a small stable way to author, register,
test, operate, and version extensions. Production activation must then replace compatibility paths
without creating duplicate Views, losing work, changing physical identities accidentally, or
allowing mixed versions to bypass the lifecycle fence.

## Scope

- promote only the interfaces proven by scalar, investigation, and personality;
- provide deterministic startup registration, compatibility rules, documentation, examples,
  distributable artifacts, and a test kit;
- shadow, backfill, activate, and eventually consolidate the generic projection runtime;
- preserve existing data, physical View keys, history, and writers throughout mixed-version
  operation;
- make each rollout gate reproducible from durable evidence tied to the exact deployed code and
  runtime semantics.

Dynamic discovery, third-party code sandboxing, marketplace distribution, and an in-place physical
default-namespace key rewrite remain out of scope. If supported existing physical spellings cannot
be reused deterministically, a separately approved physical namespace migration is a prerequisite
to this phase, not cleanup performed by this phase.

## Pre-Expand prerequisites

### 1. Physical-key compatibility contract and live inventory

Before Expand, publish a versioned physical-key compatibility contract and a live inventory from
the production graph. The contract must define, for every promoted View kind and key component:

- the logical namespace identity and every supported physical spelling, including `None`, empty,
  omitted/default, and any explicit default aliases;
- equivalence rules, precedence, and the exact deterministic choice when more than one supported
  spelling exists;
- how mixed rows are detected and handled when equivalent logical identities use different physical
  spellings;
- supersession and retirement rules, including which spelling remains writable and which spellings
  remain read-compatible;
- the physical key used by create, update, close, replay, repair, and stale-worker checks;
- typed refusal behavior when a row is ambiguous, unsupported, or corrupt.

The inventory must record counts and representative identifiers for each spelling, alias collision,
mixed-row shape, superseded row, retired row, unsupported spelling, and ambiguous logical identity.
It must include a reproducible query/version, graph watermark, exact observation interval, target
digest, total target count, and explicit error denominator.

The compatibility adapter must deterministically reuse a supported existing physical spelling for
the logical identity. It must not create a canonical replacement merely because another spelling is
preferred. If one deterministic supported spelling cannot be selected without merging, rewriting,
or splitting physical identities, stop: a physical namespace migration must complete as a separate
prerequisite phase before Expand.

### 2. Canonical extension descriptor and runtime digest

Every extension must provide one required, immutable descriptor with:

- extension ID and extension version;
- supported host API version range;
- named and versioned assertion/evidence codec schemas;
- registrations sorted by canonical registration key;
- a projection semantic hash for each projection definition;
- materializer and hash-adapter identities and versions;
- semantic configuration that can change admission, folding, targeting, hashing, materialization,
  compatibility, or lifecycle behavior.

Define and version the descriptor digest schema. Canonicalization must use UTF-8 JSON with sorted
object keys, registrations sorted by canonical registration key, schema-defined array ordering,
normalized integer/boolean/null values, and rejection of floats or unspecified values. Hash the
canonical bytes with SHA-256 and store the algorithm and digest-schema version beside the digest.
No process-local name, import order, path, timestamp, or secret may affect the digest.

The host must also compute a runtime digest over the ordered extension descriptor digests plus host
API version, lifecycle implementation identity/version, compatibility-adapter identity/version,
and semantic host configuration. Startup must refuse collisions, missing dependencies, unsupported
host API versions, inconsistent projection/materializer pairs, unknown digest schema versions, or a
runtime digest not declared by the immutable release/image manifest. Readiness stays false until the
release/image identity, descriptor digests, and runtime digest match the deployment's signed or
otherwise immutable release attestation.

## Developer surface

### 1. Stable API layers

Publish three separate namespaces with explicit compatibility policies:

1. Extension-author API: evidence-kind definitions, admission request/policy types, assertion
   envelope/codec protocols, projection definitions/outcomes/targets, View-kind registration,
   materializer/hash protocols, descriptor construction, and typed diagnostics.
2. Host-integration API: composition, descriptor loading, release binding, readiness, lifecycle
   transaction integration, scheduler integration, and authority/fence checks.
3. Test-kit API: fake and real-Neo4j harnesses, fixtures, contract assertions, corrupt-state builders,
   and receipt/attestation validators.

Stable symbols use semantic versioning. Removing or incompatibly changing an extension-author or
host-integration symbol requires a major host API version. Deprecations must name the replacement,
first deprecated version, last supported version, and minimum two-minor support window. Test-kit
helpers may add capabilities in a minor release, but behavior used by published contract tests may
change incompatibly only in a major release. Private infrastructure is not re-exported, and tests
must reject examples or temporary extensions that import it.

### 2. Registration and diagnostics

Registration is explicit and host supplied. It does not discover packages or execute
request-provided code. The host validates the canonical descriptor, publishes descriptor and runtime
digests, and exposes the same values in readiness, worker telemetry, lifecycle receipts, and rollout
receipts.

Corrupt or unsupported state must resolve to an evidence-bearing typed diagnostic, never an
uncaught parse error, silent fallback, or generic mismatch. Each diagnostic includes a stable code,
extension/registration/definition identity, logical and observed physical identity when safe,
expected and observed schema/adapter versions, graph watermark or transaction identity, release and
runtime digests, evidence/query reference, retryability, and prescribed operator disposition.
Diagnostics must distinguish ambiguous aliases, mixed rows, retired spelling, unsupported codec,
hash mismatch, stale generation, unavailable authority, and malformed state.

### 3. Authoring and distribution kit

Ship:

- a minimal extension template;
- investigation and personality examples;
- fake and real-Neo4j lifecycle harnesses;
- fold-law, replay, namespace, admission-ceiling, stale-worker, freshness, corrupt-state, and receipt
  assertions;
- a compatibility matrix and version-bump guide;
- operations guidance for queue/freshness diagnostics and safe disablement;
- wheel and sdist builds with bundled schemas, templates, metadata, and type information required at
  runtime.

Acceptance must build both wheel and sdist, install each into a clean environment outside the source
checkout, and run the public API, bundled asset/metadata, and standard harness suites from the
installed artifact. A separate temporary extension package must use only documented public imports,
install against each artifact, register through the host-integration API, and pass startup,
lifecycle, replay, and diagnostic contract tests. Tests must not rely on the repository root, an
editable install, undeclared files, or private imports.

### 4. Definition retirement and safe disablement

Definition removal is a durable protocol, not omission from local composition. While the extension
code and exact published adapters are still installed, publish a higher retirement generation tied
to the descriptor/runtime digest and release identity. The journal consumer stops enrolling new
present work for that definition, snapshot-fences the final assertion watermark, retires every known
present target through the lifecycle commit, certifies the canonical absent state, and drains all
claimed, pending, retry, and quarantine work for the retiring generation.

After the final census and work drain reconcile, publish a durable definition tombstone containing
the last active definition version/digest, retirement generation, target-set digest, final journal
watermark, receipt digest, and retention disposition. A host may omit the extension code only when
its runtime manifest contains and validates that tombstone; omission of an active definition still
refuses readiness. Retirement never removes durable sources, admission decisions, assertions, or
historical receipts and is distinct from assertion removal.

Reinstallation must publish a new active definition generation compatible with the tombstone,
resume journal consumption after the recorded watermark, census authoritative assertions, and
rebuild through ordinary lifecycle work. Same-version semantic drift, tombstone deletion, or silent
reactivation refuses readiness. Safe-disablement tests cover crash/restart at every retirement step,
in-flight old workers, retained quarantined work, fresh-host omission, retired-host omission, and
reinstall convergence.

## Production invariant

Every production worker materializing a registered projection target must use the shared-current
definition, current target generation, authoritative namespace identity and selected supported
physical spelling, current durable writer authority, and one atomic lifecycle commit; rejection
leaves the prior certified projection and all immutable evidence/assertions intact.

Authority is the published definition state plus `ProjectionWorkState` and the durable
per-definition/cohort writer-authority record at the atomic commit boundary. Both legacy/direct and
lifecycle mutation transactions must read and compare the same authority generation. Observable
refusal for stale, unpublished, mismatched, corrupt, unauthorized, or already-completed work is a
typed lifecycle failure with no partial projection mutation.

## Durable rollout evidence

Every stage, including a failed or aborted stage, emits an append-only attested receipt. A passing
receipt must contain:

- stage, definition/cohort, result, receipt schema version, and previous receipt digest;
- immutable release ID and image digest for host, API replicas, schedulers, and workers;
- descriptor/registration digests and runtime digest;
- graph authority identity/generation and writer-authority identity/generation;
- input and output graph/queue watermarks;
- target-set digest, total target count, passed/failed/skipped counts, and the exact error
  denominator;
- worker identities and immutable worker release/image attestations;
- exact UTC start/end interval and any excluded interval;
- evidence/query, telemetry, census-manifest, and approval artifact digests;
- receipt signer/attester identity, canonical receipt digest algorithm/version, and receipt digest.

The verifier rejects a receipt if any replica or worker is unattested, a digest changes inside the
claimed interval, watermarks cannot bound the target set, counts do not reconcile to the denominator,
or excluded time hides a violation. A deployment, authority change, semantic configuration change,
or relevant telemetry gap ends the interval and restarts the applicable continuous window.

`First mutation` means the earliest durable production change made for this cutover: a generic
backfill/lifecycle projection mutation, a writer-authority flip, or a compatibility-state mutation,
whichever occurs first. Before first mutation, rollback may disable the default-off path. After first
mutation, rollback is a roll-forward to a certified release or a verified reverse-generation
procedure with its own receipt; blind restoration of an old image is prohibited.

## Rollout stages

### Expand

- deploy public composition, release/runtime attestation, compatibility adapter, telemetry, and
  generic scheduler default-off;
- publish definitions and run read-only desired-state/freshness and physical-key audits;
- retain every existing scalar/event writer and physical View key;
- add an emergency disable switch that stops new generic work without deleting state;
- publish a versioned writer-census manifest and enforce its source and runtime guards.

The writer-census manifest is an Expand exit artifact. Each row names the mutation sink, source
callsite or database trigger, deployment/process, activation gate, transaction/fence behavior,
telemetry tag, owner, and disposition (`fence`, `remove`, `migrate`, or documented non-writer). It
must cover repositories, services, maintenance tasks, ingest workers, repair/backfill scripts,
direct Neo4j helpers, deployment jobs, database procedures/triggers, manual administrative paths,
and external processes sharing the graph. Every discovered sink has one owner and disposition; no
unknown or unowned row may pass.

A source/AST guard fails when a production scalar mutation sink lacks a census ID and telemetry tag
or uses an unapproved helper. Runtime telemetry reports attempted, allowed, and refused mutations by
census ID, release/image, definition/cohort, authority generation, and fence result. Unknown tags and
untagged sink activity fail the gate; source guards alone cannot prove external or administrative
paths.

Exit gate: all replicas are ready on the same attested release/image and runtime digest; the
physical-key inventory passes; shadow audits show no cross-namespace or duplicate-current
corruption; the complete writer-census manifest is owner-approved; AST/source guards pass; and
runtime telemetry accounts for every observed mutation. Rollback: disable generic scheduling. This
rollback rule applies only while first mutation has not occurred.

### Backfill

- reconcile every authoritative assertion target into a read-only desired-state and work inventory
  in bounded resumable generations;
- retain observed before-images, generation records, and cursors needed to prove coverage and rerun
  safely;
- compute expected present and absent hashes without mutating production Views, lifecycle receipts,
  or freshness certificates;
- compare generic desired outcomes/hashes with existing scalar results;
- move the high watermark until a bounded final delta closes under live writes rather than treating
  a fixed initial snapshot as completion.

Exit gate: the attested target census is complete to the output watermark, target/count digests
reconcile, the final moving-watermark delta is empty or within the explicitly certified atomic
closure, and repeated backfill produces zero new unexplained work. Failure response: stop at the
durable cursor, repair the adapter/definition, bump the definition when semantics changed, and
resume in a new generation. Never mint a lifecycle success receipt or freshness certificate for a
legacy-written View. Any durable staging mutation still invokes the post-first-mutation rollback
rule and preserves all audit evidence.

### Drain

Drain has three ordered gates:

1. Deploy fence-aware legacy writers to every census deployment. Both legacy/direct and lifecycle
   transactions must check the same durable per-definition/cohort authority generation atomically
   with their mutations. Remove or prohibit every binary, script, trigger, administrative path, or
   external process that cannot perform that check; an unfenceable writer may not remain deployable
   or runnable.
2. Prove mixed N-1/N operation. N-1 fence-aware writers may continue only while legacy authority is
   current; N lifecycle writers must refuse projection mutation. Attempts from removed,
   unfenceable, unknown, or wrong-generation writers must be refused and attributed by telemetry.
3. Atomically flip the durable per-definition/cohort authority generation from legacy to lifecycle.
   The flip transaction records release/runtime digests and fence generation. Legacy/direct
   transactions must then refuse, and only after that refusal is observed may lifecycle writes be
   activated for the cohort. The lifecycle worker then applies the staged Backfill inventory in
   bounded generations, installing and certifying present and absent state only under lifecycle
   authority.

Exit gate: authority has been lifecycle-owned continuously for at least 7 days and two complete
cycles of every asynchronous writer/repair/backfill schedule, whichever is longer. During the exact
attested interval there are zero allowed legacy/direct mutations, zero unknown/untagged attempts,
all attempted legacy writes are fenced and attributed, all workers are release-attested, parity and
freshness failures are zero, and lifecycle refused/failed counts reconcile to the exact denominator
with zero unexplained failures.
Any violation, relevant telemetry gap, deployment, or digest/authority change restarts the window.

Rollback after authority flip or any other first mutation is roll-forward to a certified
fence-aware release or the verified reverse-generation procedure followed by an atomic authority
flip. Never restore a retained old image unless its release attestation proves it checks the current
authority generation and the rollback procedure has a passing receipt.

### Verify

- hold projection and realization coverage clean for 7 continuous days after Drain passes;
- exercise create, correction, removal, replay, restart, version upgrade, stale worker, physical
  namespace alias, corrupt state, and moving-watermark cases with disposable subjects;
- compare recall-visible surfaces and provenance before and after activation;
- exercise authority-flip races, direct/bypass attempts, mixed N-1/N writers, release-attestation
  mismatch, reverse-generation, and scalar regression cases.

Exit gate: the 7-day attested interval has zero unexplained parity/freshness failures, no
duplicate-current Views, reconciled target counts/digests and error denominator, and passing
acceptance evidence for every required case. A violation, relevant telemetry gap, deployment,
runtime digest change, or graph/writer-authority change restarts the full window; elapsed time alone
never passes the gate.

### Enforce

- make the lifecycle path authoritative for promoted definitions and cohorts already proven by
  Drain and Verify;
- refuse direct production writes that bypass the required authority, generation, definition, and
  physical-key fence;
- keep compatibility reads while legacy data spellings remain and emit attributed compatibility-read
  telemetry;
- retain the emergency stop as a scheduler stop, not an authority bypass.

Exit gate: enforcement receipts prove bypass attempts fail atomically and no emergency or repair
path can write without the current authority check. Rollback follows the post-first-mutation rule:
roll forward to the last certified fence-aware release or execute verified reverse-generation and
an atomic authority flip. Do not run mixed authoritative writers.

### Contract

Contract only the legacy `scalar_state` mutation branch after every caller in the writer-census
manifest has moved to the lifecycle path and the Enforce authority remains active. Do not remove or
weaken `scalar_history`, deletion, activation, merge/unmerge, repair, provenance, replay, or recovery
guarantees. Preserve those paths unchanged or migrate each under a separate explicit plan with its
own census, invariants, receipts, and acceptance gates.

Before removal, observe one attested interval of at least 14 continuous days with zero legacy
`scalar_state` mutation attempts and zero compatibility-read hits for the contracted branch. Any
hit, telemetry gap, deployment, runtime digest change, or authority change restarts the interval.
The final receipt must also bind a verified restorable backup, restore procedure, retention period,
terminal writer-census dispositions, and explicit service/data owner approval.

Only then remove the entity-wide `scalar_state` rebuild branch and duplicate closed registration
paths, archive migration tools and compatibility documentation with an explicit terminal status,
and update source/AST guards to prohibit reintroduction. Physical default-namespace key migration
remains a separate approved plan, not cleanup hidden in contraction.

## Acceptance and proof matrix

Acceptance evidence must include all of the following:

- moving-watermark backfill under concurrent writes, including final-delta closure and count/digest
  reconciliation;
- mixed N-1/N fence-aware writers before and after authority flip;
- atomic authority-flip races and legacy/direct/lifecycle bypass attempts;
- refusal of an unattested or wrong-release/image worker and readiness refusal on runtime digest
  mismatch;
- forward generation, failed generation, and verified reverse-generation with preserved immutable
  evidence and receipts;
- scalar create, correction, removal, activation, deletion, history, merge/unmerge, replay, repair,
  restart, and recall-visible regression coverage;
- supported `None`/empty/default aliases, mixed rows, supersession, retirement, and corrupt physical
  identities;
- wheel and sdist clean-install tests, public-only temporary extension tests, bundled
  asset/metadata/API checks, and fake/real-Neo4j harness runs.

Concurrency proof must cover stale/superseded workers, definition publication races, retries,
certification failure after materialization, authority changes between read and commit, and
shutdown/restart during a batch. Deployed proof must name immutable release IDs and image digests for
every replica and worker. If a live violation cannot be exercised safely, record it as unproven; do
not infer runtime behavior from source alone.

## Exit gate

An extension outside core can be built from public documentation, installed from wheel and sdist,
validated at startup, tested against the standard harness, and operated through existing
diagnostics. Runtime behavior is bound to an immutable release/image by canonical descriptor and
runtime digests. Production projections are created only by the current fenced lifecycle authority,
physical identities are reused under the compatibility contract, shadow/backfill evidence is clean,
rollback/reverse-generation is tested, and only the measured legacy `scalar_state` mutation branch
is contracted after its 14-day gate and owner-approved backup.

## Docs to create or update

- public extension-author, host-integration, and test-kit API references
- extension template and testing/packaging guide
- descriptor/digest schema and compatibility/versioning/deprecation policy
- physical-key compatibility contract and live inventory runbook
- writer-census manifest schema, source guard, and runtime telemetry reference
- `.agent/architecture.md` and `.agent/data_models.md`
- operations, authority flip, reverse-generation, backup/restore, and migration runbooks
- default-off feature registry and production acceptance report
- package exports/metadata and `CHANGELOG.md`
