---
artifact_schema: 1
artifact_uuid: 12069633-3533-4a92-a79b-573006875905
artifact_type: investigation
artifact_status: OPEN
informs: 22082266-930e-4f00-a8f7-e9116fea6d5c
---

# Menhir deployment control-plane architecture specification

## Status, purpose, and authority

This is the non-executable architecture source of truth for the Menhir deployment control-plane
replacement. It exists because implementation planning repeatedly began while cross-boundary
decisions were still open. Each later correction moved authority into another script, receipt, or
repository and exposed another P1 at the seam.

The active consumer is
[`../plans/menhir-deployment-control-plane-architecture-reset-2026-09-08.md`](../plans/menhir-deployment-control-plane-architecture-reset-2026-09-08.md).
That plan is blocked while this investigation is `OPEN`. No phase may be implemented, reviewed as
complete, pushed for CI, or used against a host until all of the following are true:

1. every decision and acceptance item in this specification is closed;
2. a fresh no-context architecture review finds no open P0-P2 defect;
3. this artifact is moved to `COMPLETE` without changing its UUID;
4. the execution plan is rewritten from this specification, reviewed independently, and explicitly
   accepted by the owner.

This document authorizes no production read, mutation, release, installation, key enrollment, or
deployment. Its requirements are normative design constraints. Current executable behavior remains
frozen and must not be represented as compliant merely because it resembles part of the target.

**Decisions closed by owner ADR, not re-openable by review:**

| ADR | Date | Decision | Sections affected |
|---|---|---|---|
| [`0002`](../adr/0002-menhir-production-ingress-ownership.md) | 2026-09-08 | `yawn.deploy` shared Caddy remains the sole `memory.ctharvey.me` ingress; Cloudflared-sole-ingress withdrawn | Closed architectural decision, system boundary, I-13, live-state composite, lane table, ingress/gateway/contraction, adversarial evidence, acceptance ledger |

A review finding that re-litigates a decision in this table is out of scope and must be raised as a
proposed ADR supersession, not recorded as a specification defect.

## Closed architectural decision

Do not rewrite Menhir's application, database, OAuth/MCP, backup, restore, Docker, or ingress data
plane. Replace only the deployment control plane with a strangler cutover:

- one canonical protocol library owns every semantic record and digest;
- one privileged **mutating** entry point owns product and infrastructure transactions;
- one durable root state machine owns admission, locks, rollback, recovery, receipts, and adoption;
- versioned adapters own lane-specific commands but never authorization or transaction state;
- transport wrappers upload exact bytes and copy receipts, without deciding meaning;
- the shared `yawn.deploy` Caddy is the sole public ingress authority for `memory.ctharvey.me`, and
  Menhir owns no ingress route writer (ADR 0002);
- Yawn exposes only versioned read responses and no Menhir mutation capability;
- bootstrap is the only separately authorized control-plane mutation and has a fenced, one-time
  v1-to-v2 handoff;
- v1 is removed after cutover. There is no compatibility fallback or dual-writer interval.

The architecture is intentionally fail-closed. An unknown version, record, transition, algorithm,
adapter, installed artifact, writer, caller, route, or live-state field is an admission failure, not
an extension point.

## System boundary and final ownership

| Component | Final owner | Allowed authority | Forbidden authority |
|---|---|---|---|
| Menhir repository | Protocol schemas/library, release compiler, staging and preflight verifiers, approval client, root kernel, adapters, bootstrap package, generated contracts and tests | Define and verify Menhir release/control-plane semantics | Read mutable checkout bytes into a release; let wrappers or lane scripts reinterpret policy |
| Shared `C:\Users\thron\IdeaProjects\scripts` | Three transport-only PowerShell interfaces | Validate local argument shape, transfer exact files, invoke one fixed remote command, copy one receipt | Classify lanes, approve, adopt, inspect live state, choose bundles, recover, roll back, or install policy |
| `yawn.vps` | Read-only operations gateway and five versioned read operations | Supply one commit-addressed gateway input to the infrastructure package and present kernel-produced read models through fixed read-only commands | Submit, promote, recover, converge, install, delete, rotate, or proxy caller-selected paths/commands |
| `archolith_oauth` | OAuth package source | Supply one commit-addressed wheel/source identity to the release compiler | Operate deployment state or mutate the host |
| `yawn.deploy` | Yawn services plus the shared Caddy ingress, including the `memory.ctharvey.me` vhost | Own the vhost, its Origin CA certificate, Authenticated Origin Pull configuration, route table, and `menhir-proxy` attachment | Own Menhir locks, Menhir release records, Menhir transaction journal, or Menhir bundle GC |
| Shared Caddy ingress (`yawn.deploy`) | `yawn.deploy` | Sole public route authority for `memory.ctharvey.me`, including product and operations allowlists | Be written, templated, reconciled, or transacted by any Menhir lane |
| Host inspection gateway | Menhir infrastructure adapter installs/binds service; `yawn.vps` supplies read-only application code | Listen only on `172.30.0.1:8000`; expose the fixed read API to the verified shared Caddy peer | Bind publicly, trust an address without peer identity, or mutate deployment state |
| Root transaction kernel | Signed bootstrap package and root installation | Sole routine mutating authority after cutover | Self-update from a product package or execute caller-provided code/path/text |
| Owner signing key | Owner workstation and offline backup | Authorize a bounded ticket or bootstrap trust ceremony | Enter the VPS, repository, argv, environment, logs, receipts, uploads, or persistent plaintext storage |

Repository inputs are separated by package type after contraction:

- a **product release envelope** contains exactly one `menhir` application commit and one
  `archolith_oauth` commit;
- an **infrastructure package** contains one `menhir` desired-state commit and one `yawn_vps`
  gateway commit;
- a **control-plane package** contains one `menhir` kernel/protocol/bootstrap commit. It may bind an
  already compiled infrastructure package but cannot absorb or install its payload as product data;
- the shared workspace is transport tooling and is never trusted release/package content;
- `yawn.deploy` is never a Menhir package input. It remains the owner of the `memory.ctharvey.me`
  vhost and the `menhir-proxy` attachment, and is retired only as a Menhir lock, release, journal,
  and GC authority.

Each package records its own exact repository cardinality; there is no union “three-repository
release.” Until these schema migrations and contraction land atomically in the owning implementation
phase, the current four-repository format remains frozen and cannot be partially reinterpreted.

`yawn.deploy` must participate in the writer/bypass census and contraction acceptance even though it
is not a Menhir release input. Because it owns ingress (ADR 0002), the census has both positive and
negative assertions.

Required to remain present and owned by `yawn.deploy`:

- the `memory.ctharvey.me` site block, its Origin CA certificate mounts, its Authenticated Origin
  Pull `client_auth` configuration, and its `/ops/mcp` and product route matchers;
- the `menhir-proxy` network attachment that lets it reach `menhir-prod-app`.

Required to be absent:

- no Menhir release lock, including any use of `/run/lock/menhir-production.lock`;
- no Menhir release authority record, phase journal, snapshot directory, or reconcile receipt;
- no `caddy-release.sh`, `caddy-route-apply`, or `caddy-route-rollback` Menhir branch, and no Menhir
  entry in `remote-deploy.sh`, drift checks, `releases.json`, systemd units, tests, or docs;
- no Yawn process acquires a Menhir lock or creates, adopts, retains, or collects a Menhir bundle.

Yawn's generic Caddy release transaction may remain for Yawn services, but it must be namespaced to
Yawn locks and state and must have no Menhir consumer or authority. Menhir verifies the ingress
assertions above as a read-only expectation; it never writes them.

## Threat and authority model

The control plane protects against accidental drift, stale/replayed authorization, mutable local
inputs, partial installation, process interruption, confused-deputy wrappers, and a caller who has
SSH plus only the exact Menhir sudo permissions. It also detects unexpected host artifacts and
runtime identity changes.

The model does not claim to withstand unrestricted root, compromise of the owner private key,
compromise of GitHub's accepted identity/attestation roots, compromise of the container registry
after verification plus a defeated digest, or compromise of Cloudflare credentials. Those are
audited break-glass or external trust domains. The design still minimizes and records their impact.

Authority is granted only by the conjunction of:

1. a valid schema/version and canonical document digest;
2. a release/control-plane package whose complete bytes match that document;
3. accepted repository, workflow, attestation, dependency, image, and signing trust roots;
4. fresh staging and production-preflight evidence bound to the same release;
5. an unexpired owner signature over one lane and attempt;
6. the expected installed kernel, adapter, trust-store, and pre-state identities;
7. successful revalidation under all required locks before rollback is armed or mutation begins.

Possession of a package, ticket, signature, SSH account, upload directory, transport wrapper, or
read receipt alone grants no mutation authority.

## Owner key, signer, and trust-store contract

