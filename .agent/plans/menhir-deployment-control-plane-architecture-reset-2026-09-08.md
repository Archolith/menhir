---
artifact_schema: 1
artifact_uuid: 22082266-930e-4f00-a8f7-e9116fea6d5c
artifact_type: plan
artifact_status: PROPOSED
supersedes: f4a95baf-e708-4571-ad6f-fef7ae4b072e
---

# Menhir deployment recovery plan and current status

## Current status — 2026-09-08

**Verdict: NOT READY. Implementation and production work are frozen.**

The deployment-reliability branch contains substantial implementation and multiple rounds of
remediation, but it has not reached a stable, independently accepted control-plane design. Fixing
individual P1 findings repeatedly exposed other paths that implemented the same rule differently.
The problem is concentrated in deployment architecture and ownership, not in the Menhir application
or Neo4j data model.

Current repository anchors at the time this status was written:

| Repository | Revision | Status relevant to this plan |
|---|---|---|
| Menhir | `c5b09df7f79b68885e863b8deea65454956b8476` before this status update | Branch `fix/deployment-reliability-20260907`; deployment code frozen |
| Shared workspace | `6c9a9086716312e7c17051adfc82e7b52905e0b9` | Only the three Menhir PowerShell wrappers are in scope |
| Yawn VPS | `b1191b85962f83812ab805fe8d467dd96312741d` | Menhir integration must end read-only; unrelated sealed/admin changes stay excluded |
| Archolith OAuth | `8b9d8eb3a3016f48359b93c3d15ae95c2c47bef8` | Immutable OAuth source input only |
| Yawn deploy | `4937657b9ebde7d3ca128f8924da724203c37a81` | Retains `memory.ctharvey.me` ingress by ADR 0002; still holds Menhir lock/release/journal/GC ownership in source that must be retired |

The long architecture specification at
[`../reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`](../reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md)
is an `OPEN`, rejected design draft. It is not execution authority. Its independent review returned
`ARCHITECTURE NEEDS REVISION` with six P1 and two P2 issues, of which **P1 #5 is now closed by
[ADR 0002](../adr/0002-menhir-production-ingress-ownership.md)**, leaving five P1 and two P2. Do
not implement from it and do not interpret partial corrections as acceptance.

No production deployment, host mutation, release, merge, or key enrollment is authorized by this
plan. Existing production behavior remains the operational baseline until a replacement completes
all disposable-host and CI gates and receives a separate production decision.

## What went wrong

The system does not need to be rebuilt from scratch. The current code has useful release,
attestation, staging, backup, restore, health-check, and rollback primitives. The failure is that
release meaning, approval, admission, locking, recovery, receipt adoption, and installation are
interpreted in several Python, Bash, and PowerShell paths across multiple repositories.

That creates a predictable cycle:

1. a review finds one path that can bypass or reinterpret an invariant;
2. a local fix adds another guard or record;
3. another path still has the old interpretation;
4. the next full review reports a new P1 at that seam.

The remedy is not another full-system patch. Work must be reduced to small serial phases with one
owner, one artifact set, one bounded review, and an enforced stop between phases.

## Open issues

These are architecture issues, not deferred implementation details.

### P1

1. **Authorization is not type-complete.** Product releases, infrastructure packages, and
   control-plane/bootstrap packages do not yet share a fully defined subject and evidence binding.
   Bootstrap transport does not yet have a final non-circular package, staging, preflight,
   authorization, and detached-signature contract.
2. **The durable-record and lane inventory is incomplete.** Snapshot/rollback manifests, adoption
   evidence, attempt/upload reservation, adapter operation evidence, and GC/archive transaction
   records need explicit owners, schemas, consumers, digest rules, retention, and lane routing.
3. **At-most-once mutation and crash recovery are underspecified.** The design needs one durable
   attempt reservation, one authoritative journal-head/commit primitive, complete pre-mutation and
   post-mutation failure transitions, and distinct expiry/revocation rules for initial admission,
   roll-forward, rollback, replay, and adoption.
4. **The v1-to-v2 bootstrap fence is not yet realizable.** A caller that started before cutover must
   not queue and mutate after the fence. The design likely requires a separately reviewed v1 handoff
   bridge that makes every old writer global-lock-first, nonblocking, and fence-aware before
   bootstrap can begin.
