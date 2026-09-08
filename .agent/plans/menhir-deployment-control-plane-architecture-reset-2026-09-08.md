---
artifact_schema: 1
artifact_uuid: 22082266-930e-4f00-a8f7-e9116fea6d5c
artifact_type: plan
artifact_status: PROPOSED
supersedes: f4a95baf-e708-4571-ad6f-fef7ae4b072e
---

# Menhir deployment control-plane architecture reset

> **EXECUTION BLOCKED — ARCHITECTURE REOPENED 2026-09-08.** A later fresh review found that this
> plan still left root intake atomicity, the complete protocol registry, final `yawn.deploy`/ingress
> ownership, privileged-read wording, replay semantics, and bootstrap caller handoff insufficiently
> closed. The non-executable
> [`../reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`](../reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md)
> now owns those decisions. No phase below may start while that artifact is `OPEN`. After it is
> independently reviewed and moved to `COMPLETE`, rewrite this plan from the accepted specification
> and review the complete plan again. The phase text below is retained only as draft history and is
> not execution authority.

## Decision

Do not rewrite Menhir, its data plane, or its proven backup and release primitives from scratch.
Replace the deployment **control plane** through a gated strangler migration. Product releases,
infrastructure convergence, and privileged promotion must stop modifying or authenticating the
orchestrator that executes them.

The replacement has one versioned authority model, one root transaction kernel, one durable state
machine, and thin lane adapters. Every implementation phase closes with executable evidence before
work starts on the next phase. There is no dual-write period and no production deployment is
authorized by this plan.

The controlling architecture specification must first pass a fresh no-context review and receive
owner acceptance. This plan must then be rewritten from that closed specification, pass its own
fresh no-context review, and receive owner approval before Phase 0. Neither review authorizes
implementation, and implementation does not reactivate any deferred release or migration plan.

## Why

The failure pattern is architectural, not a shortage of individual guards. Between Menhir commit
`d4ec197` and reviewed head `f55e9f1`, deployment remediation added about 9,754 lines across 56 files.
The largest coordinators now range from roughly 459 to 1,477 lines, and the same release, approval,
attempt, receipt, live-state, locking, and recovery fields are parsed in Python, Bash, and
PowerShell. Shared wrappers added about 794 lines while independently reproducing those decisions.
This made each fix a new interpretation surface; a later review could correctly find another path
whose interpretation differed.

The current implementation contains useful, tested primitives. The repeated P1s cluster at their
seams: mutable authoring inputs, duplicated authority parsing, desktop receipt adoption, lane-local
journals, self-updating installers, and independently maintained wrappers. More patches at those
seams would increase the number of authorities again.

## Scope

In scope:

- Menhir release authoring, immutable source materialization, dependency/image provenance,
  staging, approval, promotion, adoption, rollback, and infrastructure convergence;
- the three shared operator entry points under `C:\Users\thron\IdeaProjects\scripts`;
- the read-only Menhir integration in `projects/yawn/yawn.vps`, including removal of obsolete
  installation and mutation instructions;
- migration from the current scripts to one control-plane protocol and retirement of every old
  production mutation path;
- continued immutable-input authority for `archolith_oauth` and `yawn.deploy`, without bringing
  either repository's implementation into this control-plane rewrite;
- contracts protecting application, Neo4j, OAuth/MCP, Cloudflared ingress, backup, and restore
  behavior from unintended change.

Out of scope:

- application features, graph/data-model changes, database migrations, provider/model changes,
  or a new deployment platform;
- production access, release, merge, deployment, or host convergence while this plan is being
  implemented;
- replacing Docker Compose, Cloudflared, GitHub Actions attestations, encrypted backup formats,
  or the proven lane mutation/rollback commands unless a focused phase proves a defect in one;
- retaining compatibility for an obsolete mutation path after the contraction gate.

## Baseline and repository boundary

The implementation baseline and immutable release-input boundary are below. Re-read heads and dirty
state when implementation begins and record any deliberate change in the execution ledger.