The owner authority is one dedicated Ed25519 key, separate from SSH, Git, TLS, OAuth and CI keys.
The normative workstation representation is PEM-encoded encrypted PKCS#8 under
`%USERPROFILE%\.menhir\keys\deployment-owner-ed25519.pem`. Encryption uses the maintained
`cryptography` PKCS#8 best-available password encryption profile; unencrypted, legacy raw, OpenSSH,
or repository-resident private-key files are refused. The key ID is lowercase SHA-256 of the DER
SubjectPublicKeyInfo bytes of the Ed25519 public key. The public key and key ID are safe to record;
private or passphrase bytes are not.

The `%USERPROFILE%\.menhir` directory and key file have Windows inheritance disabled and exactly two
access principals: the owning user and Windows `SYSTEM`, each with the minimum full-control rights
needed for backup and use. No `Administrators`, `Users`, service, inherited, network, synchronized,
workspace, repository, or temporary-path ACL is accepted. The signer rechecks canonical path,
non-link file type, owner, ACL, encryption and key-ID agreement immediately before every signature.

Passphrases enter only through a real interactive console with echo disabled. Redirected stdin,
command arguments, environment variables, pipeline input, config files, clipboard automation, and
non-interactive secret providers are refused. The signer decrypts only in process memory, creates no
plaintext temporary file, never logs exception values that may include private material, and
overwrites mutable passphrase/key buffers on every exit path as far as the runtime permits. Because
managed-runtime zeroization cannot be absolute, process lifetime is one signing operation and crash
dumps are disabled for that process. Tests instrument argv, environment, stdout/stderr, logs, upload
members and temporary directories for secret markers.

The approval client and signer are files from one accepted control-plane build. A deployment ticket
or bootstrap authorization carries `approval_client_sha256`, `signer_sha256`,
`control_plane_source_commit`, and the signer protocol version. The owner display recomputes these
from the local accepted installed-artifact manifest before signing; the root verifier requires them
to equal the versions allowlisted by its installed trust/control-plane generation. The shared
PowerShell wrapper is not a signer and is neither signed authority nor a root receipt claim.

Enrollment occurs only inside bootstrap:

- first install shows the package digest, canonical public key and key ID on both the owner
  workstation and root console; unrestricted root confirms the same fingerprint out of band before
  the trust store is activated;
- upgrade/rotation requires a valid authorization by a currently trusted, unrevoked key and names
  both old and new key IDs;
- rotation overlap is at most 24 hours. During overlap, the old key validates only tickets issued
  before the recorded rotation activation and expiring no later than overlap end. The new key is
  valid from activation. After overlap, the old key is revoked, not merely omitted;
- revocation records key ID, effective time, reason and authorizing bootstrap receipt. It immediately
  prevents first admission and any pre-mutation roll-forward under that key.

One encrypted offline backup of the same PKCS#8 file is kept on owner-controlled offline media
outside synchronized and workspace paths. Operations evidence records only its public fingerprint
and verification date. Loss freezes new deployments until an unrestricted-root recovery ceremony
enrolls a replacement and records the old key unavailable. Suspected compromise freezes submission,
revokes the key through bootstrap, invalidates every not-yet-rollback-armed attempt, rotates to a new
key and retains the revocation receipt. Recovery of an already mutated transaction follows the
post-admission safety policy below: rollback remains allowed from stored root authority; compromise
never silently grants roll-forward.

## System invariants and authoritative enforcement

| ID | Invariant | Lowest authoritative enforcement point | Refusal behavior |
|---|---|---|---|
| I-01 | Release bytes come only from declared immutable Git objects; dirty, untracked, submodule, symlink, or ambient bytes cannot enter. | Release compiler Git-object materializer before build | Abort before output publication |
| I-02 | Locked dependencies, offline wheelhouse, builder identity, image archive, image ID/config/layers, SBOM, scan, registry digest, repository, workflow, and attestation form one transitive chain. | Release-envelope compiler and verifier | Abort before staging/publication |
| I-03 | Staging uses the exact envelope on a disposable host and cannot approve or promote. | Root-owned staging verifier | Emit only a failed staging receipt |
| I-04 | Production preflight is read-only, short-lived, and fully binds the expected live state. | Preflight verifier and kernel under-lock recomputation | Refuse ticket creation or admission |
| I-05 | Owner approval binds one release, staging result, preflight, pre-state, lane, attempt, kernel, adapter, issue time, and expiry. | Approval client plus root signature verifier | Refuse before transaction creation |
| I-06 | Every routine mutation enters through one kernel command and one common admission path. | Sudoers plus root kernel dispatch | Unknown command/lane fails before locks or writes |
| I-07 | Admission and mutation are atomic with respect to competing lanes and live-state changes. | Kernel holding global and lane locks from revalidation through terminal state | Refuse or recover; never continue on changed state |
| I-08 | Snapshot, rollback arming, journal transitions, receipts, adoption, retention, and archival are kernel-owned. | Root kernel durable-state module | Remain nonterminal/blocked on uncertainty |
| I-09 | Recovery uses the stored package, adapter, and compatible kernel, never current checkout or installed-latest bytes. | Kernel recovery dispatcher | Refuse incompatible/missing stored bytes |
| I-10 | A product package cannot change kernel, trust store, sudoers, recovery, bootstrap, or read-command policy. | Package schema and kernel member allowlist | Reject package before snapshot |
| I-11 | Infrastructure uses a distinct signed package but the same admission, locks, journal, rollback, and receipt protocol. | Kernel infrastructure adapter dispatch | Refuse unsigned or mixed package type |
| I-12 | Exactly one privileged mutating entry point exists; read-only commands are separately enumerated. | Atomic sudoers replacement and installed-artifact census | Installation/cutover fails closed |
| I-13 | `yawn.deploy` owns the `memory.ctharvey.me` vhost; Menhir installs no ingress route writer and no second ingress. | Source/host census plus read-only ingress verification | Refuse convergence/acceptance |
| I-14 | App, database, OAuth/MCP, ingress, backup, and restore behavior changes only when a signed lane plan explicitly declares it. | Adapter pre/postcondition verifier | Roll back or block |
| I-15 | A signed attempt can cause at most one mutation transaction. | Kernel O_EXCL attempt-anchor publication under global lock | Resume/adopt exact match; reject every mismatch |
| I-16 | v1 cannot start, queue, or resume after bootstrap claims the handoff fence. | Bootstrap fence, caller drain, all-lock acquisition, process census, and tombstones | Bootstrap aborts or restores v1 exactly |
| I-17 | Unknown writers and legacy/absent states are first-class failures, not omitted cases. | Machine-readable source/install/host census | Gate remains open |

Wrappers, documentation, string scans, desktop state, and happy-path tests are never authoritative
enforcement points. They may provide defense in depth or evidence only.

## Canonical protocol rules

All semantic records are UTF-8 canonical JSON with no BOM. The protocol library:

- rejects duplicate object keys before decoding;
- rejects floats, non-finite numbers, ambiguous timestamps, Unicode normalization drift, unknown
  fields, missing fields, unexpected nulls, and non-canonical path/digest/ID forms;
- uses sorted object keys, compact separators, NFC strings, decimal integers, and a trailing newline
  only for stored files; the digest covers the canonical bytes without filesystem metadata;
- represents SHA-256 as exactly 64 lowercase hexadecimal characters and IDs by each schema's exact
  grammar;
- represents time as UTC RFC 3339 seconds ending in `Z`; monotonic elapsed durations are integers in
  milliseconds and never inferred from wall-clock subtraction;
- carries `schema`, `schema_version`, and `record_id` in every record;
- uses one parser and validator per schema in the canonical Python library. Generated schemas,
  fixtures, PowerShell tests, docs, and read clients consume that implementation rather than copying
  field logic.

Compatibility is deliberately narrow. Major or unknown versions, fields, algorithms, digest kinds,
record types, lane names, state transitions, and adapters fail closed. A minor additive change is
allowed only after the current reader explicitly lists and tests it. There is no “ignore unknown”
mode. Migration converts a complete record before it reaches production; mixed-version transactions
are forbidden.

The protocol registry below is exhaustive for v2. Adding a record requires an architecture revision,
owner/producer/consumer assignment, canonical digest rule, compatibility rule, retention rule, and
retirement impact before implementation.

