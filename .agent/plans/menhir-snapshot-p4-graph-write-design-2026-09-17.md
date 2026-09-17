---
artifact_schema: 1
artifact_type: plan
artifact_status: PROPOSED
---

# P4 graph write: what makes a partial write survivable

Parent plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P4)
Status: **PROPOSED — not approved, not implemented. No graph write exists yet.**

## The one way P4 differs from everything before it

Every prior phase could undo itself. The extraction writer deletes its root on any failure,
including `KeyboardInterrupt`, and a test proves nothing survives. That worked because a
half-written directory can be removed in a single call.

**A half-written graph cannot.** There is no `rmtree` for nodes and edges that are already visible
to readers, and a compensating delete is itself a write that can fail halfway. So the P3 strategy —
*leave nothing partial behind* — is not available, and repeating it here would be the mistake.

The strategy inverts:

> **P3:** a failure leaves nothing.
> **P4:** a failure may leave plenty, and none of it may be readable as complete.

Everything below follows from that. The dangerous outcome is not a crash; it is a graph that
answers questions confidently using half a snapshot, where nothing about the answer says so.

## What the gate demands, and why each item is there

The parent plan's gate is unusually specific, and reading it as a checklist misses the shape:

| Gate item | The failure it is about |
| --- | --- |
| deletion has a tested outcome | a file removed from a project must leave the graph, and must not take a live node with it |
| failed write has a tested outcome | the write stops mid-way and something must still be true afterwards |
| **process kill at every state** | the writer dies where it is least convenient, repeatedly |
| failed compensation has a tested outcome | the undo itself fails — the case most designs assume away |
| restore from `previous` | the operator's escape hatch actually works |
| no name-keyed prune remains | #99's bug class cannot come back through a new door |

"Failed compensation" is the one worth dwelling on. Every rollback design assumes rollback
succeeds. If it does not, the system is in a state no code path intended, and the only honest
answer is to make that state *loud* rather than to pretend it cannot happen.

## Mechanism: the pointer flip

Writes go to a NEW view root. Readers keep seeing `current` until a single atomic pointer moves.

```
  build → verify → FLIP (atomic) → previous := old current
```

- **Before the flip**, a partial write is invisible: nothing reads the new root, so dying mid-build
  leaves garbage that a sweep reclaims. This is the P3 property recovered — the part of the work
  that *can* be made all-or-nothing.
- **The flip is the only operation that must be atomic**, which is the point of the design. One
  compare-and-swap, on one pointer, guarded by the view's generation.
- **After the flip**, `previous` is the escape hatch the gate asks for, and rollback is another
  flip rather than an inverse write.

Compensation therefore means *flip back*, not *undo each node*. That is what makes "failed
compensation" rare enough to reason about: the failing operation is a single pointer move, not a
traversal.

## What authorizes each transition

The session's recurring lesson applies here more than anywhere: **every transition needs a durable
fact that authorizes it**, and elapsed time, local object state, or "the other writer must be
finished" is not one.

`admit_structure_writer` already does the hard version of this for structure writes — the fence
check, the identity check and both registrations are ONE statement, because validating a claim in a
separate statement lets a transfer land in between. **P4 must not invent a second, weaker
mechanism.** The view CAS extends that pattern; it does not replace it.

Specifically:

- The flip is authorized by `(project_id, view_key, generation)` — not by project name, and not by
  "this writer built the root". A writer that built a root and then lost the identity must not
  flip.
- The generation is re-checked AT the flip, not at the start of the build. A build takes minutes;
  ownership can transfer inside that window. That is the same failure `admit_structure_writer`
  documents, and the extraction lease was built for.
- A snapshot may be promoted at most once per view generation. Promotion must be idempotent by
  `snapshot_id`, because a retried promotion after an ambiguous failure is the normal case, not the
  exotic one.

## Threats specific to writing

| # | Threat | Mechanism |
| --- | --- | --- |
| 1 | **Partial write read as complete.** | Nothing reads the new root before the flip. A view's completeness is a property of the pointer, not of the nodes. |
| 2 | **Two promotions racing for one view.** | The flip is a CAS on the view generation. The loser refuses; it does not retry blindly into the winner's state. |
| 3 | **A deletion that removes a live node.** | Deletions are computed against the snapshot's manifest, and only within the view being replaced. This is #99's bug class — the prune must be keyed on the owning identity, never on a name. |
| 4 | **An omission read as a deletion.** | A file the client deliberately omitted is not absent, it is declared. **This is the flagged `protocol.py` behaviour becoming live:** malformed omissions are currently dropped silently, so a dropped omission becomes a prune. Inert in P3; a data-loss path in P4. |
| 5 | **Compensation fails.** | The view is marked degraded durably, reads carry that status, and promotion into it is refused until an operator acts. A degraded view is a fact to be surfaced, not an error to be swallowed. |
| 6 | **A writer dies holding the view.** | Same shape as the extraction lease: the claim expires, no operator action required, and PID death is never the evidence. |
| 7 | **`previous` is gone when it is needed.** | Retention is explicit and bounded, and restore is tested rather than assumed. An escape hatch nobody has opened is not an escape hatch. |

## Open decisions for the owner

1. **How many `previous` roots, retained how long?** One is enough to undo the last promotion and
   not enough to undo a bad one discovered a week later. The cost is graph size, which is the
   thing P4 is adding.
2. **Does a degraded view refuse reads, or serve with a warning?** The parent plan says
   "degraded-view blocking" in the gate and "reads carry a warning" in the invariants. Those are
   different products: one stops a caller, the other trusts them to notice.
3. **Is promotion operator-only for the whole pilot, or does trusted CI promote?** Open decision 5
   in the parent plan. It decides whether the first graph write can happen unattended.
4. **The omission behaviour (threat 4).** Flagged and characterised by test today; it must be
   settled before the first write, because the failure is silent data loss rather than an error.

## Build order, and why the backup restore now earns its place

1. **Counterexamples first**, as in P3 — kill at every state, both promotions racing, compensation
   failing, restore from `previous`. The gate is written as failure cases, so the tests come first.
2. The view pointer and its CAS, on a throwaway project.
3. Promotion, deletion and compensation.
4. **The pilot against restored data.** This is where the earlier backup-restore proposal belongs:
   P3 never touched the graph, so restoring a backup proved nothing there. P4 writes to it, and
   testing graph writes against realistic structure is exactly right — locally, in a container,
   with no tunnel and nothing exposed.