5. ~~**Ingress ownership is still split in source.**~~ **CLOSED by
   [ADR 0002](../adr/0002-menhir-production-ingress-ownership.md) (2026-09-08).** The shared
   `yawn.deploy` Caddy remains the sole `memory.ctharvey.me` ingress and Cloudflared-sole-ingress is
   withdrawn. Menhir routes in the shared Caddyfile are correct tenancy, not split authority. The
   `/ops` `strip_prefix` contract stands and the native ASGI mount change is withdrawn.

   What remains is not an architecture issue but a bounded deletion: `yawn.deploy` still holds a
   Menhir release lock (`/run/lock/menhir-production.lock`), phase journal, release authority,
   `caddy-release.sh`, `caddy-route-apply`, `caddy-route-rollback` and their tests **in source
   only** — Menhir's ansible already removes them from the host and asserts their absence. Menhir
   also still carries a non-target `deploy/docker-compose.cloudflared.yml`.
6. **Owner-key custody is not a closed protocol.** Key format/encryption, Windows ACLs, passphrase
   input, key-ID derivation, signer/approval-client identity, enrollment, offline backup, rotation,
   revocation, loss, compromise, and post-revocation transaction behavior require one normative
   contract.

### P2

1. **Descriptor-safe intake needs a post-copy source check.** Held descriptors protect copied bytes,
   but the final design must also repeat source `fstat` and directory enumeration if it claims that
   mutation during capture is detected rather than merely harmless.
2. **Canonical repository identities are unowned.** Package type must select a fixed registry of
   repository IDs, normalized origins, object formats, and attestation identities. The current OAuth
   checkout origin and its package metadata already disagree, proving caller-supplied URLs are not a
   sufficient trust anchor.

### Current implementation uncertainty

The existing deployment implementation remains unaccepted as a whole. Earlier reviews established
that it contains duplicated semantic authorities and cross-repository mutation paths. This status
update does not assert that the seven remaining architecture issues above are the complete set of
code defects, and it does not restart another full implementation review. Code findings will be evaluated only in
the bounded phase that owns the affected path.

## Working rule

Only one phase may be open. The next phase may not contain a commit until the current phase has:

- a frozen input revision and explicit file/repository scope;
- one owner and one authoritative output;
- focused positive, negative, crash, concurrency, and absence evidence applicable to that scope;
- no open P0, P1, or P2 finding in a fresh review of the complete phase;
- an exact closing commit and verification record.

If a later finding belongs to an earlier invariant, reopen the earliest owning phase and invalidate
every dependent phase. Never move the finding forward simply to preserve a completed status. Never
close a kernel/schema defect with wrapper logic, documentation, or a happy-path test.

## Serial recovery plan

### Phase 0 — Repository census and freeze

**This is the only phase currently eligible to start. It is documentation and static analysis only.**

Create one machine-readable census across Menhir, the three shared wrappers, Yawn VPS, Archolith
OAuth, and Yawn deploy. Enumerate every release input, schema/record, builder, deploy/scaffold entry,
privileged command, sudoers rule, lock, journal, receipt, recovery path, installer, worker, unit,
timer, ingress route, package store, GC/archive path, read command, and documented manual procedure.
Assign each item exactly one disposition: preserve, replace, or retire, plus one final owner.

Phase 0 does not design schemas, edit deployment code, contact a host, or fix a writer. Its sole
output is the complete inventory and a mechanical “no unclassified item” check.

Gate: the census covers all five repository boundaries, every known bypass and absent/legacy state,
and passes a fresh bounded review with no open P0-P2 issue.

### Phase 1 — Protocol and state model

Using only the accepted census, define canonical record schemas, package/subject types, repository
identity registry, digest/domain rules, legal state transitions, lock order, durable commit primitive,
replay matrix, retention, and key-custody contract. Produce generated golden fixtures. Do not build,
stage, sign, install, or mutate anything.

Gate: every censused semantic record and consumer maps to exactly one schema/owner, malformed and
cross-record fixtures fail closed, and the complete phase passes review.

### Phase 2 — Immutable compilers

Implement clean Git-object materialization and separate product, infrastructure, and control-plane
package compilers. Preserve hash-locked offline dependencies and transitive image/archive/config/
layer/SBOM/scan/registry/workflow/attestation evidence. Do not implement staging or host code.

Gate: dirty, untracked, substitution, mutable-reference, ambient-cache, and nondeterminism tests pass
from clean directories; complete phase review is clear.

### Phase 3 — Staging and read-only preflight

Implement disposable-host staging and the complete live-state observer as pure consumers of Phase 1
and 2 records. No signing, sudoers, installation, or mutation path.

