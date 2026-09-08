# Menhir deployment control-plane architecture — fresh no-context review

- **Reviewed artifact:** `.agent/reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`
  (artifact_uuid `12069633-3533-4a92-a79b-573006875905`), as it stands at worktree HEAD `fc839e3`,
  branch `fix/deployment-reliability-20260907`.
- **Reviewer context:** none prior. Specification read in full before the revision diff, the plan, or
  the ADR.
- **Date:** 2026-09-08

---

## 1. Verdict

**ARCHITECTURE NEEDS REVISION.**

Four of the seven revised closures hold (durable commit primitive, owner-key custody, descriptor-safe
intake, repository identity registry), but both closures the plan designates churn-critical do not:
the subject binding set omits the lane from every piece of evidence that is supposed to constrain it,
and the registry that declares itself exhaustive omits three durable structures on which the design
places explicit authority — the operation probe, the writer/host census, and the handoff fence. The
ratchet the specification exists to stop is therefore still open at exactly the two places the plan
predicted it would be, and I can name the missing fields and the failures they enable.

Ten P1 and seven P2 findings. No P0 — see the note at the head of section 2.

---

## 2. Findings

**On the absence of a P0.** I evaluated four candidates for P0 (maintenance-lane database rollback,
the fence crash-wedge, clock trust, and lane substitution). Each stops short: the design's failure
mode in every case is `blocked` plus audited unrestricted-root recovery, which the specification
declares an accepted outcome rather than an unhandled one, and none of them permits a silent bad
result. F-8 is the closest call and a reviewer who resolved its ambiguity the permissive way would
be justified in calling it P0. I have not done so.

---

### F-1 (P1) — The lane is absent from every binding set that is supposed to constrain it

**Category:** underspecified.
**Sections:** "Typed subjects and authorization families" (subject table, `product-release` row);
"Canonical protocol rules" registry rows `staging-receipt.v2`, `production-preflight-receipt.v2`,
`release-envelope.v2`; "Staging and production preflight".

The subject table binds `product-release` to lane "`app-only`, `security-config`, or `maintenance`
**as allowed by the envelope**". No registry row gives `release-envelope.v2` an allowed-lane field;
its declared digest binding is "complete canonical envelope and content manifest". So the phrase
names a predicate that no schema declares and no mechanism can check.

Compounding this, neither evidence record that gates the ticket binds a lane:

- `staging-receipt.v2` binds "`deployment-subject.v2`, staged bytes, adapter/verifier, host profile,
  checks and chronology" — no lane, no `lane-plan.v2` digest — while the staging section says it
  "covers clean install, restart, app-only, security-config and maintenance rehearsal behavior
  **required by the declared lane**".
- `production-preflight-receipt.v2` binds "deployment subject, kernel/read-model versions, full
  pre-state composite and observation time" — no lane, no lane-plan digest — while the preflight
  section makes the last composite component "systemd units/timers, sudoers, filesystem
  ownership/modes, locks, journals and obsolete-path absence **relevant to the lane**".

**Failure scenario.** The owner compiles subject S and rehearses it in staging under an app-only lane
plan. Staging emits a success receipt bound to S. The owner then runs preflight and issues a
`deployment-ticket.v2` binding S, that staging receipt, that preflight receipt, and a `maintenance`
lane plan. Every cross-record digest agrees, because none of the three records carries a lane. The
kernel admits. The maintenance lane's declared blast radius includes the app/database maintenance
sequence and backup/restore state; the app-only rehearsal exercised none of it, and the preflight
composite — if it is lane-scoped, as its own wording says — never observed the components the
maintenance adapter is about to change, so the under-lock equality recomputation has nothing to
compare them against. I-05 claims owner approval binds "one release, staging result, preflight,
pre-state, **lane**, attempt, kernel, adapter, issue time, and expiry"; the ticket does bind a lane,
but nothing upstream of the ticket does, so the lane is asserted by the signer rather than proven by
the evidence.

This is not only an adversary case. It is the ordinary operator-error case the threat model's
"accidental drift" clause claims to cover, and it is the exact shape of the ratchet: a fact that must
be bound, in a set that declares itself complete, that is not bound.

**Remedy.** Add `lane` and the `lane-plan.v2` digest to `staging-receipt.v2` and
`production-preflight-receipt.v2`; add a closed `allowed_lanes` set to `release-envelope.v2`; state
that the kernel refuses when ticket lane ∉ envelope allowed_lanes, or when the ticket's lane and
lane-plan digest differ from the staging and preflight receipts'.

---

### F-2 (P1) — `live-state-composite.v2` is the most-ratcheted structure in the project and is the one specified as an open list

**Category:** underspecified (and internally inconsistent).
**Sections:** "Staging and production preflight" (composite bullet list); registry row
`live-state-composite.v2`; "Fast lane and production-state equality".

The registry row says "Canonical digest of all required state components with component digests |
**Missing/extra component fails**". The preflight section introduces the component list with "The
live-state composite contains **at least**:". Those two sentences cannot both be operative. "At
least" makes the set open; "extra component fails" makes it closed. The fast-lane section adds a
third reading ("Adoption recomputes the **complete** production state"), and the preflight list's
last bullet adds a fourth ("relevant to the lane").

The plan's own diagnosis is that completeness of identity binding was discovered one field per review
cycle across two seven-commit ratchets. This composite is the direct successor of the structures those
ratchets edited. Specifying it with "at least" reinstates the failure mode verbatim: the next reviewer
adds one more component, and nothing in the document says when the set is finished.

**Named omissions, with the failure each enables:**

1. **Host/installation identity.** No component binds `/etc/machine-id`, a derived installation UUID,
   or a boot ID. Consequence: nothing in the signed chain distinguishes a disposable rehearsal host
   from production. The `gc` and `archive` lanes explicitly require the owner to "rehearse and
   authorize" a plan; a rehearsal that produces a signable ticket, on a host whose narrow composite
   matches production's, is admissible on production.
2. **The `menhir-proxy` alias→container binding.** The composite binds "container ID/config/labels/
   network attachment", but the shared Caddy resolves the upstream by the DNS alias
   `menhir-prod-app` (`yawn.deploy/Caddyfile`, verified at `4937657`). A second container joining
   `menhir-proxy` under that alias changes ingress destination while every listed component still
   matches.
3. **Clock synchronization state.** Filed separately as F-3.

**Remedy.** Delete "at least". Publish the component set as a closed, versioned enumeration owned by
the schema package (the same treatment the repository identity registry already receives), make it
lane-independent, and make adding a component an architecture revision.

