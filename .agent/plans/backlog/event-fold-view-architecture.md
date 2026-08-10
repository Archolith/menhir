# Event → Fold → View — the memory architecture (not N special node types)

> **RELOCATED 2026-08-07 (curator audit, ctharvey-approved): moved back from
> `docs/research/direction/` to `.agent/plans/backlog/`.** This doc describes the SHIPPED,
> CURRENT architecture — every mechanism named below (`services/windowed_fold.py`,
> `services/view_entropy.py`, `services/quantstate_consolidator.py`,
> `infrastructure/view_repository.py`) exists in `src/menhir` today. Per corpus convention,
> `.agent/` is the operational surface for the shipped system; `docs/research/` is forward
> research. This doc no longer fits the research corpus now that its content is fully
> realized, not speculative. It was originally relocated here-to-there on 2026-07-11
> (commit `998305f`) when it was closer to open design rationale; that call has been
> reversed now that "realized in code" is unambiguous. Reunited with its companion doc
> `aggregation-as-consolidation.md`, which stayed in this directory throughout.

**Status: ARCHITECTURAL FRAME (2026-07-02).** The load-bearing conclusion of the write-time
consolidation work. Supersedes the open month-old question "how many special memory types does
Menhir need?" — the answer is: **very few node types, a growing library of folds.**

## The three layers

```
Event   (immutable, typed, provenance-bearing)     — the substrate; append-only
  ↓
Fold    (deterministic transformation)             — write-time; the LLM's ONLY role is
  ↓                                                   perception at the Event boundary, never
View    (supersedable, recallable state)             inside the fold
  ↓
Recall
```

Everything built this session fits, with ONE node shape at the View layer:
- **FailureEvent** = Event.  **Episode** = Event.
- **count / +1-−1 / chronological sort / last-value** = Folds.
- **QuantState counter** = View.  **Timeline** = View.  **current-preference / current-branch** = View.

## The claim this settles

There are **not N special node types**. There is **one View node shape** (`is_<kind>`, PERSISTENT
scope, supersedable via `expired_at`/`qs_current`, provenance-linked, stamped-for-recall — exactly
what QuantState already is) and **N folds**. A "special memory type" was never a node type; it was
**a fold we hadn't written yet.** Timeline is not a Timeline node — it is a chronological fold over
dated events. Current-preference is not a Preference node — it is a last-value fold.

Consequence: **to add a capability you write a fold, not migrate a schema.** Folds are cheap,
deterministic, unit-testable, additive. Schemas are expensive and rigid. This is why "primitive
explosion" (ten unrelated node types) is unnecessary — resist it.

## Why this frame, and not the read-time one (retroactive fit)

- **Everything reliable this session was a fold; everything flaky was an LLM doing a fold's job.**
  SQL GROUP BY over failure_events: exact. +1/−1 fold: exact. LLM asked to COUNT weddings: 8%.
  The LLM's correct role is perception at the Event boundary (prose → typed events), never the
  arithmetic inside the fold.
- **The read-time graveyard fits too:** oracles, rerankers, BriefBuilder all tried to be folds at
  READ time — recompute the useful state per query. A fold belongs at write time, materialized as
  a View, not re-run on every recall. "Selection vs representation" and "Event→Fold→View" are the
  same insight from two angles.

## The one honest gap: pure vs. stateful folds

A pure fold is `f(events) → view` — stateless, re-runnable (FailureEvent → count → counter).
But **supersession is stateful**: recording "4 bikes" must read the prior current View ("3 bikes")
to expire it. That is read-modify-write against the existing View, not a pure reduction:
```
Event → Fold      → View   (pure:     FailureEvent → count → counter)
Event → Reconcile → View   (stateful: new value supersedes the prior current one)
```
`record_counter` already implements the stateful case correctly (reads current before superseding).
Naming it matters because **the reconciliation logic — what supersedes what, keyed on what — is the
actual hard part of any new View, and where correctness bugs will live.** "Just write a fold"
under-sells it.

Refined model: **one Event substrate · one View shape · a library of Folds — most pure reductions,
a few stateful reconciliations.** Still three categories, not ten. Still "write a fold, not a schema."

## What this governs going forward

- New capability → propose it as `(Event source) + (Fold) + (View, same shape)`. If it needs a new
  node TYPE, question it — it probably needs a new fold instead.
- The ingest primitive family (`ingest-primitive-family.md`) re-reads as: those are Event sources
  and candidate folds, not ten node types to build.
- Keep the View shape uniform (the "stamp like ingest" invariant) so recall treats every View the
  same way — the thing that made QuantState surface at all.

## Naming note: Event and View are the base objects; Fold is a verb

