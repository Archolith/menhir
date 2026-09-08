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
is `OPEN` and is not execution authority. Its independent review returned `ARCHITECTURE NEEDS
REVISION` with six P1 and two P2 issues. **All eight are now closed in the specification text** (see
Architecture issue status below), but none of those closures has been independently reviewed. Do not
implement from the specification until it reaches `COMPLETE`.

No production deployment, host mutation, release, merge, or key enrollment is authorized by this
plan. Existing production behavior remains the operational baseline until a replacement completes
all disposable-host and CI gates and receives a separate production decision.

## What went wrong

The system does not need to be rebuilt from scratch. The current code has useful release,
attestation, staging, backup, restore, health-check, and rollback primitives.

The failure has a specific, verifiable shape: **no schema enumerated the complete set of facts a
piece of evidence must bind, so completeness was discovered one field per review cycle.** Each
individual fix is correct. The set never converges, because a reviewer can always find one more
unbound field and nothing defines when binding is finished.

Two independent ratchets in the branch history demonstrate this. Each is seven commits re-editing
the same file, each adding exactly one more identity field or predicate:

**Staging identity** (`deploy/personal_stage_vps.py`):

| Commit | Added |
|---|---|
| `4cc70e4` | pull fresh images before inspection |
| `8f7f1de` | verify every image before creating disposable state |
| `20a6587` | bind the transferred image by image **ID** |
| `a64f821` | bind **wheel-manifest SHA-256** labels |
| `038aa08` | bind the **dockerfile wheel manifest SHA-256** |
| `8f4846a` | bind the **CI image identity** record |
| `5d212d1` | bind the **proxy image digest** and archive lineage |

**Authority** (`deploy/personal_promote.ps1`, `deploy/scaffold/menhir_app_only.py`):

| Commit | Added |
|---|---|
| `2fefd79` | harden the runner invocation |
| `5b8db26` | check the runner **name**; read deployment class from release authority |
| `b63fe7a` | bind the **runner SHA-256** |
| `4787ebb` | bind the runner SHA-256 into the **transaction** for recovery |
| `b94cc33` | add an **approval record** and a privileged-lane to root-runner mapping |
| `d3b7f2f` | bind **bundle, release, and ingress container** identities |
| `8489172` | add a **staging-receipt freshness** window |

The commonly cited cause — the same rule interpreted differently in several Python, Bash, and
PowerShell paths across repositories — is real and compounds the problem, but it is not what drove
the churn. Ingress ownership, which earlier drafts of this status treated as the primary generator,
accounts for two of roughly forty-six remediation commits; where it appears in the ratchet it is one
bound field among many. It needed a decision ([ADR 0002](../adr/0002-menhir-production-ingress-ownership.md))
because no amount of code could close it, but it was not the engine.

The remedy is therefore not another full-system patch and not more careful review. It is a schema
that declares the complete binding set **before** implementation, so that "is this evidence
sufficiently bound?" has a mechanical answer instead of a reviewer's judgement.

## Architecture issue status

All eight issues from the `ARCHITECTURE NEEDS REVISION` review are now closed **in the
specification text**. Seven were closed by the 2026-09-08 architecture revision preserved in Menhir
commit `2a51408`; the eighth was closed by owner decision in
[ADR 0002](../adr/0002-menhir-production-ingress-ownership.md).

The review's issue list predates that revision. It described a version of the specification that no
longer exists, which is why this plan previously reported five P1 and two P2 as open.