---

### F-3 (P1) — No record binds any fact about the host clock, and three authority predicates are wall-clock predicates

**Category:** underspecified.
**Sections:** "Canonical protocol rules" (time representation); registry rows
`production-preflight-receipt.v2` (age ≤ 15 minutes), `deployment-ticket.v2` (30-minute lifetime);
"Owner key, signer, and trust-store contract" (24-hour rotation overlap; revocation "effective
time"); "Deployment transaction state machine" (ticket time and revocation bullets).

The protocol rules are careful about time *representation* — RFC 3339 UTC seconds, integer
millisecond monotonic durations, "never inferred from wall-clock subtraction" — and silent about time
*integrity*. Preflight freshness, ticket expiry, rotation overlap end, and revocation effective time
are all evaluated by the root kernel against `CLOCK_REALTIME`, and no registered record binds a fact
about whether that clock is trustworthy.

**Failure scenario (crash, not attacker).** The VPS reboots, or is restored from a provider snapshot,
and comes up with the hardware clock behind by hours before NTP steps it — a routine occurrence on a
single VPS. A ticket that expired, and a key whose 24-hour rotation overlap ended, both evaluate as
still valid. Authority conjunct 5 ("an unexpired owner signature") and the revocation rule's claim
that revocation "immediately prevents first admission" are both unenforced during that window, by a
condition that requires no adversary at all. The other conjuncts still hold, which is why this is P1
and not a bypass: pre-state equality and the signature itself still bind. But the expiry mechanism
the design names does not do what the design claims.

**Remedy.** Add clock-synchronization state (`timedatectl` sync status, NTP source identity, boot ID,
and a monotonic anchor recorded at attempt reservation) to the closed composite of F-2, and state that
the kernel refuses admission and refuses to arm rollback when the realtime clock is unsynchronized or
has stepped since the anchor.

---

### F-4 (P1) — The operation probe decides roll-forward versus rollback after a crash, and no registered record declares it

**Category:** underspecified.
**Sections:** "Deployment transaction state machine", ordering step 7 and the third ticket-time
bullet.

Step 7: "A crash with intent but no result invokes the operation's **schema-declared probe**; the
adapter may mark complete, run a declared idempotent retry, or roll back—never guess." Third
ticket-time bullet: "the pending operation's **registered probe** proves continuation/retry is
required and idempotent."

Neither "schema-declared" nor "registered" resolves to anything. `lane-plan.v2` binds "members,
commands by symbolic operation, pre/postconditions, rollback scope" — no probe.
`adapter-operation-evidence.v2` binds evidence produced *after* an operation, not the predicate used
when evidence is absent. The registry is declared exhaustive for v2 and contains no probe record.

This is the single most safety-critical decision in the design — the one place where the kernel may
continue mutating production without a fresh authority check — and its authority source is
undeclared.

**Failure scenario.** The app-only adapter records an intent for a container-replacement operation and
the host loses power mid-`docker` call. On recovery the kernel holds an intent with no result. With no
registered probe it has no authority to conclude anything, so the honest reading of "Any nonterminal |
Journal/package/snapshot uncertainty | `blocked`" applies: every mid-apply crash terminates in
`blocked` and requires unrestricted-root recovery. For a single-operator personal service, that makes
the most likely real-world failure the one the design cannot recover from — while the text implies it
can. The alternative reading, that implementers will invent probes per operation, reproduces exactly
the "same rule implemented differently in several paths" failure the plan names as compounding cause.

**Remedy.** Add an `operation-probe.v2` registry row (or a mandatory `probe` sub-record inside
`lane-plan.v2`) with producer, consumers, digest binding, and the declared outcome vocabulary
(complete / idempotent-retry / roll-back), and require the probe digest to be bound by the anchor so
recovery uses the probe stored with the attempt rather than the installed-latest one.

---

### F-5 (P1) — The writer/bypass/host census is the named enforcement point for three invariants and two bootstrap steps, and has no registry row

**Category:** underspecified.
**Sections:** invariants I-12, I-13, I-17 (enforcement column); "System boundary and final ownership"
(`yawn.deploy` census, positive and negative assertions); "Bootstrap and v1-to-v2 handoff" steps 2
and 5; phase gate 1.

I-17's lowest authoritative enforcement point is "Machine-readable source/install/host census".
I-13's is "Source/host census plus read-only ingress verification". I-12's is "Atomic sudoers
replacement and installed-artifact census". Bootstrap step 2 verifies the bridge "installed on every
**censused** mutator"; step 5 enumerates callers "by executable identity and cgroup".

The protocol registry declares itself exhaustive and contains no census record. The nearest rows do
not cover it: `obsolete-artifact-absence.v2` binds only expected *absences*, and
`installed-artifact-manifest.v2` is produced *by* bootstrap and so does not exist on first install,
which is precisely when step 2 must run. The specification simultaneously states that a non-record
durable object "cannot contain authority unavailable from registered records" — the census contains
exactly such authority.

**Failure scenario.** On first install, bootstrap step 2 must verify the bridge on every censused
mutator. The only enumeration available to it is one carried inside the control-plane package it is
installing. A package whose census omits a v1 writer — through error, or because a writer was added
after the census was frozen — passes step 2, passes step 5 (the omitted writer is not enumerated, so
it is not a "possible future lock acquirer"), and the fence is published with a live unbridged v1
mutator on the host. I-16 ("v1 cannot start, queue, or resume after bootstrap claims the handoff
fence") is then false, and the specification's own words — "If any v1 writer cannot be bridged or
enumerated, v2 bootstrap is impossible" — are unenforceable because nothing establishes what the
complete writer set is.

**This is not hypothetical.** See F-6.

**Remedy.** Add a `writer-census.v2` record: producer, digest rule, consumers (bootstrap verifier,
infrastructure adapter, CI phase gate), retention, and an explicit rule that the census is signed
evidence independent of the package being installed — a package cannot certify its own completeness.

---

### F-6 (P1) — A fourth privileged Menhir PowerShell wrapper exists that the boundary table, the interface section, and the census scope all omit

**Category:** the design is wrong about the system it is contracting (incomplete boundary).
**Sections:** "System boundary and final ownership" ("Shared `…\scripts` | **Three** transport-only
PowerShell interfaces"); "Shared PowerShell interfaces" ("The final interfaces are exact and
versioned", enumerating two survivors plus one removal).

Ground truth, `C:\Users\thron\IdeaProjects\scripts` (origin `github:ctharvey/workspace-meta`, the
registry's `shared_workspace` row): the directory contains **four** Menhir wrappers —
`deploy-menhir.ps1`, `deploy-menhir-app-only.ps1`, `menhir-scaffold.ps1`, and
**`menhir-backup-archive.ps1`**. The fourth appears nowhere in the specification, and the plan's
anchor table likewise scopes "only the three Menhir PowerShell wrappers".

`menhir-backup-archive.ps1` (209 lines) is not transport-only. It:

- base64-encodes a Python program locally and pipes it over SSH into
  `sudo -n python3 -` (line 71) — caller-authored code executed as root;
- performs `sudo -n install -o root -g root -m 0400`, `sudo -n mv -f`, and `sudo -n rm -f` against
  root-owned paths (lines 145, 151, 172).

**Failure scenario.** The specification's post-cutover claim (I-12, "Privileged command boundary") is
that exactly one privileged mutating executable pattern remains, plus five fixed read commands. That
claim is made against an enumeration that is missing a writer which, if its sudo rules exist, is a
general-purpose root code-execution path. The contraction gate's negative census (gate 9) is derived
from the same enumeration, so it would not assert this wrapper's absence, and phase 13 would close
green with the writer still installed. The gate designed to catch this — the machine-readable census
of F-5 — is the one with no schema.

**Not verified, deliberately.** I did not contact the host, so whether the production sudoers actually
grants `python3 -` is unknown to me. The `deploy/scaffold/menhir-scaffold.sudoers` in this worktree
does not; it grants `/usr/bin/python3 /srv/menhir/scaffold/bin/menhir_stage_vps.py *` — itself an
argument wildcard of the kind the specification's privileged-boundary section forbids. Either the rule
exists elsewhere on the host, or this wrapper is already broken; both are census items.

**Remedy.** Correct the boundary table's cardinality, give `menhir-backup-archive.ps1` an explicit
disposition (preserve / replace / retire) and final owner, and treat its discovery as evidence for
F-5 rather than as a one-off correction.

---

### F-7 (P1) — There is no privileged entry point for production preflight, the GC planner, or the archive planner

**Category:** underspecified; blocks realizability of three flows.
**Sections:** "Privileged command boundary"; "Staging and production preflight"; "Retention, garbage
collection, and archival"; acceptance-ledger row "Privileged/read-only boundaries closed".

The boundary enumerates one mutating command (`menhir-txn submit <upload-id>`) and five read commands
(`menhir-status`, `menhir-release-inspect`, `menhir-logs`, `menhir-backup-status`,
`menhir-generation-inspect`), each of which "dispatches a named kernel read-model function and returns
exactly one corresponding v2 JSON response" — the five `*-response.v2` rows.

Three records the owner must obtain from root *before* signing are produced by none of them:

- `production-preflight-receipt.v2` — "a separate explicit owner-invoked, read-only operation" that
  acquires the global and lane locks, and reads sudoers, filesystem ownership and modes, Docker and
  systemd state. All root-only. No command invokes it.
- `gc-plan.v2` — "A read-only kernel planner writes `gc-plan.v2` from current anchors; the owner
  rehearses and authorizes that plan". No command emits it.
- `archive-plan.v2` — same structure, same gap.

**Failure scenario.** The owner cannot produce a ticket. Every `deployment-ticket.v2` requires a
preflight receipt no older than 15 minutes; the owner has no authorized way to make one. The design as
written cannot execute a single deployment. This is a completeness hole, not a subtle one, and the
ledger row asserting "Fresh reviewer verifies one mutator plus five exact read operations" is marked
READY FOR REVIEW and is not ready.

Note that the fix is not free: adding a sixth, seventh and eighth read command means the boundary must
also say who may invoke them, which F-16 (P2) raises separately for the Yawn gateway.

---

### F-8 (P1) — The maintenance lane's authority over Neo4j data is self-contradictory, and no snapshot prerequisite makes its rollback achievable

**Category:** wrong (contradiction) with an underspecified remedy.
**Sections:** lane table row `maintenance`; "Non-effects and explicit exclusions"; registry row
`snapshot-manifest.v2`; state machine rows `revalidated → snapshotted` and `rolling-back → blocked`.

The lane table says `maintenance` **may change** "Declared app/database maintenance sequence, backup
and restore rehearsal state". The non-effects section says the architecture does not authorize "an
application feature, API, graph schema, **database migration or data rewrite**". A "database
maintenance sequence" that performs no migration and no data rewrite is not defined anywhere, and the
two statements cannot be reconciled from the text.

Resolve it permissively and the design has no rollback for it. `snapshot-manifest.v2` binds "exact
prior presence/absence, bytes/metadata, units, containers, routes and lane state plus restoration
prerequisites"; the preflight composite binds Neo4j "container/image/volume/database identity,
migration/schema marker and health". Identity and markers, never a restorable data copy. Nothing in
the `revalidated → snapshotted` transition requires a verified fresh restorable backup before a
data-mutating lane may arm rollback.

**Failure scenario.** Maintenance lane runs a declared database sequence that writes to the graph.
Postcondition verification fails. `rollback-pending → rolling-back`. Rollback must prove exact prior
state; the snapshot records the volume's identity, which is unchanged, and the container, which is
unchanged, and has no way to restore the graph's prior contents. "Exact restoration cannot be proven"
→ `blocked`. Production now holds a partially applied database change, the graph is the one piece of
state in this system that cannot be rebuilt from Git, product submit is refused, and recovery is
break-glass. The design reached this through a fully compliant, fully authorized flow.

Resolve it restrictively — maintenance never mutates the production graph — and the contradiction is
still a defect, because the lane's declared change set says otherwise and the adapter contract has no
mechanism to enforce the narrower reading.

**Remedy.** State explicitly whether any lane may mutate the production graph. If yes, add a
restorable-state prerequisite class to `snapshot-manifest.v2` and require the `revalidated →
snapshotted` transition for such a lane to include a verified, read-back, restorable database backup;
if that is not achievable under a held lock, say so and give the lane a different authorization family
that does not claim exact rollback. If no, remove "database" from the maintenance lane's change set
and place the constraint in the adapter precondition.

---

### F-9 (P1) — The bootstrap fence carries authority no registered record carries, and "reject an old fence" creates a crash wedge

**Category:** underspecified.
**Sections:** "Bootstrap and v1-to-v2 handoff" steps 2 and 7 and the rollback paragraph; I-16;
"Canonical protocol rules" closing paragraph ("A directory layout, lock file, temporary file, derived
lookup index, or atomic pointer is not a semantic record and cannot contain authority unavailable from
registered records").

The fence is the mechanism I-16 rests on. Bridged v1 writers check *the fence file*, not the bootstrap
journal, so its authority is not available from any registered record. Step 2 requires bootstrap to
"Reject … an **old** fence", which means the fence must carry a distinguishable epoch — content, not
just presence — and therefore needs a schema, a producer, a digest rule, and a consumer. It has none.
`bootstrap-journal.v2` binds "hash-chained handoff states, lock/process census, snapshots and
activation identity", which is the record *about* the fence, not the fence.

**Failure scenario.** Bootstrap publishes the fence under all locks (step 7), fails at step 9, and
rolls back. The rollback paragraph correctly requires exact v1 restoration *before* the fence is
removed. The machine loses power after v1 is exactly restored and before `rollback remove/fsync the
fence`. On reboot: locks are gone, v1 is intact and correct, and the fence is present with the
previous attempt's epoch. Every bridged v1 writer refuses (fence present). A second bootstrap attempt
runs step 2, sees an old fence, and refuses. The host has no working v1 and no v2, from a crash at the
last step of a *successful* restoration. The specification's intended fence-remains state is reserved
for "if exact restoration cannot be proven"; this reaches the same wedge after restoration succeeded.

**Remedy.** Give the fence a registry row with an epoch bound to the bootstrap attempt anchor, and add
a declared bootstrap recovery transition that reads its own journal, confirms the recorded exact
restoration, and is authorized to clear its own fence.

---

### F-10 (P1) — Bootstrap ordering is circular against the phase order: a live `yawn.deploy` writer holds the Menhir global lock and is only touched two gates too late

**Category:** unrealizable as ordered.
**Sections:** "Bootstrap and v1-to-v2 handoff" (bridge paragraph, step 2); "System boundary and final
ownership" (`yawn.deploy` negative assertions); phase gates 7, 8 and 9; plan phases 10, 11 and 12.

Ground truth, verified in `yawn.deploy@4937657`:

- `caddy-release.sh:81` — `CADDY_RELEASE_LOCK="${CADDY_RELEASE_LOCK:-/run/lock/menhir-production.lock}"`
- `caddy-release.sh:166` — `flock -n 9 || die …` (nonblocking, which is the one thing in its favour)
- `releases.json:194` — `"release_lock": "/run/lock/menhir-production.lock"`

That script is a deployed v1 mutating entry that acquires the Menhir production mutation lock and
checks no fence. The bridge paragraph requires that "**Every** deployed v1 mutating entry must first be
converted", and step 2 requires bootstrap to reject "an unbridged writer".

**Failure scenario.** Gate 7 (plan phase 10) installs the bridge in "the censused v1 mutators". Gate 8
(phase 11) runs bootstrap, which must verify the bridge on every censused mutator including
`caddy-release.sh`. Gate 9 (phase 12) is where `yawn.deploy` is modified and this script retired. Two
outcomes, both wrong:

- Gate 7 is read narrowly as Menhir-only. Then gate 8's step 2 finds an unbridged writer and bootstrap
  refuses. The specification's own conclusion applies: "v2 bootstrap is impossible and remains
  blocked" — and the fix lives in a gate that may not begin until gate 8 closes. Deadlock.
- Gate 7 is read broadly and modifies `yawn.deploy`. Then gate 7 performs a cross-repository change
  before the gate whose entire purpose is "one coordinated compatibility break", violating "the phase
  begins from its recorded exact commits and **changes only owned surfaces**".

The specification never says which repositories gate 7 may touch. `yawn.deploy` is explicitly declared
"never a Menhir package input" while simultaneously being required to "participate in the writer/bypass
census" — and this is the case where census participation implies a code change in another
repository's release lane before the coordinated break.

**Remedy.** Either (a) split the `yawn.deploy` lock rename out of gate 9 into a gate-7 predecessor
(namespacing `caddy-release.sh` to a Yawn-owned lock removes it from the Menhir writer set entirely
and is the cleaner fix), or (b) state that gate 7 owns all repositories that contain Menhir-lock
acquirers and enumerate them. Option (a) also deletes most of the bridge's justification — see
section 4.

---

### F-11 (P1) — `gc` and `archive` forbid a package manifest while three dependent records require a package

**Category:** wrong (internal contradiction).
**Sections:** "Typed subjects and authorization families" (subject table, `gc` and `archive` rows);
registry rows `attempt-anchor.v2` and `deployment-ticket.v2`; invariant I-09.

Subject table: package manifest for `gc` and `archive` is "**Forbidden**; referenced objects are
already root-owned". But:

- `attempt-anchor.v2` "binds first upload, ticket/signature, subject, **package**, adapter and kernel";
- `deployment-ticket.v2` binds "…attempt, **package**, adapter, kernel, signer, key…";
- I-09: "Recovery uses the **stored package**, adapter, and compatible kernel… | Refuse
  incompatible/**missing stored bytes**".

The protocol rules also forbid "missing fields" and "unexpected nulls", so an optional package field is
not available as a reading.

**Failure scenario.** An `archive` transaction reaches `rollback-armed`, copies part of its object set
to the destination, and the process is killed. Recovery dispatches per I-09, requires the stored
package, finds none because the subject type forbade one, and must "Refuse incompatible/missing stored
bytes" → `blocked`. Every crashed `gc` or `archive` attempt is unrecoverable by construction, and
`archive` is the lane that may delete local evidence after destination verification, so the blocked
state can hold a half-moved evidence set.

**Remedy.** Either declare a minimal package for `gc`/`archive` (the stored plan plus adapter identity
satisfies I-09), or make the package binding in the anchor, ticket and I-09 explicitly
subject-type-conditional with the conditional stated in the subject table.

---

### F-12 (P2) — `HEAD` is declared the sole current-state authority, but `reserved` has no journal record

**Category:** underspecified.
**Sections:** "Deployment transaction state machine", paragraphs 2 and 3 and transition row 1.

"The validated record named by `HEAD` is the sole current-state authority." Transition row 1: "No
published anchor | Valid capture and unused attempt under global lock | `reserved` | None; publish
complete attempt anchor atomically" — mutation rule "None" means no journal record is written. So a
transaction in `reserved` has an anchor and no `HEAD`, and its state is carried by the anchor, which
the preceding sentence says is not the current-state authority.

**Failure scenario.** Crash immediately after anchor publication. Recovery reads `HEAD`, finds none,
and has no declared rule mapping (anchor present, HEAD absent) → `reserved`. An implementer may
reasonably read this as a partial publication and treat it as garbage, which contradicts "A crash after
publication has one complete anchor" and would allow the attempt directory to be reclaimed and the
attempt ID reused. Small, but it sits directly on the at-most-once invariant (I-15).

**Remedy.** State that the validated anchor with no `HEAD` is exactly `reserved`, and that `HEAD` is
the sole authority for states *after* reservation.

---

### F-13 (P2) — The ingress read-only expectation binds a whole configuration owned by an independent release cadence

**Category:** underspecified; operability risk.
**Sections:** "Staging and production preflight" (shared Caddy composite bullet); "Ingress, operations
gateway, and `yawn.deploy` contraction"; adoption paragraph.

The composite binds "shared Caddy container/image/config/certificate-mount/Compose labels/network
identity and health". `yawn.deploy` serves four vhosts (`agent.yawn.rip`, `ctharvey.me`,
`archolith.dev`, `memory.ctharvey.me`) and releases on its own schedule.

**Failure scenario.** The owner takes a Menhir preflight (15-minute freshness window) and signs a
ticket. A routine `yawn.deploy` deploy touching an unrelated vhost changes the Caddyfile digest and the
Caddy container ID. Menhir admission recomputes the composite under lock, finds the ingress components
unequal, and refuses — correctly by the stated rule, and for a reason that has nothing to do with
Menhir. Repeated, this creates exactly the pressure that produces a weakened check.

**Remedy.** Bind the *effective route table for the `memory.ctharvey.me` vhost* plus the certificate
and origin-pull identity and the `menhir-proxy` alias binding — not the whole Caddy config digest or
container ID.

---

### F-14 (P2) — Revocation is claimed immediate but requires a control-plane build, staging run, and root ceremony

**Category:** the design is wrong about its own latency claim.
**Section:** "Owner key, signer, and trust-store contract" (revocation and compromise paragraphs).

"[Revocation] **immediately** prevents first admission and any pre-mutation roll-forward under that
key." But revocation is recorded in `trust-store.v2`, whose producer is "Bootstrap transaction", and
bootstrap requires `control-plane-package.v2` with a **successful staging receipt** and a production
preflight. So the fastest revocation is a clean offline control-plane build plus a full disposable-host
staging run plus an unrestricted-root ceremony — hours, not immediately.

**Failure scenario.** Suspected key compromise at 02:00. The stated response is "freezes submission,
revokes the key through bootstrap". The freeze has no mechanism (no command disables submission), and
the revocation is gated behind a build. The window is bounded in practice by the attacker also needing
SSH and sudo, which is why this is P2 rather than higher.

**Remedy.** Either add a minimal root-local revocation path that writes a revocation record without a
full control-plane package, or delete the word "immediately" and state the true latency and the interim
containment step.

---

### F-15 (P2) — `intake-receipt.v2` binds no invoking caller identity, and the shared inbox has no per-caller isolation

**Category:** underspecified.
**Sections:** "Root inbox capture" (fixed caller-writable inbox parent); registry row
`intake-receipt.v2`; threat model ("a caller who has SSH plus only the exact Menhir sudo permissions").

The receipt binds "upload ID, source descriptor facts, root-copy member manifest, capture chronology".
Not the invoking UID/GID or the sudo invocation identity. The inbox is described as one "fixed
caller-writable inbox parent"; the current group grant is `%menhir-operators` (a group, not a user).

No bypass follows — authority comes from the signature, and a swapped member fails digest
verification. The gaps are attribution (no record answers "who submitted this") and the asymmetry that
the owner key gets a two-principal ACL specified to the Windows inheritance flag while the root inbox
gets one sentence.

**Remedy.** Bind caller UID/GID and the sudo invocation identity into `intake-receipt.v2`; specify
per-caller inbox subdirectories with declared ownership and modes in the installed manifest.

---

### F-16 (P2) — Who may invoke the five read commands is undeclared, and the Yawn gateway needs them

**Category:** underspecified.
**Sections:** "Privileged command boundary"; "Read-only Yawn contract"; boundary table row "Host
inspection gateway".

The boundary describes only "the **product** sudoers file". The five read commands must be reachable by
two distinct identities — the operator, and the `yawn.vps` operations gateway service bound to
`172.30.0.1:8000` that presents the five `*-response.v2` schemas. Nothing states the gateway's
authorization, and the five commands read sudoers, filesystem ownership and modes, and systemd state,
so they need root.

**Failure scenario.** Implementation either grants the gateway service account the read commands
(unstated privilege expansion, and the gateway is the component reachable from the public Caddy) or
runs the gateway as root (also unstated). Either is a boundary decision made below the architecture.

---

### F-17 (P2) — Route table row 3 freezes an ambiguity

**Category:** underspecified.
**Section:** "Ingress, operations gateway, and `yawn.deploy` contraction", route table row 3.

"suffixes remain routed for current compatibility but the app **may** return 404". In a table declared
frozen and verified read-only on every preflight, "may" is not a checkable expectation. Either
`/mcp-http/*` suffixes are part of the contract or they are not.

---

### F-18 (P2) — No stated admission rule refuses new attempts while a `blocked` transaction exists

**Category:** underspecified.
**Section:** state machine row `rolling-back → blocked` ("Locks release only after durable blocked
record; new mutation attempts remain refused").

The mechanism is not named. It is reachable indirectly — preflight "rejects an existing
unknown/nonterminal bootstrap or product state" and admission requires a valid preflight — but the
kernel's own admission path is never told to check for blocked transactions. Since `blocked` releases
its locks, the lock hierarchy does not enforce it either.

**Remedy.** State it as an admission precondition in the kernel, not only in preflight.

---

## 3. Assessment of the seven revised closures

Reviewed against `git show 2a51408` and the text at `fc839e3`. Four close; three do not. Both
churn-critical closures are among the three that do not.

### C-1 — P1 1, "Authorization is not type-complete" (churn-critical) — **DOES NOT CLOSE**

**What it achieves.** Real progress. A five-value `subject_type` discriminated union with exactly one
target digest per type is a correct structure. The two distinct domain separators
(`MENHIR-DEPLOYMENT-TICKET-V2\0` / `MENHIR-BOOTSTRAP-AUTHORIZATION-V2\0`) genuinely make cross-family
authorization impossible, and routing `control-plane` to `bootstrap-authorization.v2` *only* is
correct. The transport-manifest split — an unsigned manifest that bounds descriptor capture, with every
captured digest then required to be transitively bound by signed records — is a sound answer to a
question the previous draft did not ask.

**Why it does not close, by the stated test.** The test is "can I name a fact that a real attacker or
crash would need bound, that this binding set omits?" Three:

1. The **lane** is bound by the ticket alone, never by the staging or preflight evidence the ticket
   cites (F-1). Rehearsal evidence and pre-state evidence can be reused across lanes with different
   blast radii.
2. `release-envelope.v2` has **no allowed-lane field**, so "as allowed by the envelope" — the only
   constraint the table places on the three product lanes — is unenforceable (F-1).
3. The **package binding is contradictory** for `gc` and `archive`: forbidden by the subject table,
   required by the anchor, the ticket and I-09 (F-11).

The acceptance-ledger row claims a reviewer will confirm "every subject type has exactly one
target/manifest/authorization/lane binding". Two of the five rows fail the manifest half and all three
product lanes fail the lane half. The ratchet is open here.

### C-2 — P1 2, "Durable-record and lane inventory incomplete" (churn-critical) — **DOES NOT CLOSE**

**What it achieves.** The registry is a genuine artifact: 30 rows, each with producer, authorized
consumers, digest/binding, and compatibility/retention; a generated implementation registry that CI
diffs against actual schemas and dispatch tables; and — the strongest single sentence in the document —
"Attempt lookup indexes are rebuildable only from `attempt-anchor.v2`; transaction current-state
pointers are rebuildable only from the validated hash chain in `transaction-journal.v2`." That
eliminates a whole class of derived-state authority. Promoting `gc` and `archive` from kernel-internal
cleanup to signed lanes is right and removes an unsigned mutation path.

**Why it does not close.** The registry declares itself exhaustive — "The protocol registry below is
exhaustive for v2… The records above are also exhaustive for durable semantic structures" — and three
structures on which the specification places explicit authority are absent:

1. The **operation probe** (F-4). Named twice as "schema-declared" and "registered"; declared nowhere.
   It is the authority for the roll-forward-after-crash decision.
2. The **writer/bypass/host census** (F-5). The named lowest authoritative enforcement point for I-12,
   I-13 and I-17, and the input to bootstrap steps 2 and 5.
3. The **handoff fence** (F-9). The mechanism I-16 rests on, checked by v1 writers directly, and
   required by step 2 to carry a distinguishable epoch.

And the row that matters most for the documented failure mode, `live-state-composite.v2`, is the one
specified with an open list — "contains **at least**" — while its registry row says missing or extra
components fail (F-2). The plan is explicit that this closure exists to make "is this evidence
sufficiently bound?" mechanically answerable. For the composite, it still is not.

F-6 is the empirical confirmation: a fourth privileged wrapper piping code into `sudo -n python3 -`
sits outside every enumeration in the document, and the artifact designed to catch it is the census
that has no schema.

### C-3 — P1 3, "At-most-once mutation and crash recovery underspecified" — **CLOSES ITS OWN CLAIM**

The strongest of the seven. Replacing a mutable attempt index with a single no-replace atomic rename
into `transactions/<attempt-id>`, where the anchor is written and fsynced *inside* the prepared
directory before publication, is correct: the namespace publication is the reservation, there is no
partial-anchor window, and "If the destination already exists, only its anchor may decide replay"
closes the substitution case. The commit ordering — terminal receipt, then the journal record binding
its digest, then `HEAD` — with the explicit statement that "no three-file atomicity is assumed" is
correct and unusually well-reasoned for a design document.

The per-state expiry and revocation rules get the hard case right: after `rollback-armed`, rollback is
always permitted from stored root authority, roll-forward requires a non-revoked key *and* a valid
ticket at the arming transition *and* a probe, and revocation forces rollback and never grants
roll-forward. That is the correct safety asymmetry.

Two residuals, neither fatal to the closure: F-12 (`reserved` versus the "`HEAD` is sole authority"
sentence), and the fact that the roll-forward branch depends entirely on the probe that F-4 shows is
undeclared. The commit primitive closes; the recovery decision it hands off to does not exist yet.

### C-4 — P1 4, "v1-to-v2 bootstrap fence not realizable" — **DESIGN CLOSES; ORDERING DOES NOT**

The reordering is right, and it fixes a real deadlock. The old sequence published the fence first and
then tried to drain callers who could no longer complete. The new sequence — bridge verified → drain
to terminal **before any fence exists**, with bootstrap taking no lock and writing nothing during the
drain → acquire the bridged v1 global lock first → census by executable identity and cgroup → snapshot
→ *then* publish the fence under all locks — is coherent. Making the bridge nonblocking-only is the key
move: it converts "is anyone queued?" from an unanswerable question into "any waiter at all is a hard
refusal". The three caller timings in step 11 are correctly enumerated and each has a sound argument.
Moving fence removal to after proven exact restoration in the rollback paragraph is also correct.

It does not close on two counts. **F-10:** `caddy-release.sh` in `yawn.deploy` is a live acquirer of
`/run/lock/menhir-production.lock` that must carry the bridge at gate 7 but lives in a repository the
plan does not touch until gate 9 — a circular dependency between gates the plan says may not be
combined. **F-9:** the fence itself is an unregistered authority-bearing object, and step 2's
"reject an old fence" produces a wedge after a crash at the last step of a successful rollback.

### C-5 — P1 6, "Owner-key custody not a closed protocol" — **CLOSES**

This reads as one normative contract rather than an assembly of good intentions. Specific and
checkable: one dedicated Ed25519 key separate from SSH/Git/TLS/OAuth/CI; encrypted PKCS#8 at a named
path with legacy, raw, OpenSSH and repository-resident forms refused; key ID as lowercase SHA-256 over
the DER SubjectPublicKeyInfo (unambiguous); Windows inheritance disabled with exactly two principals
and an explicit rejection list; a re-check of path, link type, owner, ACL, encryption and key-ID
agreement immediately before every signature; interactive-console-only passphrase entry with every
alternative channel named and refused; one signing operation per process lifetime with crash dumps
disabled, and an honest acknowledgement that managed-runtime zeroization is not absolute rather than a
claim that it is.

Enrollment, rotation, revocation, loss and compromise are all present and mutually consistent, and the
binding of `approval_client_sha256` / `signer_sha256` / `control_plane_source_commit` into the ticket,
verified against the root's allowlisted generation, closes the confused-deputy path where a modified
approval client displays one thing and signs another.

Residuals are F-14 (the "immediately" overclaim on revocation) and F-3 (every time-bounded rule here
rests on an unbound clock). Neither is a hole in the custody contract itself.

### C-6 — P2 1, "Intake needs a post-copy source check" — **CLOSES**

The best-specified section in the document. Step 7 does exactly what the issue asked: re-`fstat` every
still-open source descriptor for unchanged device/inode/mount, type, owner/group/mode, link count, size
and change/modify versions, *and* re-enumerate the held directory descriptor for the identical bounded
name-to-inode set and directory metadata.

The revision also solved a problem the original issue did not raise. Step 3 previously enumerated
against "a bounded exact member allowlist" without saying where that allowlist came from before
parsing. It now requires one fixed-name transport manifest, copies and parses that member first, and
requires its name set to equal the enumeration — with semantic subject-specific allowlists deferred
until after the full root copy. That is the correct ordering, and pairing it with "The transport
manifest is not signed authority… root uses it to bound descriptor capture, then requires every
captured digest to be transitively bound by the signed semantic records" keeps an unsigned artifact
from acquiring meaning.

The rename analysis is also correct and is the kind of distinction usually missed: renaming the upload
directory entry while the held directory object is unchanged is harmless given the second census;
replacing the entry so the held object differs must refuse. Step 9's "never consult the caller inbox
again for execution, resume, adoption, rollback, or evidence" removes the remaining TOCTOU surface.

F-15 (no caller identity in the receipt, no per-caller inbox isolation) is a gap beside this section,
not in the race-safety property it closes.

### C-7 — P2 2, "Canonical repository identities unowned" — **CLOSES**

Five closed rows; caller supplies a local object source and commit rather than a trusted URL;
normalization of HTTPS and SSH spellings to a case-sensitive `github:<owner>/<repository>` identity
before comparison; the package type selects the row, so a control-plane package cannot be built from
`yawn_vps`; redirects, forks, mirrors, alternates serving objects from an unregistered remote,
replace/graft refs, and mixed object formats all refused; object format pinned to 40-character
lowercase SHA-1 with an explicit migration required to change it; and the stale
`ctharvey/archolith_oauth` package-metadata URL named as non-authoritative with the compiler required
to reject or reconcile it. Local checkout paths are explicitly excluded from authority while remaining
usable as object databases — the right separation.

Verified: `shared_workspace` resolves to `github:ctharvey/workspace-meta`, matching the registry row.
I did not verify the other four remotes.

---

## 4. Threat model proportionality

**Short answer: the ceremony is disproportionate to the control plane, proportionate to the data
plane, and the specification applies it uniformly to both.** The public endpoint justifies rigor about
*what runs and what is reachable*. It does not justify this much rigor about *who may deploy*, where
the population is one person with one workstation and one VPS.

**Who is actually the adversary here.** The named control-plane adversary is "a caller who has SSH plus
only the exact Menhir sudo permissions". With one operator, that adversary is the operator, or someone
holding the operator's SSH key — and in the latter case they usually hold the workstation too, and
therefore the signing key, which the model already excludes. The residual adversary the PKI defends
against is narrow: an attacker with SSH-and-sudo but not the signing key. Real, but narrow.

**Defends against conditions that cannot exist here:**

- **Concurrent privileged writers.** The full lock hierarchy — global plus per-lane plus bootstrap plus
  all-lane, a documented fixed acquisition order, nonblocking bounded acquisition, process censuses by
  executable identity and cgroup, "any queued process or possible future lock acquirer is a hard
  refusal" — defends against a concurrency source with exactly one human origin. The part worth keeping
  is the O_EXCL attempt anchor, which defends against *crash-then-retry by the same operator*, which is
  real and frequent. One global mutation lock plus the anchor covers what can actually happen. The
  cgroup census and the queued-waiter analysis exist almost entirely to make the v1 bridge work — see
  below.
- **Rotation overlap windows and revocation generations.** A 24-hour overlap during which the old key
  validates only tickets issued before rotation activation and expiring before overlap end is fleet
  machinery. With one key, one signer and one verifier, "rotate = enroll the new key through the root
  ceremony, the old key is revoked at that instant" is strictly simpler and strictly safer, and it
  removes the overlap-window clock dependency in F-3.
- **The v1→v2 bridge as its own reviewed gate.** This is the largest removable item. Its entire purpose
  is to make cutover safe *without* a maintenance window. The alternative for a single-tenant personal
  service is: announce a window, stop the v1 entry points, take a verified backup, install v2, verify,
  resume. That removes gate 7 and plan phase 10 outright, removes the fence and its unregistered-record
  and crash-wedge problems (F-9), removes the circular gate ordering (F-10), and removes the cgroup
  census and queued-waiter machinery that exist to serve it. The cost is minutes of downtime on a
  personal memory server. The specification spends two of ten gates and a substantial share of its
  hardest reasoning avoiding that.
- **Transitive supply-chain attestation depth.** SBOM digest, scanner *database* identity, OIDC issuer
  and subject, base-image digest, build-tool identities, and two clean builds agreeing on all
  deterministic bytes is SLSA-L3-shaped. The concrete risk — a malicious dependency reaching the memory
  server — is largely covered by the hash-locked wheelhouse, the network-disabled production build, and
  the pinned registry digest, all of which the specification also has. The remainder is worth
  *recording* and not worth *enforcing as an admission gate*, because each enforced field is another
  place the ratchet can add one more.
- **`archive` as a fully signed lane** with staging rehearsal, preflight, and ticket. Moving evidence
  files to a durable destination is not a production-mutating operation. `gc` is the closer call and I
  would keep it signed, because it deletes.

**Warranted, and I would not cut:**

- One privileged mutating entry point with an exact argument grammar. The current v1 sudoers grants
  `/usr/bin/python3 …/menhir_stage_vps.py *` — an argument wildcard on a Python script — and F-6 found
  a wrapper piping code into `sudo -n python3 -`. This is the concrete hole, and closing it is worth
  the whole exercise.
- Descriptor-safe intake. The inbox is caller-writable and the operator group may hold more than one
  principal. C-6 is correct and cheap.
- The durable journal, snapshot, and exact-rollback machinery. Personal, and irreplaceable — a broken
  deploy on a memory graph with no second copy is the actual worst case.
- One canonical schema library that Python, PowerShell, docs and tests all consume. This is the direct
  fix for the plan's named compounding cause and is the highest value per unit of work in the document.
- The public-boundary bindings: the ingress route table, OAuth/MCP policy digests, gateway peer
  identity, and origin-pull certificate identity. The endpoint is public and these deserve exactly the
  rigor given.

**The convergence risk is the real risk.** The document's own history is two seven-commit ratchets and
roughly forty-six remediation commits produced by a *smaller* design. The proposed replacement is ten
serial gates expanded to fourteen plan phases, each requiring a fresh no-context review of the complete
phase with zero P0–P2 findings, and an explicit rule that any later finding reopens the earliest owning
phase and invalidates every dependent one. That rule is correct in principle and, at fourteen phases,
is a mechanism for never finishing. This review adding ten P1s to a document already revised once is
itself a data point.

**Concrete proposal.** Keep gates 1–6 roughly as scoped (census, protocol, compilers, staging and
preflight, signing, kernel and lanes). Replace gates 7–8 with a single windowed cutover. Fold the
`yawn.deploy` lock renaming forward as a small prerequisite. Drop rotation overlap, per-lane locks, and
the enforced tail of the attestation chain. That is roughly seven gates instead of ten, with every
property that matters for this system preserved, and it deletes F-9 and F-10 as a side effect.

---

## 5. Proposed ADR supersessions

**None.** ADR 0002 is out of scope by instruction, and having read it and verified its factual basis
against `yawn.deploy@4937657`, I would not propose superseding it. The Caddyfile is exactly as the ADR
describes — `http://memory.ctharvey.me { respond 403 }`, the vhost with a manually provisioned Origin
CA certificate and `client_auth mode require_and_verify` against the Cloudflare origin-pull CA, a single
`@menhir-operations` matcher covering `/ops/mcp`, `/ops/mcp/*` and
`/.well-known/oauth-protected-resource/ops/mcp` handled with `uri strip_prefix /ops`, and a
`@menhir-public` matcher to `menhir-prod-app:8099` with a terminal 404. A shared TLS terminator serving
four vhosts having exactly one owner who is not one of its tenants is correct, and cutting a live
production hostname to a private tunnel behind nine review gates is the higher-risk path.

Two observations on the ADR's own execution, offered as notes and not as findings:

- The revised route table is **accurate to the deployed configuration**, including the subtle case. Row
  2's rationale — "the path does not begin with `/ops` so `strip_prefix` does not apply" — is correct
  Caddy semantics for `/.well-known/oauth-protected-resource/ops/mcp`, even though the real config
  routes it through the same `handle` block as the stripped paths. The spec models one matcher as two
  ordered rows, which is a modelling difference with identical observable behaviour. This closure was
  checked against ground truth and holds.
- The ADR's three "open items this ADR does not close" remain open in the specification, and one of
  them is load-bearing for F-10: whether anything besides the `Caddyfile` block still depends on
  shared-Caddy behaviour for Menhir "has not been fully traced", and `caddy-release.sh` "was reviewed at
  header and grep level only". I traced its lock usage and found the collision in F-10. The rest of its
  1948 lines remain unread by anyone.

---

## 6. Coverage statement

**Read in full:**

- The specification, all 958 lines, before opening any other document.
- `git show 2a51408`, the complete diff (239 insertions, 102 deletions).
- `.agent/plans/menhir-deployment-control-plane-architecture-reset-2026-09-08.md`, all 362 lines.
- `.agent/adr/0002-menhir-production-ingress-ownership.md`, complete.
- `yawn.deploy@4937657` `Caddyfile`, the `memory.ctharvey.me` vhost and its `http://` block.
- `deploy/scaffold/menhir-scaffold.sudoers`, complete.
- Commit messages for `fc839e3`, `70e8ee6`, `88ae245`, `c89f522`, `2a51408`.

**Read in part, targeted:**

- Lock usage across `deploy/**` (`flock`, `LOCK_EX`, `LOCK_NB`, `.lock`) — grep results only, not the
  script bodies.
- `yawn.deploy` `caddy-release.sh` lines 61, 81, 136, 166 and `releases.json:194` — grep hits only. The
  remaining ~1940 lines were not read.
- `yawn.vps@b1191b8` `menhir_server.py` — structure and constants only (94 lines, skimmed).
- `C:\Users\thron\IdeaProjects\scripts\menhir-backup-archive.ps1` — grep for ssh/sudo/param constructs
  and line count. **The remaining ~185 lines were not read.** F-6 rests on the grep hits at lines 71,
  141, 145, 151, 172.
- `deploy/` directory listing and `deploy/scaffold/` listing.

**Not read:**

- The Menhir application, Neo4j schema, and OAuth/MCP source. Out of scope.
- `deploy/personal_promote.ps1`, `deploy/personal_stage_vps.py`, `deploy/scaffold/menhir_app_only.py`,
  `menhir_scaffold.py`, `menhir_security_config.py`, and the Ansible roles. Implementation defects are
  out of scope; I read the implementation only where it bore on realizability.
- The prior architecture review that returned `ARCHITECTURE NEEDS REVISION`. I did not seek it out, to
  keep this review independent. I have not compared my findings to its eight issues, and I make no
  claim about overlap.
- `archolith_oauth` entirely.
- The remaining `yawn.deploy` files: `check-drift.sh`, `remote-deploy.sh`, `docker-compose.yml`, tests.

**Could not verify, and did not attempt:**

- **Anything about the live host.** No host or production system was contacted, per instruction. This
  materially limits three findings: F-6 (whether the production sudoers grants `python3 -` is unknown);
  F-3 (the VPS's actual clock and NTP configuration); F-10 (whether `caddy-release.sh` is actually
  installed and invocable on the production host, or only present in source).
- **Whether `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` is available
  on the VPS kernel.** It requires Linux 5.6+. The specification allows "an explicitly tested
  equivalent", so this is not a finding, but the intake design's central mechanism is unconfirmed
  against the target.
- **Four of the five repository remotes** in the canonical identity registry. Only
  `github:ctharvey/workspace-meta` was confirmed.
- **Whether the census scope gap in F-6 is unique.** I found one unlisted wrapper by listing one
  directory. I did not perform the full writer census — that is Phase 0's job, and its absence as a
  *record* is F-5. There may be more.

**Method note.** I formed my view of the specification before reading the revision diff, and read the
plan and ADR last. I did not treat the plan's closure table or the acceptance ledger as evidence. Nine
of the ledger's twelve `READY FOR REVIEW` rows are contradicted by at least one finding above; the rows
"Canonical repository identity registry closed", "Descriptor-safe intake closed", and "Owner key custody
closed" are, in my judgement, genuinely ready.