| Record ID | Producer | Authorized consumers | Digest/binding | Compatibility and retention |
|---|---|---|---|---|
| `release-envelope.v2` | Release compiler | Publisher, staging, approval, kernel | Digest covers complete canonical envelope and content manifest | Exact v2; immutable and permanently retained with release evidence |
| `publication-attestation.v2` | CI publication workflow | Release verifier, staging | Binds repository/workflow/run/commit/image registry digest | Exact accepted issuer/workflow; retained with envelope |
| `deployment-subject.v2` | Typed compiler | Staging, approval, kernel/bootstrap | Discriminated union containing exactly one product envelope, infrastructure package, control-plane package, GC plan, or archive plan digest plus its package manifest | Exact subject enum/cardinality; immutable and retained with every dependent record |
| `staging-receipt.v2` | Staging verifier | Approval client, kernel/bootstrap | Binds `deployment-subject.v2`, staged bytes, adapter/verifier, host profile, checks and chronology | Exact v2, terminal immutable; retained with attempt |
| `production-preflight-receipt.v2` | Read-only preflight verifier | Approval client, kernel/bootstrap | Binds deployment subject, kernel/read-model versions, full pre-state composite and observation time | Valid at authorization creation only if age <=15 minutes; retained with attempt |
| `deployment-ticket.v2` | Approval client | Root kernel | Binds non-bootstrap subject, staging, preflight, pre-state, lane, attempt, package, adapter, kernel, signer, key, issue/expiry | Maximum 30-minute initial-admission lifetime; one attempt; retained permanently |
| `owner-signature.v2` | Approval client signer | Root kernel, bootstrap verifier where applicable | Ed25519 over domain separator plus canonical target-record bytes; carries key ID only | Exact algorithm/domain; retained with target |
| `trust-store.v2` | Bootstrap transaction | Signer display, kernel, bootstrap verifier | Content digest binds keys, validity, revocation, overlap and generation | Product cannot alter; current and previous generations retained |
| `lane-plan.v2` | Typed compiler | Kernel and named adapter | Binds subject type/lane, members, commands by symbolic operation, pre/postconditions, rollback scope | Exact lane/adapter; stored with transaction |
| `package-manifest.v2` | Release or bootstrap compiler | Staging, kernel/bootstrap | Merkle-style member list with canonical relative path, type, size, mode and SHA-256 | No extra members; content-addressed retention |
| `transport-manifest.v2` | Approval client | PowerShell transport and root intake | Binds a flat exact filename/size/SHA-256 list for all submission records and payload files; carries no semantic authority | Exact v2; wrapper may parse only this generated transport shape; retained in intake evidence |
| `intake-receipt.v2` | Root intake capture | Kernel, status/adoption | Binds upload ID, source descriptor facts, root-copy member manifest, capture chronology | Local root evidence; retained with transaction |
| `attempt-anchor.v2` | Root kernel | Kernel recovery, replay, census | Sole authoritative O_EXCL attempt reservation; binds first upload, ticket/signature, subject, package, adapter and kernel | Written/fsynced before mutation; immutable; indexes rebuild from anchors |
| `transaction-journal.v2` | Root kernel | Kernel recovery and read model | Hash-chained durable transitions for app-only, security-config, maintenance, infrastructure, GC, or archive, bound to attempt anchor | Never auto-deleted; unknown/nonterminal blocks |
| `snapshot-manifest.v2` | Root kernel plus adapter observations | Kernel rollback/recovery, terminal receipt | Binds exact prior presence/absence, bytes/metadata, units, containers, routes and lane state plus restoration prerequisites | Written/fsynced and verified before rollback is armed; retained permanently |
| `adapter-operation-evidence.v2` | Named stored adapter | Kernel journal and verifier | Binds symbolic operation, before/after observation, exit/result and monotonic chronology; embedded by digest in journal | Exact adapter schema; retained with journal |
| `terminal-receipt.v2` | Root kernel | Adoption, status, desktop receipt copy | Binds journal head, before/after composites, checks, rollback outcome, exact stored bytes and chronology | Terminal immutable; never auto-deleted |
| `adoption-receipt.v2` | Root kernel read path | Owner/status copy | Binds terminal receipt, newly observed complete live-state equality and observation chronology; contains no mutation transition | Immutable observation; retained with caller evidence |
| `live-state-composite.v2` | Preflight/kernel state observer | Ticket, journal, terminal receipt, adoption | Canonical digest of all required state components with component digests | Missing/extra component fails; retained by containing records |
| `infrastructure-package.v2` | Clean infrastructure build | Staging, approval, kernel infrastructure adapter | Binds exact Menhir desired-state and Yawn VPS commits plus fixed host artifacts; excludes kernel/trust/sudoers | Product and bootstrap paths reject; content-addressed retention |
| `control-plane-package.v2` | Clean control-plane build | Bootstrap transaction only | Package manifest plus kernel/env/adapter/schema/read-wrapper/sudoers/unit identities | Product path rejects; all referenced generations retained |
| `installed-artifact-manifest.v2` | Bootstrap transaction | Kernel, preflight, audits | Binds every installed path/type/mode/owner/group/digest and every required absence | Current/previous/rollback generations retained |
| `bootstrap-authorization.v2` | Owner signer or first-install root ceremony | Bootstrap transaction | Binds control-plane subject, its successful staging receipt, preflight/pre-state, trust action, bridge/handoff generation, signer/key, expiry and bootstrap attempt | First install also requires out-of-band fingerprint confirmation |
| `bootstrap-journal.v2` | Bootstrap transaction | Bootstrap recovery/read model | Hash-chained handoff states, lock/process census, snapshots and activation identity | Never auto-deleted; nonterminal blocks product submit |
| `bootstrap-terminal-receipt.v2` | Bootstrap transaction | Preflight, audit | Binds exact prior/final installation, v1 absence, trust/sudoers/kernel and handoff chronology | Terminal immutable; retained permanently |
| `obsolete-artifact-absence.v2` | Bootstrap/infrastructure verifier | Bootstrap receipt, acceptance | Binds exhaustive expected-absent source and host identities | Exact census version; retained with bootstrap receipt |
| `gc-plan.v2` | Root kernel planner | Owner approval display and kernel GC adapter | Binds complete reachable set and proposed removals at one locked state; wrapped as deployment subject and ticket lane `gc` | Recomputed under locks; caller cannot add removals |
| `gc-receipt.v2` | Root kernel | Status/audit | Binds plan, removed scratch/package objects and retained reachability roots | Terminal immutable; retained permanently |
| `archive-plan.v2` | Kernel planner from owner-selected retention policy | Owner approval display and kernel archive adapter | Binds exact terminal evidence objects, fixed destination identity and source disposition; wrapped as deployment subject and ticket lane `archive` | Cannot include active/current/previous/rollback roots; retained permanently |
| `archive-receipt.v2` | Kernel archival adapter | Status/audit | Binds copied objects, destination verification, source disposition and chronology | Source deletion only after verified durable destination |
| `status-response.v2` | Kernel read model through Yawn wrapper | Yawn gateway/owner | Binds protocol version, active/nonterminal/blocked transactions and installed generation | Read-only, exact v2, bounded output |
| `release-response.v2` | Kernel read model through Yawn wrapper | Yawn gateway/owner | Binds active/previous release and composite digests, without secrets | Read-only, exact v2 |
| `logs-response.v2` | Kernel read model through Yawn wrapper | Yawn gateway/owner | Bounded structured events, cursor, truncation flag; secret-redacted | Read-only, exact v2; no arbitrary unit/path |
| `backup-response.v2` | Kernel read model through Yawn wrapper | Yawn gateway/owner | Binds current backup/rehearsal generations and readiness, without credentials | Read-only, exact v2 |
| `generation-response.v2` | Kernel read model through Yawn wrapper | Yawn gateway/owner | Binds installed control-plane current/previous/rollback identities | Read-only, exact v2 |

The exhaustive schema implementation registry is generated from the schema package and must carry,
for every row above: schema file, owner module, producer command, consumer commands, domain
separator, digest function, compatibility range, retention class, introduction phase, and any
retired predecessor. CI compares that registry with actual schemas and dispatch tables. A schema,
producer, or consumer absent from the registry fails the phase census.

The records above are also exhaustive for durable semantic structures. A directory layout, lock
file, temporary file, derived lookup index, or atomic pointer is not a semantic record and cannot
contain authority unavailable from registered records. Attempt lookup indexes are rebuildable only
from `attempt-anchor.v2`; transaction current-state pointers are rebuildable only from the validated
hash chain in `transaction-journal.v2`. Adapter stdout/stderr are bounded fields inside
`adapter-operation-evidence.v2`, not free-standing evidence. GC and archive use the common attempt
anchor, deployment ticket, transaction journal, snapshot, terminal receipt, and adoption protocol;
their specialized plan/receipt rows are the only additional semantic records.

## Canonical repository identity registry

The protocol package owns this closed registry. Callers provide a local object source and commit,
not a trusted URL. The compiler normalizes supported GitHub HTTPS and SSH transport spellings to the
case-sensitive `github:<owner>/<repository>` identity below, compares that identity to the package
type, and then verifies the object. Redirects, forks, mirrors, alternates that supply objects from an
unregistered remote, replace/graft refs, and mixed object formats are refused. Updating any row is a
reviewed architecture and schema change.