| Repository | Baseline | Boundary |
|---|---|---|
| Menhir worktree | `f55e9f1a082d7df17dffecde568ffc85d063b4c6` | Owns schemas, release compiler, staging verifier, signed authorization client, root kernel, adapters, fixtures, and canonical docs. |
| Shared workspace | `6c9a9086716312e7c17051adfc82e7b52905e0b9` | Only `scripts/deploy-menhir.ps1`, `scripts/deploy-menhir-app-only.ps1`, and `scripts/menhir-scaffold.ps1` are in scope. Existing unrelated work is never staged or changed. |
| Yawn VPS | `b1191b85962f83812ab805fe8d467dd96312741d` | Menhir surface remains read-only. Existing unrelated changes in the four sealed-content/admin files remain untouched. |
| Archolith OAuth | Re-read at Phase 0 | Immutable release input. One exact commit supplies the OAuth wheel/source identity. Its repository still participates in the complete writer/bypass census even though no implementation change is pre-authorized. |
| Yawn deploy | Re-read at Phase 0 | Immutable release input and current owner of shared Compose/Caddy files. One exact commit supplies configuration identity. Its Menhir-linked locks, routes, release authority, and root scripts are mandatory census entries; no implementation change is pre-authorized until Phase 0 assigns a disposition. |

`deploy/RELEASE_AUTOMATION.md`, `deploy/LIVE_VPS_PLAYBOOK.md`, and `deploy/PRODUCTION.md` describe
the accepted operator flow only after executable contracts exist. They are not independent policy
sources.

The prior production-release plan and Contabo migration plan are `DEFERRED` and non-executable as
of this plan's review. The former may be replanned against the completed control plane; the latter
is retained as historical topology/migration rationale only. Neither authorizes use of the existing
deployment machinery, Caddy ownership, or a Yawn Menhir writer during this work.

## Architectural invariants

| Invariant | Single authoritative enforcement point |
|---|---|
| Release bytes come only from one clean, immutable commit per declared repository; untracked or ambient worktree bytes cannot enter. | Release compiler materializing Git objects, before any build. |
| Dependencies, image config/layers, archive, SBOM, scan, registry digest, workflow identity, and repository identity form one transitive provenance chain. | Release-envelope verifier, before publication or staging. |
| Staging runs the exact release envelope on a disposable host and emits one immutable result. | Staging verifier on its root-owned staged copy. |
| Read-only production preflight observes one exact prior live state and emits a short-lived immutable result. | Production preflight verifier before approval, then root kernel revalidation under lock. |
| A human approval authorizes exactly one release envelope, staging result, production-preflight result, expected pre-state composite, lane, attempt, root-kernel version, and expiry. | Root kernel verifying an owner signature, before acquiring mutation intent. |
| No routine product or infrastructure path mutates before admission; all such paths serialize on the same kernel-held lock. The separately authorized bootstrap transaction co-acquires v1/v2 locks before its first write. | Root transaction kernel; bootstrap transaction only for kernel/trust/sudoers replacement. |
| Journal transitions, snapshots, rollback arming, receipt creation, adoption, and live-state equality are atomic and kernel-owned. | Root transaction kernel. |
| Retry never trusts a desktop receipt and never reinterprets state with a newer lane implementation. | Root kernel under both admission and lane locks, using the transaction's stored adapter and package. |
| Product releases cannot replace the root kernel, sudoers, trust keys, or recovery code that authenticates them. | Package-type schema plus root kernel. |
| Infrastructure changes use a distinct signed package and transaction but the same admission and journal protocol. | Infrastructure adapter invoked by the root kernel. |
| Exactly one installed privileged entry point exists after cutover; Yawn exposes no Menhir writer. | Installed-artifact manifest, sudoers contract, and source/host censuses. |
| App, database, OAuth/MCP, ingress, backup, and restore semantics change only when declared in the lane plan. | Lane pre/postcondition manifest and acceptance verifier. |

These are system invariants. A source-string assertion, wrapper-side guard, or passing happy-path
test is not closure evidence for any of them.

## Single sources of truth

Create one dependency-light Python package, `deploy/control_plane/`, consumable by the release CLI,
test harness, and root installation. It owns canonical JSON encoding, duplicate-key rejection,
strict schemas, digest construction, state transitions, and compatibility rules. No PowerShell or
Bash script reimplements those semantics.