| # | Issue | Status | Closing section |
|---|---|---|---|
| P1 1 | Authorization is not type-complete | Closed | Typed subjects and authorization families: five-value `subject_type` enum with one target/manifest/authorization/lane binding each, and distinct domain separators so a deployment ticket cannot authorize bootstrap |
| P1 2 | Durable-record and lane inventory incomplete | Closed | Protocol registry declared exhaustive for v2, with producer, consumers, digest binding, compatibility and retention per record; indexes declared rebuildable from anchors and journal heads |
| P1 3 | At-most-once mutation and crash recovery underspecified | Closed | O_EXCL attempt-anchor publication as sole reservation, `HEAD` as sole commit primitive, full state transition table, per-state ticket expiry and revocation rules |
| P1 4 | v1-to-v2 bootstrap fence not realizable | Closed as design | Separate v1 handoff bridge gate; bootstrap reordered so the fence is published while all locks are held, after drain and snapshot |
| P1 5 | Ingress ownership split in source | Closed | ADR 0002 — `yawn.deploy` retains the vhost; Menhir installs no ingress writer |
| P1 6 | Owner-key custody not a closed protocol | Closed | Owner key, signer, and trust-store contract — format, ACL, passphrase, key ID, enrollment, rotation overlap, revocation, loss and compromise |
| P2 1 | Intake needs a post-copy source check | Closed | Intake step 7 — re-`fstat` every held source descriptor and re-enumerate the directory for the same name-to-inode set |
| P2 2 | Canonical repository identities unowned | Closed | Canonical repository identity registry — closed five-row registry; the stale `ctharvey/archolith_oauth` metadata URL is explicitly non-authoritative |

### Two closures are churn-critical

P1 1 and P1 2 are not peers of the other six. They are the two that answer the question the ratchets
above kept failing to answer: **what is the complete set of facts this evidence must bind?** P1 1
gives each subject type one declared binding set; P1 2 gives each record a declared digest/binding
rule, producer, and consumers.

If those two binding sets are incomplete or wrong, the ratchet resumes on the first implementation
phase that touches evidence, and no downstream gate will catch it — a reviewer will simply find one
more unbound field, exactly as before. Everything else in the specification is downstream of them.

Review them first and hardest. The specific test is not "is this section well written" but "can I
name a fact that a real attacker or a real crash would need bound, that this binding set omits?"

### What closed does and does not mean

Closed here means the specification now says what the issue asked it to say. It does **not** mean the
designs have been independently checked. Seven architecture closures were written in one sitting and
have had no review since. They are the entry condition for the fresh no-context review the
acceptance ledger requires, not a substitute for it.

The verdict is therefore unchanged: **implementation and production work remain frozen.** What
changed is the reason. The blocker is no longer open architecture questions; it is that the answers
are unreviewed.

### Current implementation uncertainty

The existing deployment implementation remains unaccepted as a whole. Earlier reviews established
that it contains duplicated semantic authorities and cross-repository mutation paths. Closing the
architecture issues does not close any code defect, and this status does not restart a full
implementation review. Code findings will be evaluated only in the bounded phase that owns the
affected path.

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

These 14 phases derive from the specification's 10 serial gates. The specification permits splitting
a gate further but forbids combining adjacent gates, so the mapping is recorded here for verification:

| Spec gate | Plan phase |
|---|---|
| 1 Census and frozen contract | 0 |
| 2 Canonical protocol | 1 |
| 3 Immutable compilers | 2 |
| 4 Staging and read-only preflight | 3 |
| 5 Signing and non-mutating authorization | 4 |
| 6 Product kernel and adapters | 5, 6, 7, 8, 9 (split by lane) |
| 7 v1 handoff bridge | 10 |
| 8 Bootstrap and installation | 11 |
| 9 Cross-repository contraction | 12 |
| 10 Integrated clean-host acceptance | 13 |

No plan phase spans two spec gates. Gate 7 and gate 8 are deliberately separate phases; an earlier
version of this plan combined them, which the specification does not allow.

### Phase 0 — Repository census and freeze

**No phase is eligible to start until the specification is `COMPLETE`.** Phase 0 is first in line and
is documentation and static analysis only.

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
rollback arming, crash recovery, replay/adoption, and retention reachability against fake disposable
state only. No real app, database, security, infrastructure, bootstrap, or wrapper integration.

Note that `gc` and `archive` are signed lanes in the specification, not kernel-internal cleanup.
Only their reachability computation and retention rules belong here; their authorized execution is
Phase 9.

Gate: every durable and mutation kill point, concurrency case, replay row, post-copy intake race, and
exact rollback case passes; complete phase review is clear.

### Phase 6 — App-only lane