| Repository ID | Canonical identity and accepted origin | Git object format | Package use | Attestation identity | Authority owner |
|---|---|---|---|---|---|
| `menhir` | `github:Archolith/menhir`; `https://github.com/Archolith/menhir.git` or transport-equivalent SSH after normalization | SHA-1 commit, exactly 40 lowercase hex until an explicit object-format migration | Product, infrastructure and control-plane source at independently declared commits | `Archolith/menhir` and the exact allowlisted workflow path/ref in the subject | Menhir protocol registry |
| `archolith_oauth` | `github:Archolith/archolith_oauth`; `https://github.com/Archolith/archolith_oauth.git` or transport-equivalent SSH after normalization | SHA-1 commit, exactly 40 lowercase hex | Product source only | Repository identity above; package metadata URL is non-authoritative and must be made consistent or rejected by the compiler | Menhir protocol registry with Archolith repository ownership |
| `yawn_vps` | `github:ctharvey/yawn.vps`; `https://github.com/ctharvey/yawn.vps.git` or transport-equivalent SSH after normalization | SHA-1 commit, exactly 40 lowercase hex | Infrastructure source only | `ctharvey/yawn.vps` when an attestation is required | Menhir protocol registry with Yawn repository ownership |
| `shared_workspace` | `github:ctharvey/workspace-meta`; `https://github.com/ctharvey/workspace-meta.git` or transport-equivalent SSH after normalization | SHA-1 commit, exactly 40 lowercase hex | Never package content; interface-test and review anchor only | None for Menhir authority | Workspace transport owner |
| `yawn_deploy` | `github:ctharvey/yawn.deploy`; `https://github.com/ctharvey/yawn.deploy.git` or transport-equivalent SSH after normalization | SHA-1 commit, exactly 40 lowercase hex | Never v2 package content; contraction-test and review anchor only | None for Menhir authority | Yawn deploy owner |

Local checkout paths are deliberately absent from authority. The standard OAuth checkout currently
uses `C:\Users\thron\IdeaProjects\projects\archolith\archolith_oauth`; other checkouts with the same
origin are acceptable object sources only when their requested commit and object database pass the
same immutable checks. The stale `ctharvey/archolith_oauth` package-metadata URL is not an alternate
accepted repository identity.

## Typed subjects and authorization families

`deployment-subject.v2` is the common discriminant. Its exact `subject_type` enum and payload are:

| Subject type | Exactly one target digest | Package manifest | Authorization family | Allowed lane |
|---|---|---|---|---|
| `product-release` | `release-envelope.v2` | Required product package | `deployment-ticket.v2` | `app-only`, `security-config`, or `maintenance` as allowed by the envelope |
| `infrastructure` | `infrastructure-package.v2` | Required infrastructure package | `deployment-ticket.v2` | `infrastructure` |
| `control-plane` | `control-plane-package.v2` | Required control-plane package | `bootstrap-authorization.v2` only | Bootstrap state machine, never a normal lane |
| `gc` | `gc-plan.v2` | Forbidden; referenced objects are already root-owned | `deployment-ticket.v2` | `gc` |
| `archive` | `archive-plan.v2` | Forbidden; referenced objects are already root-owned | `deployment-ticket.v2` | `archive` |

Every staging receipt, preflight receipt, authorization, attempt anchor, journal and terminal receipt
carries the complete subject digest and exact subject type. Product fields cannot appear for another
type. GC/archive staging is a pure disposable simulation of the plan against a fixture package store;
production preflight binds the real under-lock reachability roots and destination identity.

`deployment-ticket.v2` is valid only for the five normal subject/lane combinations above. It binds
the successful staging receipt, production preflight, full pre-state, lane plan, attempt, installed
kernel/adapter and signer identities. `bootstrap-authorization.v2` is valid only for
`control-plane`; it binds its successful staging receipt, bootstrap preflight, expected installation
pre-state/absence, accepted v1 bridge generation, trust action, bootstrap attempt and signer. The
domain separators are distinct (`MENHIR-DEPLOYMENT-TICKET-V2\0` and
`MENHIR-BOOTSTRAP-AUTHORIZATION-V2\0`), so a signature for one family cannot authorize the other.

The approval client emits a flat submission directory and `transport-manifest.v2`. The product
manifest names exactly: subject record, its required target record/package where applicable,
package manifest where required, staging receipt, production-preflight receipt, deployment ticket
and detached owner signature. The bootstrap manifest names exactly: control-plane
subject/package/manifest, successful staging receipt, production-preflight receipt, bootstrap
authorization and detached owner signature. The transport manifest is not signed authority; root
uses it to bound descriptor capture, then requires every captured digest to be transitively bound by
the signed semantic records. Every cross-record digest and subject type must agree before
reservation. First install additionally uses the unrestricted-root out-of-band digest/fingerprint
confirmation; it does not relax the record bindings.

## Release compiler and supply-chain design

The compiler starts from explicit canonical repository URLs and 40-character commits. It fetches no
mutable branch or tag as authority. In a new empty directory per repository it materializes exactly
the commit tree from Git objects, rejects missing objects, submodules unless explicitly enumerated,
symlinks where the member contract forbids them, case-colliding paths, alternate data streams, and
any source whose canonical remote differs. A caller checkout may be dirty; its filesystem is never a
source. If a local checkout is used only as an object database, the compiler verifies the object and
remote identity and exports from the object, not the worktree or index.

Repository identity is typed rather than combined into one release:

- product: `menhir` supplies application/configuration bytes and `archolith_oauth` supplies exactly
  one wheel/source identity, each verified against its commit;
- infrastructure: `menhir` supplies desired host/gateway installation state, excluding ingress, and
  `yawn_vps` supplies exact read-only gateway/runtime bytes, each verified against its commit;
- control plane: `menhir` supplies kernel, protocol, adapters, bootstrap, sudoers and root read
  wrappers from one exact commit.

The release compiler uses a hash-locked toolchain and wheelhouse built on a clean networked builder,
then performs the production build with network disabled. Every wheel has name, version, SHA-256,
source repository/commit where internal, and lockfile lineage. No dependency resolver, mutable
index, Git branch, local editable install, or ambient cache participates in the offline build.

CI builds and tests one candidate image, exports that exact image archive, and records:

- OCI/Docker archive SHA-256 and member manifest;
- image ID and config digest;
- ordered layer diff IDs and compressed layer digests;
- platform, entrypoint, command, labels and environment allowlist;
- base-image digest and build-tool identities;
- SBOM digest and subject identity;
- vulnerability report/scanner/database identity and policy result;
- canonical registry repository plus immutable digest;
- GitHub repository, workflow path/ref, run ID/attempt, commit, OIDC issuer/subject, and attestation
  bundle identity.

Publication attaches the immutable registry digest to the already validated subject. It never
rebuilds, retags as semantic authority, or substitutes a registry pull for the sealed archive
without proving config and every layer equal. Two clean builds from the same inputs must agree on
all deterministic bytes. Any unavoidable signed timestamp is outside the deterministic subject and
is separately bound by digest.

## Staging and production preflight

Staging accepts one complete subject: `release-envelope.v2`, `infrastructure-package.v2`, or
`control-plane-package.v2`. The receipt carries an exact `subject_type` and subject digest, and a
subject cannot borrow evidence from another type. The disposable staging host performs the same
descriptor-safe intake defined below, verifies every applicable package and image lineage field
before load/start, and creates production-equivalent configuration from declared fixture secrets.
It has no production credential or route.

The staging receipt records the exact release/package/adapter/kernel-compatible verifier digests,
host profile, start/end/elapsed times, checks and results. It covers clean install, restart,
app-only, security-config and maintenance rehearsal behavior required by the declared lane; OAuth
PKCE, MCP allow/deny, backup/restore, isolation and resource tests remain explicit checks. A failed
or interrupted run produces a failed receipt or resumes from its own stored bytes; it cannot produce
an approval-eligible success.

Production preflight is a separate explicit owner-invoked, read-only operation. It acquires the
global and relevant lane locks non-mutatingly, rejects an existing unknown/nonterminal bootstrap or
product state, and emits a `production-preflight-receipt.v2`. The live-state composite contains at
least:

- active and previous release-envelope and terminal-receipt digests;
- installed kernel, protocol, adapter, trust-store and artifact-manifest generations;
- effective configuration member digests and secret *identity/version* markers, never secret bytes;
- app image digest, image ID, container ID/config/labels/network attachment and health;
- Neo4j container/image/volume/database identity, migration/schema marker and health;
- shared Caddy container/image/config/certificate-mount/Compose labels/network identity and health,
  observed read-only as an ingress expectation owned by `yawn.deploy`;
- inspection gateway binary/unit/bind/firewall and authenticated peer identity;
- OAuth issuer/audience/client/redirect/scope/tier policy digests and MCP tool/catalog policy digests;
- backup generation, manifest, destination readiness and most recent restore-rehearsal binding;
- systemd units/timers, sudoers, filesystem ownership/modes, locks, journals and obsolete-path
  absence relevant to the lane.

The kernel reruns the same observer after acquiring mutation locks and demands component-by-component
equality, not only a caller-supplied composite hash. Ticket creation requires a preflight no older
than 15 minutes. Kernel admission requires the preflight still be within the ticket's validity and
that the current state remains exactly equal.

## Root inbox capture: race-safe immutable intake