| Authority | Canonical representation | Consumers |
|---|---|---|
| Release envelope | `release-envelope.json`, content-addressed and produced from Git objects | publication, staging, approval client, root kernel |
| Staging result | `staging-receipt.json`, bound to the complete release-envelope digest | approval client and root kernel |
| Production pre-state | `production-preflight-receipt.json`, short-lived and bound to the complete expected live-state composite | approval client and root kernel |
| Promotion authorization | canonical `promotion-ticket.json` plus detached owner signature | root kernel only; wrappers transport bytes |
| Transaction state | root-owned `journal.json` in a transaction directory named by the ticket/attempt digest | root kernel and read-only status tools |
| Terminal result | atomically published root receipt containing the composite live-state digest | root adoption/status and desktop copy |
| Installed control plane | signed control-plane package manifest plus installed-artifact manifest | bootstrap verifier, root kernel, scaffold audit |
| Protocol and lane contracts | versioned schemas and lane pre/postcondition definitions in `deploy/control_plane/` | generated JSON Schema/docs, all contract tests |

Owner approval must be cryptographically distinguishable from possession of sudo. Use a dedicated
Ed25519 deployment signing key and pin its public verification key and key ID in the control-plane
installation. Reuse the repository's hash-locked `cryptography` implementation in a dedicated,
offline-built root-kernel environment; the verifier and every wheel are part of the signed
control-plane manifest. The private key never enters the VPS. The canonical ticket bytes include
all semantic authority digests, expected pre-state composite, lane, attempt ID, issue/expiry times,
and expected root-kernel digest. Tickets have a maximum 30-minute lifetime, reject issue times more
than two minutes in the future, and require the bound production preflight to be no more than 15
minutes old. Phase 1 freezes those values in the schema rather than leaving runtime defaults.

The trust store records key ID, algorithm, public key, validity interval, and revocation state.
Rotation is a signed bootstrap transaction with an explicit bounded overlap; revocation immediately
invalidates unstarted tickets. Product packages cannot install or rotate keys. This protects the
narrow deployment sudo capability from a caller who has SSH and only that allowlisted sudo command.
Unrestricted root can replace the kernel or trust store and is therefore explicitly an out-of-band,
audited break-glass/recovery authority, not part of the threat claim.

The signed authorization contains the approval-client digest because it authored the ticket. The
shared PowerShell wrapper is semantically inert and is not an authorization claim: a local transport
log may record its observed digest, but the root receipt must not claim that the wrapper executed or
make success depend on its identity.

The custody profile is also part of the Phase-1 contract. Enrollment generates a dedicated Ed25519
key on the owner's workstation as encrypted PKCS#8 at
`%USERPROFILE%\.menhir\keys\deployment-owner-ed25519.pem`; the directory and file must have
inheritance disabled and grant access only to the owner and Windows `SYSTEM`. The signing client
refuses an unencrypted key, permissive ACL, redirected/non-interactive secret input, a repository
path, or a key supplied through argv/environment. It prompts for the passphrase interactively and
never writes private bytes, passphrases, or decrypted material to logs, receipts, packages, upload
directories, or persistent temporary files. The key ID is the SHA-256 fingerprint of the canonical
public key and is confirmed out of band during bootstrap enrollment.

Maintain one owner-controlled, encrypted offline backup outside all synchronized/workspace paths;
record only its public fingerprint in operations evidence. Key loss blocks promotion until an
unrestricted-root bootstrap ceremony enrolls a replacement and records the old key as unavailable.
Suspected compromise immediately freezes deployment, revokes the key through that ceremony,
invalidates every unstarted ticket, rotates to a new key, and retains a revocation receipt. Hardware
backing may replace the file profile only through a separately reviewed signer/verifier extension;
it is not silently assumed by this plan.

Documentation tables and examples are generated or tested against these schemas. They explain the
protocol but do not redefine fields, defaults, chronology, or allowed transitions.

## Target component boundaries

1. **Release compiler (unprivileged, no production access).** Materializes every input from the
   declared Git commit, rejects dirty/untracked source selection, performs hash-locked offline
   builds, verifies the CI trust chain, and emits a release envelope. The install-bundle builder and
   installer are themselves commit-addressed inputs in that envelope.
2. **Staging verifier (disposable host only).** Accepts one release envelope, copies it to a
   root-owned transaction directory, verifies image archive/config/layers and all attestations,
   runs the declared lane rehearsal, and emits one atomic receipt. It cannot approve or promote.
3. **Production preflight verifier (read-only host path).** Under the common locks, reads the current
   release, effective config, app/database/ingress identities, OAuth/MCP policy, backup/restore
   readiness, and installed control-plane identity. It emits a short-lived atomic receipt and makes
   no mutation. The root kernel reruns the same observations under lock and demands exact equality
   before arming a transaction.