Connect only the app-only adapter to the accepted kernel on a disposable host. Preserve Neo4j,
ingress, configuration, OAuth/MCP, backup/restore, and control-plane identities exactly. Do not add
other lanes.

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

### Phase 9 — GC and archive lanes

Add the `gc` and `archive` lanes as fully signed subjects: kernel read-only planner, owner rehearsal
and authorization through the same subject/staging/preflight/ticket protocol, adapter execution under
the common attempt anchor, journal and snapshot, and their plan/receipt records. The adapters accept
no caller removal list, and the recomputed plan digest must equal the authorized plan under locks.

Gate: refusal to collect active, current, previous, rollback and snapshot roots; destination
verification before any source disposition; complete phase review is clear.

### Phase 10 — v1 handoff bridge

Change only the censused v1 mutators to acquire the v1 global lock before any lane lock, use
nonblocking acquisition, and check one fixed root-owned handoff fence both before and after lock
acquisition, refusing when it is present. Add no v2 mutation, trust, package, receipt, or fallback
behavior. Prove it on disposable v1 fixtures with a complete writer census.

This is a separate gate from bootstrap and may not be combined with it. If any v1 writer cannot be
bridged or enumerated, v2 bootstrap is impossible and remains blocked.

Gate: every censused v1 mutator carries the accepted bridge; blocking acquisition and unbridged
writers are detected; complete phase review is clear.

### Phase 11 — Infrastructure convergence and bootstrap

Implement infrastructure convergence and signed bootstrap on disposable v1 hosts against the accepted
bridge. Hold all old and new locks through cutover or exact rollback, publish the fence only while
every lock is held and after drain and snapshot, atomically replace sudoers, and prove every old
writer absent.

Gate: callers started before, during, and after the fence cannot mutate after it; first install,
upgrade, interruption, exact restoration, and least-privilege tests pass; complete phase review is
clear.

### Phase 12 — Cross-repository contraction

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

### Phase 13 — Integrated acceptance

Run the full non-production flow from exact clean checkouts on disposable hosts, then required CI on
the exact pushed revisions. Verify application/API behavior, Neo4j persistence, OAuth/MCP contracts,
ingress allow/deny behavior, backup/restore, and unrelated Yawn behavior remain unchanged.

Gate: exact-revision CI and clean-host acceptance are green and one independent full-system review
has no open P0-P2 code defect. This makes the work eligible for a separate production decision; it
does not authorize deployment.

## Immediate next action

**Commission one fresh no-context architecture review of the specification.** That is the only open
action. Every architecture issue is now closed in the text and none of those closures has been
independently checked, so review is what the work is waiting on — not more design and not Phase 0.

Give the reviewer the specification, ADR 0002, and this plan. Tell them explicitly:

- the seven non-ADR closures were written on 2026-09-08 and preserved in Menhir commit `2a51408`;
  they are new and unreviewed, and are the intended focus;
- **P1 1 and P1 2 carry the most weight.** They define the complete binding sets whose absence
  produced the two ratchets in "What went wrong". Review them first, and judge them by whether a
  fact that a real attacker or crash would need bound is missing from a binding set — not by
  whether the prose is sound;
- decisions recorded in the specification's closed-decisions table are out of scope. A finding that
  re-litigates one must be raised as a proposed ADR supersession, not filed as a defect;
- findings must be scoped to the specification as written. Re-deriving the original review's issue
  list against the superseded version is the specific failure this plan exists to stop.

If the review is clear, move the specification to `COMPLETE` without changing its UUID, record owner
acceptance, and only then start Phase 0. If it is not clear, edit the specification as one
architecture revision, reset the affected acceptance-ledger rows, and review the complete document
again. Do not patch this plan first.

Do not revise the architecture, implement a fix, or start a phase in parallel with the review.

## Operational gates kept separate from code defects

Even after Phase 13, production requires separate confirmation of current backups and restore
evidence, host capacity, installed trust/kernel identity, final CI revisions, signed release and
staging evidence, owner authorization, and a maintenance window. Those are operational go/no-go
inputs and must never be used to hide or waive a code defect.