The product sudo command accepts only a 32-character lowercase hexadecimal upload ID. It does not
accept a path, package member, lane, command, URL, digest, or shell text. The fixed caller-writable
inbox parent and root transaction parent are on filesystems whose mount and ownership policy is part
of the installed manifest.

The kernel performs intake before semantic parsing as one descriptor-based operation:

1. open the fixed inbox parent once as a directory descriptor with no symlink traversal;
2. open the upload directory relative to that descriptor using `openat2` resolution constraints
   (`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`) or an explicitly tested
   equivalent; never resolve an absolute/canonicalized caller path and reopen it;
3. enumerate a bounded flat member set from the held directory descriptor and record each
   name/inode fact; require exactly one fixed-name transport manifest, safely copy/parse that member
   first, and require its bounded name set to equal the enumeration. Reject extra, missing, nested,
   case-colliding, link, device, socket, FIFO, sparse/oversized, multi-link, wrong owner/group/mode,
   or changed entries. Semantic subject-specific allowlists are checked after the full root copy;
4. open every member relative to the held directory descriptor with `O_NOFOLLOW`, verify `fstat`
   device/inode/mount identity, type, owner/group/mode, link count, size and change/modify times, and
   keep every source descriptor open; cross-mount resolution is forbidden;
5. create a fresh root-owned, mode-0700 capture directory with `O_EXCL`; copy bytes only from the held
   descriptors into fresh files, hashing and enforcing per-member/total bounds while copying;
6. `fsync` every destination file, write and `fsync` the canonical member manifest and intake
   receipt, `fsync` the capture directory, atomically rename it to its content-addressed root-owned
   transaction location, then `fsync` the parent;
7. after copying, repeat `fstat` on every still-open source descriptor and require all recorded
   device/inode/mount, type, owner/group/mode, link count, size and supported change/modify versions
   unchanged; enumerate the held upload-directory descriptor a second time and require the exact same
   bounded name-to-inode set and directory metadata;
8. close source descriptors only after the source recheck and root copy are durable; reopen only the
   root-owned copies, repeat type/mode/size/digest checks, then parse schemas and verify signatures;
9. never consult the caller inbox again for execution, resume, adoption, rollback, or evidence.

Any replacement, added/removed entry, symlink swap, truncation, append, ownership/mode change, inode
change, short read, digest mismatch, directory metadata change, cross-mount resolution, or duplicate
intake during capture fails before a journal can reach `admitted`. Renaming the upload directory
without changing the held directory object is harmless only if the second descriptor census and all
member facts are unchanged; the upload ID remains bound to that held object. Renaming/replacing the
directory entry and changing the held object or its contents refuses. Tests race every operation
above, including both rename cases and file replacement after `fstat`; the root copy must be the
unchanged held-descriptor bytes or the operation must refuse. Caller uploads are never executed in
place.

## Privileged command boundary

After cutover the product sudoers file contains exactly one privileged mutating executable pattern:

`sudo -n /usr/local/libexec/menhir-txn submit <32-lowercase-hex-upload-id>`

The executable performs its own exact argc/verb/ID validation. Sudoers does not use an argument
wildcard broad enough to pass paths or arbitrary verbs. Direct root invocation follows the same
admission path; root status is not treated as a valid owner signature.

Five separately enumerated fixed read-only commands may remain because “one entry point” applies to
mutation, not observation:

- `/usr/local/libexec/menhir-status`
- `/usr/local/libexec/menhir-release-inspect`
- `/usr/local/libexec/menhir-logs --lines <bounded-integer>`
- `/usr/local/libexec/menhir-backup-status`
- `/usr/local/libexec/menhir-generation-inspect`

Each dispatches a named kernel read-model function and returns exactly one corresponding v2 JSON
response. They accept no caller path, unit, selector, cursor outside the schema, environment override,
shell text, or output destination. They cannot create/transition a journal, acquire a mutation lock,
invoke an adapter, write a file, call Docker mutation, control systemd, or alter backup/restore state.
Sudoers replacement is atomic: write a root-owned candidate, validate it with `visudo -cf`, rename it
over the one canonical file, fsync the directory, and prove all obsolete sudoers fragments absent.

Bootstrap is never exposed in product sudoers. First install and control-plane upgrade require an
explicit unrestricted-root ceremony at a fixed local executable with a fixed package path or
content-addressed package ID, never a caller-provided remote shell program.

## Deployment transaction state machine and durable commit primitive

The common transaction applies to `app-only`, `security-config`, `maintenance`, `infrastructure`,
`gc`, and `archive`. Bootstrap has its own state machine. Descriptor-safe capture and full semantic
verification first create a root-owned prepared directory. Under the global lock, the kernel writes
and fsyncs `attempt-anchor.v2` inside that directory, fsyncs the directory, then publishes it as
`transactions/<attempt-id>` with a no-replace atomic rename and fsyncs the transactions parent. That
single namespace publication is the authoritative attempt/upload reservation. If the destination
already exists, only its anchor may decide replay. A crash before publication has no attempt; a crash
after publication has one complete anchor. No later upload may fill or replace a partial anchor.

The attempt directory contains the intake receipt, canonical authority, stored package/adapter,
snapshot manifest, immutable numbered journal records, terminal/adoption receipts, and an atomic
`HEAD` file. Each journal record binds the prior record digest. The validated record named by `HEAD`
is the sole current-state authority. Directory listings and convenience indexes are derived and
rebuildable from attempt anchors plus validated journal heads.

| Current state | Event/condition | Next state | Mutation rule |
|---|---|---|---|
| No published anchor | Valid capture and unused attempt under global lock | `reserved` | None; publish complete attempt anchor atomically |
| `reserved` | Global then lane lock acquired | `locked` | None |
| `reserved` or `locked` | Invalid/expired/revoked authority, incompatible recovery, or lock refusal before any mutation | `refused` | Terminal non-mutating receipt |
| `locked` | Installed state and every authority digest valid | `revalidated` | None |
| `locked` or `revalidated` | Pre-state mismatch or pre-mutation refusal | `refused` | Terminal non-mutating receipt |
| `revalidated` | Exact restorable prior state durably captured | `snapshotted` | Snapshot writes only; no managed-state mutation |
| `snapshotted` | Snapshot and restoration prerequisites independently verified | `rollback-armed` | Journal transition fsynced before first managed-state mutation |
| `snapshotted` | Verification/refusal before rollback arming | `refused` | Terminal non-mutating receipt; retained snapshot may be collected only by normal reachability rules |
| `rollback-armed` | First symbolic adapter operation is about to run | `applying` | An operation-intent record is fsynced before invocation |
| `rollback-armed` | Failure/interruption before first adapter operation | `rollback-pending` | Rollback verifies exact prior state even if no managed bytes changed |
| `applying` | All operations have durable evidence | `verifying` | No new operation after transition except fixed verification |
| `applying` or `verifying` | Operation failure, interruption, changed undeclared state, or revoked authority | `rollback-pending` | No further roll-forward unless the recovery policy below expressly permits it |
| `rollback-pending` | Stored snapshot/adapter/kernel compatible | `rolling-back` | Each rollback intent/result is journaled and fsynced |
| `rolling-back` | Exact prior state proven | `rolled-back` | Terminal receipt and transition |
| `rolling-back` | Exact restoration cannot be proven | `blocked` | Locks release only after durable blocked record; new mutation attempts remain refused |
| `verifying` | All postconditions and undeclared-state equality proven | `committed` | Terminal receipt and transition |
| Any nonterminal | Journal/package/snapshot uncertainty or incompatible safe recovery | `blocked` | No unrecorded action |
| `refused`, `rolled-back`, or `committed` | Any submit/recovery event | Same terminal state | Observation/adoption only; never mutate |

`refused`, `rolled-back`, and `committed` are terminal. `blocked` is nonterminal but only a
journal-declared recovery action may leave it. There is no transition out of a terminal state and no
journal deletion transition.

Atomicity and ordering are mandatory:

1. descriptor-safe capture, parse, signature/package verification and prepared-directory fsync occur
   without managed-state mutation;
2. acquire the global admission lock, atomically reserve the complete attempt anchor, then acquire
   the lane lock in the one fixed order with nonblocking bounded acquisition; bootstrap uses the
   larger fixed order below;
3. validate recovery compatibility, installed manifest, subject/ticket/staging/preflight authority,
   signer/key policy, package/adapter/kernel digests and the time policy below;
4. recompute full live state under the held locks and compare each component to preflight/ticket;
5. create/fsync `snapshot-manifest.v2` and prove every restoration prerequisite exists;
6. append/fsync `rollback-armed`; only then may an adapter mutation intent be recorded and executed;
7. append/fsync operation intent before, and `adapter-operation-evidence.v2` after, every operation.
   A crash with intent but no result invokes the operation's schema-declared probe; the adapter may
   mark complete, run a declared idempotent retry, or roll back—never guess;
