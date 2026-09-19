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

1. ~~How many `previous` roots, retained how long?~~ **DECIDED 2026-09-17: exactly one, replaced
   at the next promotion.** Undo always works for the most recent promotion, and the cost is
   bounded — roughly 2x structure size per project, never growing.

   The trade accepted with it: a bad promotion discovered AFTER a later good one is no longer
   reversible in place. That is the right default because it keeps the cost a constant rather than
   a function of promotion frequency, and because a promotion that is wrong is usually wrong on
   the next query rather than a week later.

   Two consequences to build in rather than discover:
   - **The retention rule is the gate's escape hatch**, so `restore from previous` must be tested
     against the state where a second promotion has already replaced it — the case where restore
     correctly REFUSES. An escape hatch that silently restores the wrong generation is worse than
     one that says no.
   - Anyone who needs a longer window needs an explicit export before promoting, not a deeper
     retention setting. Worth stating in the operator docs when P4 ships.
2. ~~Does a degraded view refuse reads, or serve with a warning?~~ **NOT OPEN — the parent plan
   already decides it, and this document was wrong to call it a contradiction.** Line 738: a
   degraded view is fail-closed, meaning *reads carry a warning*, uploads may still become
   immutable candidates but cannot be promoted into that view, and repair is an explicit operator
   action. Invariant 10 adds that failed compensation blocks further commits.

   "Degraded-view blocking" in the gate never meant blocking READS; it means blocking
   PROMOTIONS. The two statements agree and I read a conflict into them.

3. ~~Is promotion operator-only for the whole pilot?~~ **NOT OPEN for P4.** Line 639 specifies
   operator-only access for both pilot projects. The parent plan's open decision 5 — trusted CI
   versus explicit maintainer publish — is about the eventual canonical policy, not this phase.
4. ~~The omission behaviour (threat 4).~~ **DECIDED 2026-09-17: refuse the bundle.** A manifest
   whose own declarations do not parse is one the server cannot reason about, so it fails at
   upload, before any graph write.

   The reason it has to be strict is in how deletion is signalled: **the manifest carries a
   deletion COUNT, not the deleted paths** — those stay client-side in `BundlePlan.deleted`. So
   the server infers deletion from absence, and a dropped omission is indistinguishable from a
   deleted file. Leniency here is not "ignore a bad field", it is "prune a file the client
   explicitly said it was keeping".

   Cost accepted: a client bug blocks syncing until it is fixed. That is the point — the
   alternative fails silently and takes data with it.

   **This is a change to `protocol.py`, which is P0-frozen.** It is additive in the sense that no
   existing code changes meaning, but a bundle that parsed yesterday may be refused tomorrow, so
   it lands with the P4 work rather than as a drive-by. `test_a_malformed_omission_is_silently_dropped_which_is_worth_knowing`
   characterises today's behaviour and is where the change registers.

## Build order, and why the backup restore now earns its place

1. **Counterexamples first**, as in P3 — kill at every state, both promotions racing, compensation
   failing, restore from `previous`. The gate is written as failure cases, so the tests come first.
2. The view pointer and its CAS, on a throwaway project.
3. Promotion, deletion and compensation.
4. **The pilot against restored data.** This is where the earlier backup-restore proposal belongs:
   P3 never touched the graph, so restoring a backup proved nothing there. P4 writes to it, and
   testing graph writes against realistic structure is exactly right — locally, in a container,
   with no tunnel and nothing exposed.
