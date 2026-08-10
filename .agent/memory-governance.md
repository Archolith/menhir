# Governance — the fifth document

**The capstone of the anchor-doc set.** The four layer documents —
`memory-ingest-under-uncertainty.md`, `memory-aggregation-under-uncertainty.md`,
`memory-retrieval-under-uncertainty.md`, `memory-lifecycle-under-uncertainty.md` — establish how
each layer stays *correct* under uncertainty. This document establishes what makes them one
**governance/memory system**: not a store that remembers well, but a system in which every
consequential act — admitting a memory, asserting it as truth, destroying it, merging it — happens
*by explicit right, on recorded evidence, reversibly where possible, and reconstructibly always*.
The write-side constitution draws on the forensic-admissibility transfer review
(`IdeaProjects/.agent/reviews/menhir-frontier-transfer-forensic-admissibility.md`); the read-side
judiciary already exists in `domain/warden.py`.

---

## 1. Why memory correctness is not enough

The 2026-07-03/04 review program proved each layer's mechanisms sound — and then discovered that
the deployed system runs almost none of them: every governance mechanism is frontier-only or
flag-dormant, while the destructive paths ran live for months on a broken signal, merging 2,679
entities and deleting an uncountable number of session memories, **with no ledger able to say
what was lost**. That is the difference between correctness and governance:

> **A correctness mechanism that is not deployed, not enforced at every door, and not auditable
> is indistinguishable from its absence.** Governance is the discipline that keeps the mechanisms
> honest — and keeps their absence visible.

A memory system asks "is this true enough to store?" A governance system also asks: by what
foundation does it enter? by what right is it asserted? who is allowed to act on it? where is the
receipt? and can we undo it? Five obligations. Everything below is one of them.

## 2. The five obligations

### 2a. Admission — what may enter the record, on what foundation

> **Nothing enters the record without a foundation; content plausibility never substitutes for
> one.** The most damaging inadmissible evidence is the plausible kind.

Exists: the stamping choke point (trust metadata in one function; `locked` guard prevents
downgrade of settled scope), the perception veto-gate (aggregates commit only past a conjunctive
abstain-only chain — the model admission gate in miniature), the CANDIDATE tier (human-review
holding pen, structurally unrecallable), per-namespace ingest serialization.
Missing — the write-side constitution: raw entities and edges are admitted on extraction alone.
The forensic review's mechanism 2.1 is the roadmap: every event carries a **basis**
({STATEMENT, RECORD, DERIVED, OPINION}) and a declarant; perception is a *lay witness* — it may
transcribe (STATEMENT) and never conclude (DERIVED belongs to folds alone, each carrying its
validation card); an **ADMITTED predicate** derived from foundation, never from content. Lay
opinion is excluded categorically, not down-weighted — weight cannot compensate for
admissibility, because ranking-time voir dire never happens.

#### 2a-1. Event-history admissibility (Phases 1–4 + transport/lifecycle, implemented, default-off)

The Event History path lands with its own admission discipline, matching this document's
obligations, implemented as a default-off production-capable path at Menhir `370eff1`:

- **Evidence/quote before content.** A `TypedEventAssertion` carries `stated_span` plus
  `span_start`/`span_end`/`claim_ordinal` (or a `claim_ordinal` when offsets are absent), a source
  episode/turn evidence identity, an `evidence_tier`, and a `time_basis`. An assertion missing any
  required grounding field fails closed at construction rather than entering a weakly-grounded
  record. No content plausibility substitutes for a source span.
- **`valid_at` is the only authority.** World/source time (`valid_at`) is the sole ordering and
  selection time; `learned_at` is retained as audit/ingest time and is never authority ordering or a
  fallback for an unparseable `valid_at`. An invalid/`valid_at`-less assertion stays durable for
  audit but can never enter the View or win a selection.
- **Ambiguity fails closed.** Exact replay dedups deterministically; distinct candidates tied at the
  winning world time are `AMBIGUOUS` and produce no winner. Event siblings are occurrences and never
  supersede one another merely because one is newer.
- **Exact lane isolation.** Selection, rebuild, and reconciliation are scoped to exactly one
  `EventLane` `(namespace, subject_uuid, predicate, domain)`; sibling lanes are never folded in or
  retired. Namespace/lane isolation is exact.
