# Plan: Perceiver versioning + re-fold (insurance for when perception improves)

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11. **Additive insurance, not a rearchitecture** — the current
episodes -> perceive -> fold -> View pipeline works and is kept as-is.
**Gap source:** `.agent/research/menhir-cross-domain-representation-research-2026-07-02.md` §A.1.

---

## The gap (one line)

We don't record **which version of the extractor produced each View**, so when perception improves (or
a bad extractor ships), we can't tell what needs re-doing — it's all-or-nothing.

## Why this matters (plainly)

The system already treats Views as a **rebuildable cache over durable episodes** — that's exactly right,
and `perceive_and_fold` already re-derives Views from a namespace's episodes. The one thing missing is a
label: a `perceiver_version` stamp (model + prompt + schema hash) on what perception produced.

With that label you get two things cheaply:
- **Upgrades are incremental.** When the extractor gets better, re-perceive only the episodes whose
  stamp is old — not the whole store.
- **Bad extractors are recoverable.** If a new extractor turns out buggy, you can find everything it
  produced and re-do just that ("strike that from the record"). Without the stamp you can't even tell
  which Views a bad extractor touched. This is the safety net that makes the other write-time changes
  (identity, cessation, foundation admission) safe to try.

## Current default (code-anchored)

- **Works today:** `perceive_and_fold` (`scheduler_tasks.py:383`) re-derives counter Views from each
  DIRTY namespace's episodes; Views are a cache, episodes are the durable log (`event_fold.py` stores
  the accumulator + `episode_uuids`). This is the successful part — leave it.
- **Missing:** no `perceiver_version` stamp, so a re-derivation is always *all* episodes in a namespace,
  and a bad extractor's output is unidentifiable.

## Two ways to do it — pick the simple one first

| | **B. Stamp episodes (recommended — fits the working system)** | **A. Persist a typed-event log (fuller, only if ever needed)** |
|---|---|---|
| What you add | a `perceiver_version` field on each episode's last perception | store typed events as durable records, stamped, replayable |
| "Re-fold" means | re-perceive only stale-stamped episodes, then fold (reuses `perceive_and_fold`) | replay stored events through the fold with **no LLM** |
| Cost | small, additive, no new storage | a new persisted layer (meatier) |
| Guarantee | good enough: re-do only what changed | bit-exact deterministic rebuilds |

**Recommendation: do B.** The current system is successful precisely because episodes are the durable
log and Views are disposable — B leans into that. A is only worth it if we ever need bit-for-bit
deterministic rebuilds (we don't today).

## Promotion criteria (default -> versioned)

- **supported-by-spike (Option B):** every perceived episode carries a `perceiver_version`; a "re-fold"
  job re-perceives **only** episodes with an older stamp and updates their Views; a bad extractor's
  output can be listed and re-done by stamp.
- **Falsifier — note the caveat:** the research doc's test ("delete all Views, re-fold, expect an
  identical diff") only holds for **Option A** (deterministic replay). Under **Option B** re-perceiving
  uses the LLM, so it won't be byte-identical — judge it on *behavior* instead: re-perceiving a stale
  episode with the improved extractor yields the better View, and re-perceiving an up-to-date one is a
  no-op.

## Path (Option B — how to get there)

1. **Stamp** each episode's last perception with `perceiver_version = hash(model + prompt + schema)` at
   perceive time.
2. **Re-fold job:** extend the existing `perceive_and_fold` scheduler task to select episodes where
   `perceiver_version < current` (instead of whole namespaces), re-perceive those, refold their Views.
3. **Retraction helper:** "list/re-do everything produced by extractor version X" — a query over the
   stamp, so a bad extractor is recoverable.

## Non-goals

- Do **not** rearchitect the working pipeline or persist a typed-event log (that's Option A — deferred
  until bit-exact rebuilds are actually needed).
- No new node types for Option B — just a field + a smarter selection in the existing job.

## Risks

- **Low.** Additive; the stamp is inert until a re-fold uses it. Re-perceive cost is bounded to
  stale-stamped episodes.

## Source

`.agent/research/menhir-cross-domain-representation-research-2026-07-02.md` §A.1 (Kappa replay).
Code confirmed 2026-07-11: `perceive_and_fold` re-derives from episodes; typed events are transient
(episodes are the durable log); no `perceiver_version` stamp exists.