8. verify lane postconditions and undeclared-state equality while still holding locks;
9. write/fsync the terminal receipt first, write/fsync the terminal journal record that binds its
   digest second, then atomically replace/fsync `HEAD` last. Only `HEAD` commits terminal state. A
   crash before the head update leaves an ignored orphan and resumes from the prior authoritative
   state; no three-file atomicity is assumed;
10. rebuildable lookup/status indexes may update only after the authoritative head commits and are
    never consulted over anchors/journal heads for admission or replay.

Journal/receipt/head writes use a new file plus file `fsync`, no-replace creation where immutable,
atomic rename where replacing `HEAD`, and parent-directory `fsync`. Snapshots record absence as well
as bytes, metadata, enablement/activity states, Docker identities, routes and backup selections.
Rollback restores exact prior state, including deleting only newly created declared objects and
recreating prior absence. A failed exact comparison ends `blocked`, never “best effort success.”

Ticket time and revocation have state-specific semantics:

- first publication of an attempt anchor and every transition through `rollback-armed` require the
  ticket to be unexpired and its key currently valid/unrevoked;
- an attempt that expires or is revoked before `rollback-armed` transitions to non-mutating
  `refused` once its locks are reacquired;
- after `rollback-armed`, safety recovery may run after expiry from stored root authority. Rollback
  is always permitted. Roll-forward is permitted only when the key is not revoked, the ticket was
  valid at the durable `rollback-armed` transition, and the pending operation's registered probe
  proves continuation/retry is required and idempotent;
- revocation after `rollback-armed` forces rollback. If exact rollback is impossible, the attempt is
  `blocked` for audited unrestricted-root recovery; revocation never grants a new roll-forward;
- terminal replay and adoption are read-only and may run after ticket expiry or revocation.

## Replay, retry, resume, and adoption matrix

The signed `attempt_id` is globally unique and causes at most one mutation. `upload_id` is a
transport identity: the first admitted capture binds it to the attempt. The kernel decides every
repeat under both locks using root-owned state.

| Existing state for attempt | Incoming bytes/identity | Required result |
|---|---|---|
| No attempt anchor | Valid ticket/package/signature and unused upload ID | Atomically publish one complete prepared attempt directory and `attempt-anchor.v2` under the global lock |
| Nonterminal | Same ticket, signature, package, adapter, kernel compatibility and first-bound upload ID | Resume/recover from stored root bytes and journal; never re-copy for execution |
| Nonterminal | Same ticket under a different upload ID, or any byte/digest/lane/kernel/adapter mismatch | Refuse as replay/substitution; leave original transaction unchanged |
| `committed` | Exact same identities and first-bound upload ID | Recompute live state under locks; return adoption receipt only if equal to terminal receipt |
| `rolled-back` | Exact same identities and first-bound upload ID | Return the terminal rollback receipt; never mutate again |
| `refused` | Exact same identities and first-bound upload ID | Return the terminal non-mutation receipt; never retry under this attempt ID |
| Terminal | Any identity/upload mismatch | Refuse; never create a second transaction |
| `blocked` | Exact same identities and first-bound upload ID | Run only the journal-declared compatible recovery action from stored bytes, or remain blocked |
| Any state | Same ticket bytes but changed caller receipt/destination/wrapper | Ignore caller receipt as authority; kernel result is unchanged |
| Any state | Reused attempt ID with a newly signed or edited ticket | Refuse permanently as attempt collision |

Adoption is observation, not a journal transition and not success inferred by a desktop wrapper. The
kernel writes `adoption-receipt.v2` after checking subject authority, configuration, app, database,
ingress, OAuth/MCP policy, backup/restore, installed control plane, and lane-specific state against
the terminal receipt while locks are held. Any mismatch returns a refusal requiring a new
preflight/ticket; it never repairs or repeats mutation.

## Lane adapter contract

Every adapter is immutable, versioned, stored before mutation, and implements only:

- `preflight(context) -> observations`
- `snapshot(context) -> snapshot manifest`
- `apply(context, operation) -> operation evidence`
- `verify(context) -> observations`
- `rollback(context, snapshot) -> rollback evidence`

The kernel supplies fixed, already-open package members and symbolic operations. Adapters receive no
caller strings or paths and cannot parse tickets, signatures, envelopes, journals, receipt
destinations, locks, retention rules, or trust stores. They cannot update themselves.

Six non-bootstrap lanes exist:

| Lane | May change | Must remain equal unless separately declared |
|---|---|---|
| `app-only` | Menhir app image/container and app release marker | Neo4j data/container/volume, ingress, gateway, OAuth policy, backup/restore, kernel/trust/sudoers |
| `security-config` | Declared effective app/OAuth/MCP security config and required app restart | Images except declared app restart identity, Neo4j data, ingress route/config, backup/restore, kernel/trust/sudoers |
| `maintenance` | Declared app/database maintenance sequence, backup and restore rehearsal state | Ingress, gateway public boundary, OAuth/MCP contract, kernel/trust/sudoers except explicitly declared product state |
| `infrastructure` | Declared units, timers, firewall, gateway bind, directories and read wrappers | Application behavior/data, OAuth/MCP semantics, backup formats/content, kernel/trust/sudoers, and all ingress configuration |
| `gc` | Only unreferenced scratch/package objects named by the kernel-generated and under-lock-recomputed GC plan | Every semantic record and active/current/previous/rollback/snapshot root; all runtime state |
| `archive` | Exact eligible evidence objects copied to one registered durable destination and, only after verification, the plan's explicit source disposition | Active/current/previous/rollback/snapshot roots, deployment/runtime behavior and unlisted evidence |

Changes to kernel, protocol package, trust store, sudoers, bootstrap recovery, or adapter installation
are not an infrastructure lane; they require bootstrap.

## Fast lane and production-state equality

“Fast” describes a narrower lane plan, never weaker authority. App-only and security-config consume
the same envelope, staging/preflight receipts, owner ticket, intake, locks, journal, snapshot,
rollback, terminal receipt, replay matrix, and adoption checks as maintenance.

Every successful receipt contains a `live-state-composite.v2` plus component digests. A fast-lane
receipt must prove exact equality for every undeclared component before and after. Its composite is
not a shortcut hash over fewer fields. Adoption recomputes the complete production state and checks
each component. An app-only deployment that leaves the expected image running but changes database,
ingress, policy, backup, kernel, or configuration state is not adoptable success.

## Bootstrap and v1-to-v2 handoff

Bootstrap requires one deliberately narrow **v1 handoff bridge** before cutover. Every deployed v1
mutating entry must first be converted, through the currently authorized v1 transaction mechanism,
to acquire the same v1 global lock before any lane lock, use nonblocking lock acquisition, check one
fixed root-owned handoff fence both before and after lock acquisition, and refuse when the fence is
present. The bridge adds no v2 mutation, trust, package, receipt or fallback behavior. Its source,
installed manifest, disposable-host rehearsal and complete writer census are an independently
reviewed gate. If any v1 writer cannot be bridged or enumerated, v2 bootstrap is impossible and
remains blocked; break-glass is incident recovery, not an architecture substitute.

Bootstrap is a separate root transaction with states:

`authorized -> bridge-verified -> v1-drained -> all-locks-held -> snapshotted -> fenced -> callers-disabled -> staged -> verified -> activated -> v1-retired -> absence-verified -> committed`

Failure transitions enter `rollback-pending -> rolling-back -> rolled-back` or `blocked`. Its package
contains the complete offline kernel environment, schemas, adapters, read wrappers, units, sudoers,
trust-store action and installed/obsolete manifests. Product packages cannot contain these members.

The handoff protocol explicitly covers callers that began before, during, and after the fence:

1. verify the bootstrap subject, successful control-plane staging receipt, production preflight,
   owner authorization/signature and package completely before first mutation;
2. verify the exact accepted v1 handoff bridge installed on every censused mutator. Reject an
   unknown bridge, blocking lock behavior, unbridged writer, old fence, or unknown transaction;
3. if a known v1 transaction is active, allow it to reach a verified terminal state **before any
   fence exists**. Bootstrap takes no lock and writes nothing during this bounded drain. Unknown,
   blocked or unsafe recovery refuses bootstrap;
4. acquire the bridged v1 global admission lock first. A v1 caller that already owns it finishes
   before bootstrap can proceed; a caller starting after bootstrap owns it fails nonblocking. Then
   acquire every v1 lane lock followed by v2 bootstrap/global/all-lane locks in the documented fixed
   order. Hold every descriptor through commit or rollback;
5. enumerate all v1/v2 caller processes, service activations, sockets, workers, timers, shell
   sessions and lock waiters by executable identity and cgroup. Because the bridge forbids waiting,
   any queued process or possible future lock acquirer is a hard refusal;
6. snapshot exact presence/absence, bytes, owner/group/mode, ACL, unit enablement/activity, trust,
   sudoers, kernel environments, adapters, journals, package stores, locks and manifests. Fsync and
   independently verify rollback authority;