4. **Approval client (owner workstation).** Displays the exact envelope, staging digest, production
   preflight digest, and expected pre-state composite; creates a one-attempt expiring ticket; and
   signs its canonical bytes. It cannot mutate production.
5. **Transport wrappers.** The shared PowerShell scripts accept an explicit package/ticket/receipt
   destination and perform upload/invocation/copy only. They contain no classification, approval,
   chronology, adoption, rollback, bundle discovery, or live-state policy.
6. **Root transaction kernel.** A small Python executable in a signed, hash-locked offline
   environment owns strict parse, signature verification, package verification, common admission,
   locks, durable journal,
   snapshot, transition validation, adapter dispatch, rollback, terminal receipt, adoption, and
   post-lock live-state equality. It never executes caller-supplied shell text or paths.
7. **Versioned lane adapters.** Maintenance, app-only, security-config, and infrastructure adapters
   implement `preflight`, `snapshot`, `apply`, `verify`, and `rollback` against fixed package
   members. The transaction stores the exact adapter bytes before first mutation and always uses
   that copy for recovery.
8. **Read-only integration.** Yawn may report status through exact read wrappers. It has no submit,
   mutation, installer, worker, or compatibility alias for Menhir operations.

The root kernel is control-plane infrastructure. Routine release bundles contain product/runtime
artifacts only and cannot update it. A kernel upgrade uses a separately signed, clean-host-rehearsed
bootstrap package and an explicit root ceremony with its own rollback anchor.

## Frozen target interface

Phase 1 owns the protocol schemas; Phase 4 proves authorization; Phase 7 performs the one-time caller
conversion. The intended surface is:

| Caller | Target contract |
|---|---|
| Menhir owner CLI | `menhir deploy submit --package <path> --ticket <path> --signature <path> --receipt <new-path>`; lane and all authority come from the signed ticket, never flags. |
| Shared product transport | `deploy-menhir.ps1 -PackagePath <path> -TicketPath <path> -SignaturePath <path> -ReceiptPath <new-path>`; upload exact bytes, invoke the fixed root entry point, atomically copy returned receipt. |
| Privileged product entry | `sudo -n /usr/local/libexec/menhir-txn submit <32-lowercase-hex-upload-id>`; the ID selects one fixed inbox directory. Repeating `submit` performs kernel-owned resume or adoption for the same signed attempt. |
| Shared app-only transport | `deploy-menhir-app-only.ps1` is retired; app-only is a signed lane in the common product protocol. |
| Bootstrap transport | `menhir-scaffold.ps1 -PackagePath <path> -ExpectedPackageSha256 <digest> -BootstrapHost <explicit-root-endpoint>`; transport only, for the separately authorized bootstrap ceremony. |
| Yawn integration | The existing five Menhir read operations remain read-only and consume the versioned status/release/backup/generation JSON contract; no submit/recover/promote tool exists. |

The product sudoers surface contains one executable path. Its `submit` implementation accepts only a
bounded upload ID, resolves only beneath the fixed inbox, rejects links/special files/ownership or
mode drift, and copies verified bytes to root-owned storage before use. Read-only `status` operations
may remain direct fixed wrappers but cannot transition state. The bootstrap root endpoint is not in
product sudoers.

## Implementation gates

The phases below are serial. A phase is `DONE` only when all deliverables and tests that its own
implementation can satisfy pass, the writer/bypass census has no unclassified entry, its complete
phase review has no open P0/P1/P2 defect, and the exact closing commit is recorded in the execution
ledger. Target-state assertions owned by later phases are recorded as pending acceptance items, not
committed as failing tests and not used to block an earlier gate. The next phase must not contain
code before that. If a later phase exposes an earlier invariant defect, reopen the earliest owning
phase and invalidate every dependent gate.

### Phase 0 — Freeze, census, and executable contract

- Freeze the current deployment branch as a reference; prohibit production mutation from it.
- As the first documentation-only change, mark the shared-script and Yawn Menhir runbooks frozen and
  link them to this plan. They retain no current mutation/install authority.
- Enumerate every release input, deploy/scaffold entry point, sudoers command, systemd unit, timer,
  root executable, writer, recovery command, receipt, journal, lock, and documentation procedure
  across all five declared repository boundaries—Menhir, shared workspace, Yawn VPS,
  `archolith_oauth`, and `yawn.deploy`—plus generated install manifests. Read-only or input-only
  classification never exempts a repository from the census; `yawn.deploy` Caddy/lock/root surfaces
  require explicit preserve/adapt/retire decisions.