- **Projection is not the source of truth.** The event timeline View is disposable/rebuildable; the
  durable `TypedEventAssertion` + evidence is the source of truth. A rebuild reports `complete` only
  after a successful View write, exact `EVENT_HISTORY_ENTRY` contributor-edge proof, and exact-lane
  reconciliation; it returns `complete=False` (fail closed) and does not begin stale-view retirement
  before the write and edge proof succeed, so it never produces a silently stale view and the durable
  assertions remain rebuildable. Namespace cleanup is event-aware and shared-head safe (the event
  history namespace lifecycle is closed); any event-specific durable repair receipt beyond the
  existing lifecycle receipts remains a rollout concern.
- **Not yet law — still default-off.** The default-off production path lands with perception/
  admission, deterministic selection, recall authority, runtime scheduling/manual Phase 3
  integration, API/backend/MCP/context transport, bounded metrics, and namespace cleanup/shared-head
  lifecycle safety; it remains flag-dormant with **no default enablement**: these rules govern a
  default-off surface until an operator explicitly enables it. Recall Lab task inspection renders
  grounded event assertions and ordered event Views alongside distinct current/change/event scalar
  roles; event-specific durable repair receipts, selected-verdict rendering, and the broader
  stratified rollout remain pending. LLM perception only; scalar contracts are unchanged; no
  canonical KU78 gain is claimed from the wiring alone.

### 2b. Assertion — what may be claimed as current truth

> **Ranking orders; wardens decide; a decision without a reason is void.**

Exists — and is the system's best governance artifact: the warden judiciary (`domain/warden.py`).
Every verdict carries its issuer and reason; chains compose most-restrictive-wins with all
contributing verdicts preserved for explainability; missing signals admit (absence is unknown,
not guilt); axes are disjoint by contract (scope, evidence-anchor, oracle-admission, currentness,
contradiction, exhaustion). The `EvidenceAnchorWarden` enforces rule zero: *retrieval and LLM
summaries are attention, not truth.* Structural trust tiers in recall (CANDIDATE never surfaces,
SESSION opt-in, superseded Views excluded) are assertion governance already shipped.
Missing: deployment (§5) — the judiciary is flag-dormant on frontier and absent from production —
and the rendering contract (bundle honesty: the answering model must see verdicts, supersession
labels, and "memory found nothing," because a verdict that never reaches the consumer governs
nothing).

### 2c. Authority — who may act, through which door

> **One policy, every door. Identity comes from tokens, never from self-assertion.**

Exists: MCP tier enforcement (per-tool `required_tier`, centrally checked — 13 operator, 14
readonly, remainder agent), namespace-wipe double guards, locked stamps.
Missing: the **one-door principle**. Menhir still has three doors — protected HTTP, the CLI hook,
and local/no-auth development posture — and the hook is still unauthenticated (Q7). The protected
HTTP part of the remedy is now landed in `plans/auth-oauth-mvp.md`: OAuth 2.1 resource server,
scopes ↔ tiers on every surface, and `sub`-derived identity.

### 2d. Accountability — every consequential decision reconstructible