7. while every lock remains held, atomically create/fsync the root-owned handoff epoch/fence. From
   this transition onward no v1 mutation may begin or continue. Disable submission services and
   atomically replace every discoverable v1 entry with a fail-closed tombstone;
8. stage v2 at a versioned path; verify its offline environment and package manifest. On first
   install, root confirms the owner public-key fingerprint and package digest out of band. On
   upgrade, the installed trusted key verifies the package and declared rotation;
9. install fixed tombstones/removals for every v1 mutating path while locks remain held. Atomically
   replace validated read-only sudoers first, then activate the one v2 mutating sudoers command last;
10. prove obsolete source/host paths, units, timers, aliases, workers, sockets, sudoers fragments,
    routes, locks and Menhir-owned Yawn/Caddy authorities absent; publish and fsync the terminal and
    absence receipts before releasing locks;
11. a v1 caller launched immediately before fence publication either finished before bootstrap
    acquired the global lock or already failed nonblocking; callers launched during activation or
    after activation see the fence/tombstone and fail. Focused tests exercise all three timings and
    assert zero post-fence v1 mutation and zero queued waiter.

If any post-snapshot step fails, rollback uses the stored bootstrap code to restore the exact v1
installation and prior absence/presence states while all locks and the fence remain. Only after the
bridge, v1 entries, units, sudoers and state are exactly restored may rollback remove/fsync the fence
and release locks. If exact restoration cannot be proven, the fence remains, product submit remains
disabled, and the bootstrap transaction is `blocked`. A half-v2/half-v1 “available” state is
forbidden.

## Ingress, operations gateway, and `yawn.deploy` contraction

Per ADR 0002, the shared `yawn.deploy` Caddy owns all public `memory.ctharvey.me` routing. Menhir
does not write, template, reconcile, or transact this configuration. The table below is the
**expectation Menhir verifies read-only** during preflight and adoption; `yawn.deploy` is its
authoritative source, and a change there is a `yawn.deploy` change, not a Menhir lane.

| Order | External matcher | Upstream | Upstream path | Backend authentication expectation |
|---|---|---|---|---|
| 0 | `http://` scheme, any path | None | None | Terminal `403` before TLS-authenticated routing |
| 1 | Exact `/ops/mcp` and prefix `/ops/mcp/` | `http://172.30.0.1:8000` | `uri strip_prefix /ops`; gateway receives `/mcp` | Gateway requires the operations OAuth bearer policy |
| 2 | Exact `/.well-known/oauth-protected-resource/ops/mcp` | `http://172.30.0.1:8000` | Unchanged; the path does not begin with `/ops` so `strip_prefix` does not apply | Gateway publishes protected-resource metadata; no bearer required for discovery |
| 3 | Exact `/mcp-http` and prefix `/mcp-http/` | `http://menhir-prod-app:8099` | Preserve unchanged | Menhir MCP OAuth challenge/token policy; suffixes remain routed for current compatibility but the app may return 404 |
| 4 | Exact `/oauth/authorize`, `/oauth/token`, `/oauth/register` | `http://menhir-prod-app:8099` | Preserve unchanged | Menhir OAuth endpoint-specific client/user policy |
| 5 | Exact `/.well-known/jwks.json` | `http://menhir-prod-app:8099` | Preserve unchanged | Public discovery |
| 6 | Exact `/.well-known/oauth-authorization-server` and prefix `/.well-known/oauth-authorization-server/` | `http://menhir-prod-app:8099` | Preserve unchanged | Public discovery; dynamic suffix is an application-defined RFC metadata route |
| 7 | Exact `/.well-known/oauth-protected-resource` and prefix `/.well-known/oauth-protected-resource/`, except the order-2 exact operations path | `http://menhir-prod-app:8099` | Preserve unchanged | Public discovery; dynamic suffix is an application-defined RFC metadata route |
| 8 | Exact `/livez` and `/readyz` | `http://menhir-prod-app:8099` | Preserve unchanged | Current health-endpoint policy |
| 9 | Same hostname, every other path | Terminal `404` | None | Denied before any origin |

TLS for the vhost terminates at the shared Caddy using a manually provisioned Cloudflare Origin CA
certificate and key, with Authenticated Origin Pull enforced as `require_and_verify` against the
Cloudflare origin-pull CA. Those materials, their mounts, and their rotation belong to
`yawn.deploy`. Menhir records their observed identity in the live-state composite and refuses on
mismatch; it never installs or replaces them.

The operations gateway keeps its current `/ops` `strip_prefix` contract. The previously specified
migration to a native ASGI mount at `/ops/mcp` is withdrawn. Internal `/mcp` is not publicly exposed
and is not an accepted alias. The gateway listener remains host-bound only to `172.30.0.1`, and
firewall/peer validation admits only the running shared Caddy service whose container, image,
labels, alias and network attachment match the recorded expected state. The address alone is not
identity.

The `menhir-proxy` network legitimately contains the shared Caddy, the Menhir app role, and the host
gateway endpoint. Menhir must not install a second ingress. Any Cloudflared tunnel definition in the
Menhir repository is non-target and is removed or explicitly marked as such.

Contraction is complete only when Menhir, shared scripts, Yawn VPS, and `yawn.deploy` source tests
agree that `yawn.deploy` retains ingress and holds no Menhir lock, release authority, journal, or GC
role; clean installation and upgrade tests prove the same host absence for the retired writers. The
Menhir release schema keeps `yawn_deploy` only as a read-only ingress expectation anchor and drops
Caddy release lock, Caddy release-authority, and Menhir bundle GC references. There is no
transitional release that accepts a second ingress.

## Retention, garbage collection, and archival

Packages are stored by digest. The kernel computes reachability under the common locks. Roots are:

- every nonterminal or blocked product/bootstrap transaction;
- current, previous and rollback control-plane generations;
- current, previous and rollback product generations;
- every retained snapshot/rollback manifest;
- every terminal receipt whose declared retention still requires executable recovery material.

GC is the common signed `gc` lane. A read-only kernel planner writes `gc-plan.v2` from current
anchors; the owner rehearses and authorizes that plan through `deployment-subject.v2`, staging,
preflight and `deployment-ticket.v2`, then submits it through the one mutating entry. The adapter
cannot accept a caller removal list. It starts the common attempt anchor/journal/snapshot protocol,
recomputes reachability under locks, requires the recomputed plan digest equal the authorized plan,
fsyncs terminal/journal/snapshot/package references first, removes only unreferenced scratch/package
objects, and emits both `gc-receipt.v2` and the common terminal receipt. Receipt, journal, ticket,
signature, envelope, staging, preflight, snapshot manifest, bootstrap and absence evidence are never
automatically deleted.

Moving durable evidence or deleting an expired local copy uses the common signed `archive` lane. A
read-only kernel planner creates `archive-plan.v2`; the owner rehearses and authorizes it through the
same subject/staging/preflight/ticket protocol and submits it through the one mutating entry. The
plan names exact digests, a destination identity from the installed allowlist, and source
disposition. The common journal/snapshot wraps the adapter, which copies, fsyncs, reads back, verifies
and emits `archive-receipt.v2` plus the terminal receipt before any source disposition.
Active/nonterminal/current/previous/rollback material is categorically ineligible.

## Shared PowerShell interfaces

The final interfaces are exact and versioned:

- `deploy-menhir.ps1 -SubmissionDirectory <existing-directory> -TransportManifestPath
  <existing-file-in-that-directory> -ExpectedManifestSha256 <64-lower-hex> -TargetHost
  <explicit-nonroot-endpoint> -ReceiptPath <nonexistent-file>`
- `deploy-menhir-app-only.ps1` is removed. App-only uses `deploy-menhir.ps1` with lane authority only
  inside the signed ticket.
- `menhir-scaffold.ps1 -SubmissionDirectory <existing-directory> -TransportManifestPath
  <existing-file-in-that-directory> -ExpectedManifestSha256 <64-lower-hex> -BootstrapHost
  <explicit-root-endpoint> -ReceiptPath <nonexistent-file>` is bootstrap transport only and cannot
  be called by a deployment ticket. The manifest necessarily transports the control-plane package,
  staging receipt, production preflight, bootstrap authorization, and detached signature.

The wrappers consume only the generated `transport-manifest.v2` shape: a bounded flat filename,
size, and digest list. They do not parse subject, ticket, lane, authority or package semantics. The
product wrapper hashes every listed file before and after upload, rejects unlisted local/remote
members, creates one random upload ID, transfers to the fixed inbox layout, invokes only
`menhir-txn submit <upload-id>`, receives a root terminal/adoption response, and creates the caller
receipt path atomically without overwriting. It may log observed wrapper and transport versions
locally, but those are not root claims or approval inputs. A missing/extra parameter, wildcard/path
ambiguity, existing receipt destination, changed local file during hashing or transfer, failed
remote invocation, malformed response, or digest mismatch fails locally without claiming success.