- Record each as `preserve`, `adapt`, or `retire`, with one target owner. Include absent/legacy and
  already-completed transaction states.
- Write a machine-readable census test that passes only when every current path has exactly one
  `preserve`, `adapt`, or `retire` disposition. Record target-state absence assertions for Phase 7
  without committing a deliberately failing suite. Add a non-mutating protocol fixture that carries
  one synthetic identity through every planned document shape.

Gate: competing deployment/release plans and current operator docs are explicitly deferred/frozen;
the census is complete and mechanically checked; every mutator and bypass has a disposition; no
architecture implementation has started.

### Phase 1 — Authority schemas and state machine

- Implement canonical encoding and strict versioned schemas for the release envelope, staging
  receipt, promotion ticket/signature, journal, lane plan, terminal receipt, live-state composite,
  control-plane package, and installed-artifact manifest.
- Define legal journal transitions and the exact atomic write/fsync/rename protocol.
- Define compatibility policy: unknown fields, versions, algorithms, adapters, missing values,
  duplicate keys, expired tickets, and partial files fail closed.
- Generate schema documentation and cross-language fixtures from this package.

Gate: property/fixture tests cover valid, malformed, duplicate, absent, stale, future-version, and
cross-document mismatch cases; the new `deploy/control_plane/` code has exactly one parser per
schema. Frozen v1 parsers remain classified for Phase 7 retirement and receive no new policy.

### Phase 2 — Immutable release compiler

- Preserve the existing release cardinality—one exact commit each for `menhir`,
  `archolith_oauth`, `yawn_deploy`, and `yawn_vps`—unless a separately reviewed schema migration in
  this phase proves an input is no longer consumed. The shared wrapper repository is implementation
  tooling, not a release-content repository.
- Replace worktree copying with Git-object materialization for every release input, including the
  bundle builder and installer. Reject a missing commit/object, submodule ambiguity, untracked
  source, dirty selection, and nondeterministic output.
- Preserve hash-locked dependencies, offline wheelhouse construction, CI repository/workflow trust
  roots, SBOM/scan/archive/image config/layer lineage, and canonical digest-only image references in
  one release envelope.
- Build the same commit twice in independent clean directories and require byte/digest equality for
  all deterministic artifacts; enumerate and justify any signed timestamp exception.

Gate: substitution and ambient-input tests fail before publication, two clean builds agree, and a
Phase-2 no-op consumer can validate the envelope without consulting any checkout. Execution by the
real staging consumer is owned by Phase 3.

### Phase 3 — Staging as a pure verifier

- Rebuild staging to consume only the release envelope and root-owned copy.
- Verify exact image archive, image ID, config, layers, dependencies, release configuration, runner,
  and production-equivalent preflight before starting a disposable stack.
- Preserve current OAuth PKCE, MCP allow/deny, restart, rollback, isolation, and resource checks.
- Emit the staging receipt atomically; retries use the stored envelope/adapter and never current
  checkout bytes.
- Implement the separate read-only production-preflight receipt and composite. It may contact the
  production host only when an owner explicitly authorizes that operational read; all ordinary
  Phase-3 tests use a disposable host/fixture. It never becomes part of disposable staging.

Gate: clean-host staging passes; tamper, mutable-tag, wrong-layer, current-worktree, and
interrupted-retry cases fail without production contact. A disposable production-preflight fixture
proves atomic output, age calculation, and complete pre-state composition; no live preflight is
required to close the code phase.

### Phase 4 — Signed approval and non-mutating root authorization

- Implement Ed25519 key generation/signing, trust-store schema, rotation/revocation rules, clock
  policy, the required encrypted-PKCS#8/owner+SYSTEM ACL custody profile, enrollment fingerprint,
  offline backup/loss/compromise procedures, approval display, and bounded ticket lifetime.
- Implement the root kernel's non-mutating `authorize` path: strict parse, trusted-key lookup,
  signature verification, ticket/preflight age, expected kernel digest, and exact under-lock
  pre-state recomputation. It must stop before snapshot or mutation.
- Freeze the future transport protocol as generated fixtures. Do not convert shared wrappers in
  this phase; Phase 7 owns their one-time conversion after the mutation kernel exists.