A recurring temptation is to rename the base objects to "Fold." Resist it — it's a layer mismatch:
- **FailureEvent is already an Event.** QuantState (the node) **is already a View.** Neither is a Fold.
- **A Fold is the transformation *between* them** — `count`, `+1/−1`, `sum`, `chronological-sort` —
  implemented as a FUNCTION (`sync_failure_counters`, the consolidator's `fold()`, a SQL GROUP BY),
  NOT a node type. A `Fold` node would be the eleventh node type this whole model exists to avoid.

**Decision — the rename to actually make eventually: `QuantState → View(kind)`, NOT `→ Fold`.**
Today the node is `is_quantstate` / `QuantStateRepository` with `qs_*` props — but QuantState is
just ONE View. The real generalization is: the node becomes a generic **View** (`is_view`,
`view_kind`, `view_value`, ...) and "quantstate" becomes `view_kind="counter"` — one kind among
future `"timeline"`, `"current_value"`, etc. Then Timeline needs no new repository; it's the same
View node written by a different fold.

**Trigger: do this when the SECOND fold produces a SECOND View kind (Timeline is the candidate) —
not now.** With exactly one kind in the graph, renaming is speculative generalization. Build
Timeline-as-a-View next: if it drops cleanly into the same node shape with `view_kind="timeline"`,
the generalization has proven itself and the rename is mechanical. If it does NOT fit, we've learned
the View shape must flex — before committing to a name. Earn the abstraction on the second case.

## RESULT — abstraction EARNED, rename DONE (2026-07-02)

Built Timeline as `view_kind="timeline"` (`infrastructure/view_repository.py`) and validated it
against a live graph. It dropped into the same node shape. Verdict: **the rename was earned, so it
was made** — the machinery now lives in a generic `ViewRepository` with a single `_write_version`
core; `QuantStateRepository` is a thin back-compat alias (`quantstate_repository.py`).

**What is genuinely shared (written once, in `_write_version`):** the recall stamps (`is_view`,
`namespace` stamped, `scope=PERSISTENT`, `freshness=ACTIVE`, `type=SEMANTIC`, `name`+`name_embedding`),
supersession (`view_current`/`SUPERSEDES`/`expired_at`, old kept), MENTIONS provenance, `view_key`
keying, and an idempotency `view_sig`. Recall needed **zero** changes — `recall_service.py` never
references `view_kind`, so both kinds surface through the identical stamped-`:Entity` path (verified:
"when did we sign the acme contract" returned the timeline View at rank 1; "how often did enrichment
time out" returned the counter).

**The ONE thing that flexed — the value slot.** A counter's value is a scalar (`view_value`); a
timeline's is an ordered list (`view_payload`, JSON). That is the honest finding: the View shape is
"identity + recall-surface + supersession + provenance + a **kind-specific value slot**." The
counter's scalar was never the general case — it was one value shape among many. Everything OUTSIDE
the value slot is universal. So the governing rule tightens to: **a new memory type = a new fold +
a new value slot on the one View node; never a new node type.**

Compat: counter nodes still carry `is_quantstate:true` + `qs_*` mirror props and reads fall back to
`qs_key`, so pre-View counters supersede correctly with no migration.

**Recall supersession — FIXED (2026-07-02).** Default recall now EXCLUDES superseded View versions
(`view_current=false`) so stale state never competes with current — the metadata filter drops them
(mirrors the SESSION/CANDIDATE filters), keyed on `meta.get("view_current") is False` so only View
nodes are affected (normal memories have it unset and pass through). `include_superseded=True`
(threaded HTTP API → backend → recall_service, next to `include_session`) surfaces them for
historical/provenance/debug recall, where each is flagged `is_superseded_view=true` end-to-end
(CandidateData → ScoredMemory → RecallMemory). `view_current`/`view_kind` were added to
`ENTITY_METADATA_FIELDS`. Verified: default returns only current (counter=6, 3-event timeline);
`include_superseded` returns both, stale ones labeled. This closes the "current vs stale" gap —
`Event → fold/reconcile → View(kind) → normal recall` now yields current state by default.

## STRUCTURE — one SSOT per kind: the `ViewKind` object (2026-07-02)

The first flat version defined each kind's value slot in TWO places — a `record_*` writer and a
`fetch_*` reader that independently re-stated the columns — so "what a counter IS" was smeared
across write and read. Fixed by making each kind ONE object that is its single source of truth in
BOTH directions:

```
ViewKind (ABC)          the SSOT for one memory type
  name                  view_kind discriminator
  key_discriminator()   the ns::subject::<seg> key segment
  signature()           idempotency (counter=value, timeline=hash of events)
  surface()             (name, summary) recall surface
  write_props()         the value slot (+ compat mirror)
  read_fields / parse() the read projection — columns defined ONCE, here
  CounterKind · TimelineKind      the two registered kinds

ViewRepository          owns only what is SHARED, fully kind-agnostic
  KINDS = {k.name: k}   registry — add a kind here, nothing else
  record() / _fetch_current()     dispatch to KINDS[name]
  _write_version()      stamps · supersession · provenance · keying  (never names a kind)
  record_counter/timeline, fetch_*   thin ergonomic wrappers over the generic path
```

**The payoff — adding a memory type is one subclass, zero repository edits.** A `current_value`
(last-write) kind = a `CurrentValueKind(ViewKind)` with its slot/surface/signature/parse + one
`KINDS` entry. The write core, supersession, stamps, and recall are untouched. This is the
invariant ("new fold + new value slot, never a new node type") enforced by the code's *shape*, not
just asserted in prose.

**The seam that is now load-bearing:** `surface`/`signature`/`write_props`/`parse` must be the
TRUE per-kind boundary. They held cleanly for the two real kinds (verified: both validation suites
pass unchanged after the refactor — identical shape, supersession, and recall behavior). If a
future kind fights the interface, that is the signal the seam — not the node — must flex. Compat
statics (`retrieval_text`, `_timeline_surface`) and `QuantStateRepository` (alias) are preserved.