Gate: tamper and clean-host tests pass; staging cannot approve/promote and preflight cannot mutate;
complete phase review is clear.

### Phase 4 — Signing and authorize-only admission

Implement owner-key custody, approval display, signed authorization, trust-store verification, and a
root authorize-only path that must terminate before snapshot or mutation.

Gate: wrong/stale/revoked/expired/mismatched authority and key-leak tests fail before mutation;
complete phase review is clear.

### Phase 5 — Transaction kernel with fake adapters

Implement descriptor-safe intake, attempt reservation, locks, durable journal/head, snapshots,
rollback arming, crash recovery, replay/adoption, GC retention, and archival against fake disposable
state only. No real app, database, security, infrastructure, bootstrap, or wrapper integration.

Gate: every durable and mutation kill point, concurrency case, replay row, post-copy intake race, and
exact rollback case passes; complete phase review is clear.

### Phase 6 — App-only lane

Connect only the app-only adapter to the accepted kernel on a disposable host. Preserve Neo4j,
Cloudflared, configuration, OAuth/MCP, backup/restore, and control-plane identities exactly. Do not
add other lanes.

Gate: full app-only success, refusal, interruption, rollback, retry, and adoption pass; only then
retire the old app-only path in the disposable installation; complete phase review is clear.

### Phase 7 — Security-config lane

Add only security configuration and required app restart behavior. Reuse every Phase 1-6 primitive
without a lane-local journal, approval, receipt, or recovery format.

Gate: declared config changes and all undeclared-state equality checks pass; complete phase review is
clear.

### Phase 8 — Maintenance lane

Add maintenance, backup, restore rehearsal, and database-preservation behavior through the same
kernel. Reuse prior protocol and transaction code unchanged unless the owning earlier phase is
formally reopened.

Gate: backup/restore and database crash/rollback matrices pass; complete phase review is clear.

### Phase 9 — Infrastructure, v1 bridge, and bootstrap

First implement and independently close the minimal v1 handoff bridge. Then, as a separate commit and
review gate, implement infrastructure convergence and signed bootstrap on disposable v1 hosts. Hold
all old/new locks through cutover or exact rollback, atomically replace sudoers, and prove every old
writer absent.

Gate: callers started before, during, and after the fence cannot mutate after it; first install,
upgrade, interruption, exact restoration, and least-privilege tests pass; complete phase review is
clear.

### Phase 10 — Cross-repository contraction

Reduced by [ADR 0002](../adr/0002-menhir-production-ingress-ownership.md). This is no longer an
ingress migration; it is deletion plus interface work.

Convert the shared PowerShell scripts to transport only, convert Yawn to versioned read-only
responses, and remove Menhir **lock, release-authority, journal, and GC** ownership from
`yawn.deploy` — `caddy-release.sh`, `caddy-route-apply`, `caddy-route-rollback`,
`/run/lock/menhir-production.lock`, the phase journal, and their tests. **Keep** the
`memory.ctharvey.me` vhost, its certificate mounts, and the `menhir-proxy` attachment. Remove the
non-target `deploy/docker-compose.cloudflared.yml` and `cloudflared*.example` from Menhir. Delete
every v1 writer, alias, unit, timer, sudoers entry, fallback, and manual mutation procedure
identified in Phase 0.

Gate: all repositories agree on exact arguments, records, return codes, paths, and privilege
boundaries; the ingress positive assertions still hold and the retired-writer negative censuses pass
in both source and a clean install; complete phase review is clear.

### Phase 11 — Integrated acceptance

Run the full non-production flow from exact clean checkouts on disposable hosts, then required CI on
the exact pushed revisions. Verify application/API behavior, Neo4j persistence, OAuth/MCP contracts,
Cloudflared allow/deny behavior, backup/restore, and unrelated Yawn behavior remain unchanged.

Gate: exact-revision CI and clean-host acceptance are green and one independent full-system review
has no open P0-P2 code defect. This makes the work eligible for a separate production decision; it
does not authorize deployment.

## Immediate next action

Do Phase 0 only. Do not revise the full architecture, implement a P1 fix, or start another
full-system review in parallel. The owner should receive the completed census and its bounded review
before deciding whether Phase 1 may begin.

## Operational gates kept separate from code defects

Even after Phase 11, production requires separate confirmation of current backups and restore
evidence, host capacity, installed trust/kernel identity, final CI revisions, signed release and
staging evidence, owner authorization, and a maintenance window. Those are operational go/no-go
inputs and must never be used to hide or waive a code defect.