Gate: a caller possessing SSH and only the allowlisted deployment sudo command cannot pass
`authorize` without a valid, unexpired owner signature and unchanged pre-state. Wrong/revoked keys,
clock skew, stale preflight, changed live state, changed kernel, and replayed attempts fail before
mutation. Unencrypted/permissive/repository-resident keys, non-interactive secret injection, and any
private-key/passphrase leakage into argv, environment, logs, receipts, packages, uploads, or temp
files also fail focused tests. Loss and compromise drills prove the freeze/re-enrollment/revocation
paths. Unrestricted root is documented and tested only as the break-glass exception.

### Phase 5 — Root transaction kernel and lane adapters

- Install the kernel in a disposable root filesystem and implement one common admission path for
  maintenance, app-only, security-config, and infrastructure.
- Before first mutation, verify ticket signature/expiry, envelope/staging/adapter/kernel/package
  digests, acquire the global admission and lane locks, copy immutable inputs, snapshot prior state,
  fsync the journal, and durably arm rollback.
- Move existing mutation primitives behind fixed adapter methods. The kernel alone records
  chronology, transitions, receipts, and rollback outcome.
- Adoption reacquires both locks and recomputes the live-state composite: release authority,
  effective configuration, app image/container, database identity, ingress identity, OAuth/MCP
  policy, installed control-plane version, and lane-specific state must equal the terminal receipt.
- Exercise kill points after every durable transition and every mutation. Recovery uses the stored
  kernel-compatible adapter and package; exact prior file, directory, unit, sudoers, container,
  route, backup, and configuration state is restored or the transaction remains explicitly blocked.
- Store packages content-addressed. Automated kernel GC must retain every package/adapter referenced
  by a nonterminal transaction, current/previous/rollback generation, or retained snapshot. It may
  remove terminal scratch copies only after the terminal receipt, journal, snapshot manifest, and
  package-store reference are fsynced. Receipt/journal evidence is never automatically deleted;
  archival or destructive retention is a separate signed transaction. GC itself runs under the
  common locks and emits an atomic receipt.

Gate: a crash matrix, stale-adoption matrix, cross-lane concurrency test, direct-root invocation
test, and exact rollback comparison all pass on a clean disposable host.

### Phase 6 — Infrastructure and bootstrap transaction

- Convert scaffold/Ansible output into one signed infrastructure package and lane plan. Normal
  convergence updates only declared host artifacts through the kernel and shares its global lock.
- Keep kernel/trust-key/sudoers bootstrap separate from product release. Specify a distinct
  root-operated bootstrap transaction with states `preflight -> drained -> snapshotted -> staged ->
  verified -> activated -> v1-retired -> committed` and reverse transitions to `rolled-back` or
  `blocked`.
- Before bootstrap mutation, resolve every v1 nonterminal transaction, acquire v1's admission/lane
  locks and the future v2 locks in a fixed order, and hold all descriptors through activation or
  rollback. Snapshot the old kernel/scripts, trust store, sudoers, units, timers, and manifests;
  fsync rollback authority before installing anything.
- On first install, unrestricted root explicitly confirms the owner public-key fingerprint and
  signed package digest out of band. On upgrade, the installed trusted key verifies the package.
  Stage v2 at a versioned path, verify its offline environment and manifest, atomically activate
  the new narrow sudoers entry last, disable/remove v1 writers while locks remain held, then emit an
  old-path-absence receipt. Any failure restores the exact v1 state before releasing locks.
- Make check mode compare the desired package to the actual pre-state without assuming post-state.
- Generate the installed-artifact and obsolete-artifact manifests; replacement of sudoers is atomic
  and validated before activation.

Gate: first install, v1 drain, upgrade, interruption after every bootstrap transition, old/new
recovery boundary, exact unit/sudoers/key/kernel restoration, lock handoff, and least-privilege sudo
tests pass. The receipt proves v1 path absence and product packages cannot alter control-plane
infrastructure.

### Phase 7 — Cross-repository convergence and contraction

- Convert all three shared wrappers once to the frozen transport contract and verify the Menhir
  caller contract on Windows PowerShell 5.1 and PowerShell 7. A missing or extra argument fails
  before upload. Yawn remains on the read-only status contract.
- Delete/disable old desktop coordinators, root runners, submit wrappers, workers, systemd units,
  timers, sudoers entries, compatibility aliases, mtime discovery, and manual mutation/install
  instructions identified in Phase 0.
- Add source and installed-host negative censuses that require obsolete names and command patterns to
  be absent. There is no fallback to v1 after this gate.