The scaffold wrapper sends the exact manifest-listed bootstrap submission to an explicit
unrestricted-root endpoint and invokes only the fixed bootstrap loader with the upload ID. On first
install, unrestricted root obtains that small loader from the same control-plane package and
executes it only after manually confirming both loader-member and complete-package digests against
the owner's out-of-band display; on upgrade, the installed loader digest must match the current
installed-artifact manifest. This first-install exception is within the documented unrestricted-root
trust boundary and does not allow a product caller to invoke bootstrap. The wrapper does not
construct remote commands from package content, select a host implicitly, install files, edit
sudoers, recover, or decide success. Windows PowerShell 5.1 and PowerShell 7 consume the same golden
argument, manifest, detached-signature, and byte-transfer fixtures.

## Read-only Yawn contract

The five Yawn operations map one-to-one to the five response schemas. HTTP and MCP layers parse the
version, validate exact fields, preserve structured errors and never scrape human text. Logs are
bounded by a fixed integer maximum and a kernel cursor; no arbitrary unit or file can be selected.

The Yawn source and installed manifests must reject all Menhir writer surfaces, including old deploy,
promote, maintenance, scaffold, install, recovery, worker, systemd/timer, sudoers and compatibility
aliases. Historical source files may remain only in Git history or explicitly archived documentation,
not in importable/executable/install manifests. The unrelated sealed-content/admin changes are
outside this architecture and must remain untouched.

## Phase derivation and stop/go rules

Only after this artifact is `COMPLETE` may the execution plan be rewritten. It must derive these
strictly serial deliverables; it may split a deliverable further but may not combine adjacent gates:

1. **Census and frozen contract.** Complete machine-readable writer/bypass/schema/record/host census,
   dispositions, golden identity fixture and frozen negative assertions. No architecture code.
2. **Canonical protocol.** Implement only schemas, canonical encoding, digest domains, registry,
   state transitions and generated fixtures/docs. No build, staging, host or wrapper changes.
3. **Immutable compilers.** Implement the two-repository product release, two-repository
   infrastructure package, and one-repository control-plane package from Git objects and clean
   builders. Prove package-type separation. No staging or host changes.
4. **Staging and read-only preflight.** Implement consumers of the frozen envelope and full
   live-state model. No signing or mutation.
5. **Signing and non-mutating authorization.** Implement key custody, trust and kernel authorize-only
   behavior. It must be impossible to reach snapshot/apply.
6. **Product kernel and adapters.** Implement descriptor-safe intake, locks, state machine, the six
   non-bootstrap lanes (`app-only`, `security-config`, `maintenance`, `infrastructure`, `gc`,
   `archive`), rollback, replay/adoption and retention on disposable hosts. `gc` and `archive` are
   signed lanes and therefore depend on gates 4 and 5, not on kernel-internal cleanup. No
   bootstrap/cutover.
7. **v1 handoff bridge.** Change only the censused v1 mutators to one global-first, nonblocking-lock,
   double-fence-check contract; prove it on disposable v1 fixtures. Add no v2 mutation or cutover.
8. **Bootstrap and installation.** Implement signed control-plane installation, all-lock handoff,
   fence, sudoers/read wrappers and crash recovery against the accepted bridge. No repository
   contraction.
9. **Cross-repository contraction.** Convert shared/Yawn interfaces and remove `yawn.deploy` Menhir
   ownership plus all v1 source/install paths in one coordinated compatibility break.
10. **Integrated clean-host acceptance.** Run complete non-production author/stage/preflight/sign/
   submit/retry/adopt/recover/rollback/bootstrap/converge flow and unchanged-behavior checks.

For each gate:

- the phase begins from its recorded exact commits and changes only owned surfaces;
- all deliverables and focused adversarial/crash/concurrency tests pass;
- source/install/host censuses have no unclassified item;
- a fresh full-phase review of the whole phase, not only the latest fix, has no P0-P2 finding;
- the closing commits, commands and evidence are recorded;
- only then may the next phase acquire an implementation commit.

If a later review finds an earlier invariant defect, the earliest owning phase reopens and every
dependent phase becomes invalid. A finding cannot be moved into a later phase to preserve a green
ledger. A workaround in a wrapper, test fixture or document does not close a kernel/schema defect.

## Required adversarial evidence

The derived plan must assign every item below to exactly one phase and no item may be waived by prose:

- dirty/untracked/submodule/symlink/case-collision/ambient-cache and Git-object substitution tests;
- offline dependency and builder/base-image trust-anchor failures;
- archive/config/layer/SBOM/scan/registry/workflow/attestation transitive mismatches;
- inbox directory/file swap, rename, truncate, append, link, special-file and held-FD races;
- duplicate/unknown/missing/stale/future schema and cross-record mismatch vectors;
- wrong/revoked/lost/compromised key, ACL, plaintext, argv/env/log/temp leak and clock cases;
- simultaneous lanes, lock-order, process interruption and every durable/mutation kill point;
- exact rollback for prior presence, absence, file metadata, unit states, containers, routes, backups
  and restore selection;
- replay matrix rows, terminal adoption with every live-state component changed individually, and
  attempt/upload collision;
- bootstrap caller begun before fence, during activation and after activation; queued waiter,
  malformed journal, old/new recovery boundary, sudoers validation and exact v1 restoration;
- retention reachability and refusal to GC active/current/previous/rollback evidence;
- source and clean-host absence of every retired command, route, unit, lock, sudoers entry, alias and
  manual instruction;
- PowerShell 5.1/7 exact arguments and transfer digests; five Yawn response schema consumers;
- unchanged app/API, Neo4j persistence, OAuth issuer/audience/client/scope/tier/PKCE/refresh behavior,
  MCP catalogs and allow/deny, ingress route denials, backups, restore and unrelated Yawn tests.

## Non-effects and explicit exclusions

This architecture does not authorize or require:

- an application feature, API, graph schema, database migration or data rewrite;
- a changed OAuth issuer, audience, client identity, callback, scope, tier, PKCE, consent or refresh
  policy;
- a changed MCP product tool catalog or access rule;
- a changed Neo4j volume/database identity or backup/restore format;
- a public app port or a second ingress;
- contacting production during implementation or local acceptance;
- a release, merge, push, deployment, host convergence, key enrollment, or artifact deletion;
- preserving obsolete mutation compatibility after contraction.

Any necessary semantic change in those areas requires a separate reviewed plan and cannot be hidden
inside control-plane implementation.

## Architecture acceptance ledger

| Check | Status | Evidence required |
|---|---|---|
| Repository and host ownership closed | READY FOR REVIEW | Fresh reviewer checks all five repository boundaries and the typed 2/2/1 repository-input decisions |
| Canonical repository identity registry closed | READY FOR REVIEW | Fresh reviewer confirms package type selects a fixed registry row and that caller-supplied URLs are never a trust anchor |
| Typed subjects and authorization families closed | READY FOR REVIEW | Fresh reviewer confirms every subject type has exactly one target/manifest/authorization/lane binding and that the two domain separators cannot cross-authorize |
| Protocol/record registry exhaustive | READY FOR REVIEW | Fresh reviewer maps every producer/consumer/state artifact and finds no missing authority record |
| Descriptor-safe intake closed | READY FOR REVIEW | Fresh reviewer validates the open-descriptor/fsync design, the post-copy source recheck, and the race matrix |
| Attempt reservation and durable commit primitive closed | READY FOR REVIEW | Fresh reviewer confirms one O_EXCL attempt anchor, one authoritative `HEAD`, complete transition table, and per-state expiry/revocation rules |
| Replay/adoption closed | READY FOR REVIEW | Fresh reviewer executes the decision table against all terminal/nonterminal cases |
| Owner key custody closed | READY FOR REVIEW | Fresh reviewer confirms format, ACL, passphrase, key ID, enrollment, rotation overlap, revocation, loss and compromise are one normative contract |
| v1 handoff bridge and bootstrap closed | READY FOR REVIEW | Fresh reviewer checks bridge-before-fence ordering, pre-fence/activation/post-activation callers, and queued-caller exclusion |
| Ingress and `yawn.deploy` ownership closed | CLOSED by ADR 0002 (2026-09-08) | `yawn.deploy` retains the vhost; Menhir installs no ingress writer and no second ingress |
| Privileged/read-only boundaries closed | READY FOR REVIEW | Fresh reviewer verifies one mutator plus five exact read operations |
| Phase ownership and invalidation closed | READY FOR REVIEW | Fresh reviewer maps each invariant/test to one serial phase |
| Fresh no-context architecture review | PENDING | Independent report with no open P0-P2 findings |
| Owner acceptance | PENDING | Explicit lifecycle decision after review |

Every row except the ADR-closed one reached `READY FOR REVIEW` through the 2026-09-08 architecture
revision preserved in commit `2a51408`. None of them has been independently reviewed since. Treat
this ledger as the entry condition for the fresh review, not as evidence that the designs are sound.

No implementation plan is sound until the last two rows close. If review finds a defect, edit this
specification as one architecture revision, reset every affected row to `READY FOR REVIEW`, and
review the complete document again. Do not patch the execution plan first.