> **A destructive act without a receipt is a defect, not an event. Receipts are for audit and
> never for ranking** (the aggregation doc's §7, generalized to the whole system).

Exists — a genuine receipts culture, scattered: gate receipts (`view_audit_*`), merge receipts
(`merged_from` — the only surviving evidence of the merge damage), the revision sidecar (every
compression's original), warden reasons, lifecycle actions, telemetry events; planned: abstention
receipts by firing veto, identity receipts by band, reachability receipts by lens.
Missing: the **decision ledger** — one queryable answer to "who refused/merged/deleted/admitted
what, when, on which evidence." Today receipts live per-layer in shared, unattributed sinks
(prod/bench/test in one telemetry DB), and the review program's forensics had to be assembled by
hand. The ledger is what turns receipts into accountability. Corollary: silent failure is an
accountability breach — the hook's blanket-except (recall dead, nobody told) is the same defect
class as a missing receipt.

### 2e. Reversibility — wrong decisions must be undoable, or gated hardest

> **Reversibility monotone in corroboration** (canon from the ingest and lifecycle docs, now a
> system-wide obligation): the more irreversible the act, the more independent the evidence it
> requires — and an irreversible act with no undo trail requires the most of all.

Exists: bridged deletion, keep-both conflict defaults, the revision archive, the three hotfix
disarms of scalar-gated destruction.
Missing (all planned, none built): unmerge-sufficient audit trails before any merge executes,
the raw-capture fallback for terminal ingest failures, archive-reading rehydration (compression
is currently a one-way door), a recoverable middle rung for session consolidation, and destruction
*warrants* — a destructive sweep should name its authorizing policy and evidence in its receipt,
so "why is this gone" is always answerable.

## 3. The conservation law — the invariant that ties it together

From the forensic review's Part 5, adopted as menhir's single deepest check:

> **Belief is conserved from admitted evidence.** Folds reorganize admitted facts and never create
> them; perception transcribes and never concludes; every assertable node's provenance chain must
> terminate in admitted raw events.

Menhir already has the expensive halves — provenance edges and replayable folds. Once the
ADMITTED predicate exists (2a), the conservation law becomes a mechanically checkable audit: any
recallable node whose chain fails to terminate in admitted evidence is a violation. This also
buys the impossible question cheaply: *"strike that from the record"* — exclude a declarant, a
session, or an extractor version at the gate, replay the folds, diff the Views.

## 4. Deployment is a governance property

The diff between checkouts is the program's most consequential finding restated as law:

> **A control that is not deployed in front of the asset it governs does not exist.** The
> promotion ladder (shadow → flag → measure → default) is the legislative process; a mechanism
> parked at "shadow" indefinitely is a bill that never became law, and the delta between the
> designed system and the deployed system must itself be tracked (tracker D5) — an unowned delta
> is where governance quietly dies.

Two corollaries. *Implicit policy is forbidden:* consolidation running as a side effect of every
process start (tracker D3) and decay having no invoker at all (D2) are policies nobody enacted —
each sweep needs an owner, a cadence, and a receipt. *Both checkouts or neither:* any control
worth having in frontier is worth a landing path in production, hotfix-style, until the forks
converge.

## 5. What each layer owes the governance frame

| layer | already compliant | owes |
|---|---|---|
| ingest | stamping choke point; lease custody (the queue has better chain-of-custody than the memories) | foundation fields + ADMITTED (2a); identity receipts; raw-capture (2e) |
| aggregation | the perception gate IS the model admission gate; receipts as gate inputs only | fold validation cards (Daubert analog); abstention receipts persisted |
| retrieval | structural trust tiers; warden judiciary built | deploy the judiciary; rendering contract; reachability receipts |
| lifecycle | keep-both conflicts; type exemptions; disarms in place | destruction warrants; archive-reading rehydration; lawful signals before re-arming |
| platform | MCP tier model; namespace guards | one door (Q4/Q7 + OAuth); the decision ledger; sink attribution (Q2) |

## 6. Summary — the principles, portable

1. **Correctness undeployed is correctness absent.** Governance keeps mechanisms honest and their
   absence visible; track the designed-vs-deployed delta as a first-class artifact.
2. **Foundation before content.** Nothing enters the record without a basis and a declarant;
   plausibility is the failure mode, not the credential. Weight never substitutes for
   admissibility.
3. **Ranking orders; wardens decide; every decision carries its reason.** And the reason must
   reach the consumer, or it governed nothing.
4. **One policy, every door.** Three doors with three policies is zero policies. Identity from
   tokens, never self-assertion.
5. **No receipt, no act.** Destructive operations require warrants; receipts feed a single
   queryable ledger; receipts audit and never rank. Silent failure is a missing receipt.
6. **Reversibility monotone in corroboration, system-wide.** The irreversible act gets the
   strongest gate and leaves the richest trail.
7. **Belief is conserved from admitted evidence.** Folds reorganize, perception transcribes,
   provenance terminates in admitted events — mechanically checked, routinely audited.
8. **Implicit policy is forbidden.** Every sweep, cadence, and default names its owner and its
   authorizing decision. A behavior nobody enacted is a bug wearing a schedule.