- Update the three canonical Menhir documents from generated contract tables and make other
  repository docs link to them rather than restate procedures.

Gate: all three repositories agree on arguments, schemas, digests, return codes, receipt locations,
and read/write boundaries; a clean install exposes exactly one privileged entry point.

### Phase 8 — Integrated acceptance

- From exact clean checkouts, build the control-plane and product packages and run the complete
  non-production flow: author, stage, approve with a test key, promote to a disposable host, adopt,
  retry, recover, rollback, and infrastructure convergence.
- Prove application, Neo4j data, OAuth/MCP discovery and authorization, Cloudflared ingress, backups,
  restore selection, and unrelated Yawn behavior are unchanged except for declared deployment
  interfaces.
- Exercise the complete access contract: stable CIMD/DCR identities and callbacks, explicit
  consent, JWT issuer/audience/client/scope/tier binding, refresh-token rotation, restart continuity,
  exact product roles/tool catalogs, PKCE, and allow/deny behavior.
- Run focused local suites during development, then required CI once on the exact integrated commits.
- Obtain one independent full-system implementation review of the exact implementation and immutable
  input revisions. A delta-only review is not acceptance; the plan's own no-context review is a
  pre-Phase-0 gate recorded below.

Gate: exact-sha CI is green, clean-host rehearsal is green, no P0-P2 code defect is open, all old
mutation paths are absent, and remaining production prerequisites are explicitly operational.

## Commit and review discipline

- One phase per linear commit series and pull request. Commit messages include the phase number.
- A phase PR may change its owned component, generated schemas/docs, and focused tests only. It may
  not opportunistically repair a later phase.
- The phase review packet contains the baseline, exact head, full phase diff, invariant map, census
  diff, commands/results, and explicit non-goals. It never asks a reviewer to trust a prior summary.
- P0-P2 findings keep that phase open. Fixes are reviewed against the complete still-open phase, not
  just the latest patch. P3 cleanup is either completed before closure or recorded as a separately
  owned, non-safety backlog item.
- The execution ledger is append-only. A changed closing SHA or reopened invariant invalidates the
  gate and every dependent gate; prose cannot waive it.
- Do not combine implementation, remediation, and release authorization. Completing this plan only
  makes a candidate eligible for CI and rehearsal; it never authorizes production.

## Preserve, adapt, retire

Preserve behind the new interfaces:

- CI provenance and attestation verification, Git history bundles, image archive/config/layer checks;
- dependency lock and offline wheelhouse behavior after Phase 2 proves it transitively;
- existing release classification rules and production-equivalent staging assertions;
- fixed lane mutation, health, backup, restore, and rollback primitives whose focused tests pass;
- Cloudflared topology, one Menhir app, one Neo4j store, OAuth/MCP policies, and backup formats;
- read-only Yawn Menhir status tools.

Adapt:

- `deploy/release_flow.py`, `release_spec.py`, `release-author.py`, and
  `build_install_bundle.py` into the release compiler and envelope verifier;
- `personal_deploy.py` and staging code into explicit clients of the canonical schemas;
- `release-install.sh`, `menhir_app_only.py`, `menhir_security_config.py`, and scaffold/Ansible
  operations into fixed lane adapters without journal or authority ownership;
- the shared PowerShell scripts into transport-only shims.

Retire after Phase 7 proves replacement:

- `deploy/personal_promote.ps1` authority/adoption logic and every desktop-local success decision;
- mutable current-worktree installer/scaffold recovery inputs;
- lane-specific journal schemas and receipt writers;
- inline remote policy programs, implicit/default/mtime bundle selection, and wrapper-side
  classification;
- product-bundle installation of root control-plane executables or sudoers;
- Yawn manual root installation instructions and all Menhir mutation wrappers, aliases, workers,
  or services outside the canonical root kernel.

## Validation matrix

Required focused evidence includes:

- schema golden vectors consumed by Python and PowerShell transport tests;
- deterministic clean-checkout builds, dirty/untracked/substitution negatives, and offline failure;
- wrong repository/workflow/attestation/SBOM/scan/archive/config/layer/image negatives;
- stale, future, replayed, foreign-attempt, expired, unsigned, wrong-key, and changed-runner tickets;
- encrypted signing-key storage/ACL/enrollment, leak negatives, offline-backup fingerprint, loss
  recovery, compromise revocation, and invalidation of unstarted tickets;
- simultaneous lane starts, lock interruption, malformed/partial journals, and every crash point;
- completed receipt with changed app, database, ingress, config, policy, release, or kernel state;
- first-install and upgrade rollback, including absent files, unusual stable unit states, and retired
  artifacts;
- sudoers exact-command negatives and direct invocation without authorization;
- clean-host installation plus old-path absence;
- application/API and Neo4j persistence; stable CIMD/DCR callbacks; explicit consent; JWT
  issuer/audience/client/scope/tier; refresh rotation; restart continuity; exact product roles/tool
  catalogs; OAuth PKCE/MCP allow/deny; ingress; backup; restore; and Yawn regression checks;
- package/adapter retention and GC refusal for active/current/previous/rollback references, plus
  atomic terminal cleanup and separately authorized archival.

The structural scanner requested by repository guidance is not available in this environment.
Phase 0 uses `rg`, Git history, generated manifests, sudoers/systemd inventories, and executable
contract tests; implementation may add a structural scanner only as a read-only census aid, never
as a second source of policy.

## Risks and mitigations

- **Large migration.** Keep old production code frozen while v2 is built and tested inertly; do not
  permit both to mutate. Cut over once, then delete v1.
- **Bootstrap circularity.** Product releases cannot update their verifier. Kernel/key changes use a
  separate signed bootstrap package and exact rollback ceremony.
- **Schema drift.** One library generates fixtures/docs; unknown versions fail closed. Cross-repo
  tests consume the same committed vectors.
- **Recovery across versions.** Each active transaction stores its adapter and declares the compatible
  kernel range. Upgrade refuses while a transaction is nonterminal.
- **False confidence from tests.** Each gate includes adversarial, crash, concurrency, and absence
  evidence plus a fresh full-phase review; final acceptance uses complete clean checkouts.
- **Operational burden of signatures.** Provide one bounded owner command that shows the exact ticket
  summary and signs it. Never hide or auto-create approval.

## Documentation and records to update during implementation

- `deploy/RELEASE_AUTOMATION.md`
- `deploy/LIVE_VPS_PLAYBOOK.md`
- `deploy/PRODUCTION.md`
- `deploy/ansible/README.md`
- `.agent/scripts-index.md`
- Menhir and workspace changelogs
- shared `scripts/README.md`
- Yawn `ops/menhir/README.md` and its Menhir operation tests
- the implementation wrapup, with exact three-repository anchors and phase ledger

## Operational gates separate from code completion

Even after Phase 8, production remains blocked until an owner separately confirms current backups
and restore evidence, host capacity, installed kernel/key identity, exact release/staging/ticket
digests, CI on the final pushed revisions, and an approved maintenance window. Those are operational
go/no-go inputs, not defects to patch around and not evidence that this architecture plan was
implemented.

## Execution ledger

| Phase | Status | Closing commit(s) | Review | Evidence |
|---|---|---|---|---|
| Architecture specification | OPEN | documentation worktree | pending fresh no-context review | [`../reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`](../reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md) closes system ownership before executable phase derivation. |
| Plan review | REOPENED | documentation worktree | task `01a082b1-1726-74c2-ad21-c8146b0fd8f2` | Later full-plan pass found three P1 and three P2 design gaps; prior `PLAN SOUND` verdict is invalidated. |
| 0 | NOT STARTED |  |  |  |
| 1 | NOT STARTED |  |  |  |
| 2 | NOT STARTED |  |  |  |
| 3 | NOT STARTED |  |  |  |
| 4 | NOT STARTED |  |  |  |
| 5 | NOT STARTED |  |  |  |
| 6 | NOT STARTED |  |  |  |
| 7 | NOT STARTED |  |  |  |
| 8 | NOT STARTED |  |  |  |

Plan review record: an earlier review's final `PLAN SOUND` verdict was superseded on 2026-09-08 by a
fresh full-plan pass. That pass found three P1 issues—race-safe root intake, an incomplete protocol
record registry, and undecided `yawn.deploy`/Cloudflared ownership—and three P2 issues—ambiguous
privileged-entry wording, incomplete replay rules, and a bootstrap handoff race for already-started
v1 callers. The plan is therefore `PROPOSED`, not reviewed. Those decisions now belong to the linked
architecture specification. Phase 0 remains blocked until that specification is complete and this
entire plan is re-derived and reviewed; owner approval remains a later separate lifecycle decision.
