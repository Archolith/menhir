# Fold Algebra — the minimal deterministic operation set

> **Status note 2026-08-08 (curator audit).** The line below is stale — implementation IS done;
> see this doc's own "Implementation — the algebra is now real (2026-07-02)" section further down,
> `src/menhir/domain/fold_algebra.py` (module docstring cites this file as its design of record),
> and `src/menhir/services/event_fold.py`. Five-plus downstream commits build on it (Levers A/B/C1/
> C2/C3). Kept as the design-of-record document per its own header comment in the code.

**Status: DESIGNED 2026-07-02 (charter → design, per `HANDOFF-2026-07-02-fold-algebra-design.md`).
Implementation still NOT started — this document is the vocabulary and its laws, not code.**

## Verdict, up front: it collapses

The candidate list — `COUNT · SUM · MIN/MAX · DISTINCT COUNT · CURRENT · LATEST · DELTA · DATEDIFF ·
WINDOW · TIMELINE · GROUP BY` — is **not eleven operations**. It factors into a three-stage pipeline
whose middle stage is **four reducers**, all classic monoids:

```
View  =  δ? ∘ ρ ∘ σ?      over the keyed event stream for one view_key
```

| stage | name | type | what lives here |
|---|---|---|---|
| **σ** | SHAPE | `[Event] → [Event]` | filter by predicate / time range (WINDOW) |
| **ρ** | REDUCE | `[Event] → Accumulator` | **SUM · EXTREME · SET · LIST** — the whole fold vocabulary |
| **δ** | DERIVE | `Accumulator(s) → Answer` | pure scalar arithmetic: DATEDIFF, cardinality, subtraction (DELTA) |

Everything else on the candidate list is a **named composition** or **not an operation at all**:
GROUP BY is `view_key` construction (already exists); WINDOW is σ (with a materialization caveat,
below); CURRENT/LATEST is the EXTREME monoid evaluated incrementally — not a separate stateful op.

This is the fourth simplification in the sequence (oracles → routed subsets, Views → one generic
shape, value slots → ViewKind SSOT, now aggregation → four monoids). The either-way bet in the
charter resolves to: **seam found.**

## The four reducers (ρ) — signatures and laws

An Event here is the typed, dated, provenance-bearing record perception emits:
`(when: datetime, key: view_key, kind: EventType, value?: number, identity?: str, what?: str, episode_uuid)`.
The LLM's only role is producing these at the Event boundary; ρ/δ never call a model.

| reducer | signature | identity | combine | **idempotent?** | named forms it subsumes |
|---|---|---|---|---|---|
| **SUM** | `[Event] → ℝ` (Σ e.value) | 0 | `+` | **no** | SUM; **COUNT** = SUM ∘ map(1) |
| **EXTREME(k, dir)** | `[Event] → Event` (arg-min/max by k) | ⊥ | pick by k | **yes** | MIN/MAX(value); **LATEST/CURRENT** = EXTREME(valid_at, max) — the LWW register |
| **SET(id)** | `[Event] → Set[Id]` | ∅ | ∪ | **yes** | **DISTINCT COUNT** = δ‖·‖ ∘ SET |
| **LIST** | `[Event] → [(when, what, ep)]` sorted by when | `[]` | merge-sort ∪ (dedup by entry identity) | yes, *iff entries carry identity* | **TIMELINE** (the lossless fold) |

Two properties are load-bearing, not decoration:

1. **Every reducer is a monoid** (associative combine with identity). That is what makes a fold
   deterministic, order-insensitive where it should be, unit-testable, and — critically — legally
   evaluable either in batch or incrementally (next section).
2. **LIST is the escape hatch.** It is the free monoid — zero information loss. Any question the
   write-time vocabulary didn't anticipate is a read-time δ over the timeline payload. This bounds
   the worst case: an unanticipated query never requires going back to raw episodes as long as a
   timeline View exists for the subject.

## The pure-vs-stateful split, sharpened: one monoid, two evaluation modes

The architecture doc split folds into *pure reductions* vs *stateful reconciles* and flagged
reconciliation as "where correctness bugs live." The algebra sharpens that: **there are not two
kinds of operations. There is one monoid per kind, and two ways to evaluate it:**

```
batch (re-fold):        View  = fold(all events)                      — stateless, re-runnable
incremental (reconcile): View' = combine(prior View, fold(new events)) — read-modify-write
```

Incremental evaluation is legal **iff** the homomorphism law holds:

```
fold(E₁ ∪ E₂)  ==  combine(fold(E₁), fold(E₂))
```

When a reconcile is buggy, it is because this law was silently violated. The three concrete ways it
breaks — each anchored to tonight's evidence — are the algebra's correctness laws:

### Law 1 — Ordering (the LWW law) · *EXTREME/CURRENT/LATEST*
`combine` for EXTREME(valid_at, max) must compare **event time (`valid_at`)**, never arrival/write
order. The to-watch case (D0 Arm B: 25 *and* 20 both extracted) is exactly this hazard.
**Gap found in current code — NOW FIXED (2026-07-02, landed in view_repository.py within commit
`9ee3443`; bundled there via a shared-branch working tree).** Previously `_write_version` superseded
whenever the signature differed — never comparing `valid_at` — so the reconcile was *arrival-ordered*
and, fed events out of temporal order, would install a stale total as current. The fix makes the LWW
law explicit and kind-scoped: `ViewKind.lww_register` (True for `CounterKind`, False for
`TimelineKind`), `_current_by_key` now returns the current `valid_at`, and `_write_version` takes
`require_newer` — for a register it **skips** a value change whose `valid_at` is strictly older than
current (returns `stale_skipped=True`, current stays authoritative). Set/list kinds keep sig-driven
supersession (guard does not fire). Verified end-to-end on a live graph: counter v5@Jun → v6@Mar
(older) skipped, current stays 5 → v7@Sep supersedes; timeline earlier-dated correction still
supersedes. Callers no longer need to feed registers in order.

### Law 2 — Dedup (the replay law) · *SUM/COUNT (and LIST without entry identity)*
Non-idempotent reducers double-count on replay; perception can and will re-emit. Guard options,
in preference order: (a) **re-fold from the full event set** (always correct — batch mode); or
(b) incremental with **event-identity dedup**, where the View's MENTIONS-linked episode set *is*
the "already folded" ledger — skip events whose episode is already linked. Idempotent reducers
(EXTREME, SET) are replay-safe by construction and may reconcile freely. **This is the practical
pure/stateful boundary: idempotent monoids may reconcile; non-idempotent monoids re-fold or dedup
on provenance.**

### Law 3 — The RESET corner (cross-source reconcile) · *the one genuinely hard spot*
When a **stated total** (assertion event) and **item events** coexist on one key — "I have 3 tanks"
then later "bought another tank" — no single monoid covers the key. The composition is anchor+delta:

```
CURRENT = EXTREME(valid_at) over assertions  .value   +   SUM(item events with when > anchor.valid_at)
```

i.e. an assertion **re-bases** the accumulator; later events adjust it. This is precisely where the
architecture doc's "reconciliation is where correctness bugs live" lands, and it is the only place
two reducers must be reconciled *against each other*. **Named, not designed further, not gating:**
none of the 14 D0 questions requires it (each falls cleanly to one move), so it waits for demand.

## Composition — what a View(kind) is

**A View kind names one declared pipeline `(σ?, ρ, δ?)` — ρ mandatory, σ/δ optional; most kinds are
ρ alone.** Pipelines are fixed at design time inside a `ViewKind`; they are **not** a runtime DSL.
That is the anti-stream-processor guard: the algebra is a *vocabulary + laws for writing ViewKinds*
(each fold stays a plain function), not an engine that interprets pipelines. The vocabulary's job is
that every new fold arrives knowing (a) which reducer it is, therefore (b) which laws bind it.

The non-ops, resolved:

- **GROUP BY is keying, not an operation.** `view_key = ns::subject::discriminator` already *is* the
  partition; folds run per key. The real decision GROUP BY hides is **keying granularity, and it
  belongs to perception**: to count tanks, perception must key item events to the aggregate subject
  ("fish tanks"), not one key per tank. Calendar bucketing (below) is the same decision.
- **WINDOW is σ, and relative windows must never be materialized.** "Last month" changes meaning
  daily, which fights write-time materialization head-on. Two legal forms:
  (a) **bucketed keying** — the discriminator carries a calendar bucket (`pages_read::2026-06`), and
  a relative window resolves *at read time* to bucket key(s); or
  (b) **read-time δ over LIST** — filter the timeline payload by date range.
  Pick per kind; never write a View whose truth decays with the calendar.
- **DATEDIFF is δ** — pure arithmetic over dates already produced by ρ (EXTREME endpoints, LIST
  span: `last.when − first.when`). Not a fold; never touches events.
- **DELTA is δ over the supersession chain.** The SUPERSEDES link already materializes version
  history (`ViewRepository.history()`); current − previous is a read-time derivation. Free.

## Map to View(kind) — where each reducer's output lands

Governing principle (generalizes what Timeline already does): **the value slot stores the monoid
accumulator; the recall surface is the answer projection.** For SUM the accumulator *is* the answer
(scalar); for SET the accumulator is the set and the answer is its cardinality; for LIST the
accumulator is the entries and answers (span, windows, count) derive from it. Storing the
accumulator — not just the answer — is what keeps incremental evaluation lawful.

| reducer | accumulator | View kind / slot | status |
|---|---|---|---|
| SUM / COUNT | scalar | `counter` (`view_value`) | **exists** |
| EXTREME(valid_at), numeric | scalar + valid_at (version chain is the accumulator) | `counter` + supersession | **exists — needs the Law-1 guard** |
| LIST | ordered entries | `timeline` (`view_payload`, count mirrored in `view_value`) | **exists** |
| SET | id set | future `SetKind`: set in `view_payload`, ‖set‖ mirrored in `view_value` | **deferred until demanded** — one `ViewKind` subclass, zero repository edits (the SSOT payoff) |
| EXTREME(valid_at), non-numeric | value + valid_at | `CurrentValueKind` | **still not earned** — D0 Finding 1 re-confirmed: counter surface ranked #1 in 12/14; defer |

Coverage check against the real demand (the D0 counting-14 + temporal slice):

| demand (qids) | pipeline |
|---|---|
| stated totals — pages=220, bikes=4, playlists=20, pre-approval=$400k, to-watch=25 | EXTREME(valid_at) over assertions (Law 1 is the to-watch case) |
| sums — bike-spend=$185, 36b9f61e=$2,500, 7527f7e2=$800 | SUM over purchase events (Law 2 applies) |
| dedup counts — tanks=3, citrus=3, plants=3, 0a995998, 6d550036, 18dcd5a5 | δ‖·‖ ∘ SET over item identities |
| temporal — "days between…", "last month" | δ DATEDIFF over LIST endpoints; σ bucket or read-time window over LIST |

**Nothing in the demand table falls outside σ/ρ/δ.** Arm B's move-1/move-2 split maps exactly:
move 1 (stated total) = EXTREME over assertion events; move 2 (event-log fold) = SUM/SET over item
events — same counter View, different reducer, as the D0 roadmap predicted.

## Boundary — what would falsify this algebra

Named so the breach is recognized early instead of patched quietly:

- **Most-frequent / top-k** ("which store did I order from most") needs `MAP(id → ρ)` — a keyed-monoid
  generalization of SET (SET = MAP(id → ⊤)). Still monoidal, so it's the *anticipated fifth reducer*,
  waiting in the wings. Do not build it until a real question demands it.
- **Median / percentile** is not a monoid. Served as read-time δ over LIST while lists stay small; a
  true streaming quantile would be the algebra's first real breach. Acceptable to never support.
- **Cross-key event joins** ("did I spend more on bikes than on plants" is fine — that's δ over two
  Views' answers; anything requiring event-level correlation *across* keys is outside the algebra
  and should stay outside).
- **Anything probabilistic** belongs to perception at the Event boundary — never inside ρ/δ. If a
  fold "needs the model," it is misclassified: either perception owes a typed event, or the question
  is not a View.

## Implementation — the algebra is now real (2026-07-02)

Built as pure functions in `domain/fold_algebra.py` (`Event` + the four monoid reducers + σ `window`/
`exclude_folded` + δ `datediff_days`/`delta` + a `REDUCERS` registry carrying each reducer's
idempotency), with the fold→View path in `services/event_fold.py`. No engine, no DSL, no `Fold`
node type — plain functions, laws declared in the registry. Verified end-to-end on a live graph and
by unit tests (`tests/test_fold_algebra.py`, `tests/test_view_repository_lww.py`).

1. **✅ SUM on the counter kind + Law-2** — `fold_events_to_counter(reducer="sum")`. BATCH re-fold
   (absolute value, replay-safe) is the default; `exclude_folded` is the incremental guard. Verified:
   `[$50,$135] → $185`, idempotent re-fold, `+$40` supersedes, backfilling an earlier event grows the
   sum without tripping Law-1 (a growing set never moves `valid_at` back). **Unlocks the 3 sum Qs.**
2. **✅ DISTINCT-COUNT via batch re-fold** — `reducer="distinct_count"` into a counter (`4 tank
   mentions, 1 dup → 3`). This is the δ‖·‖∘SET *answer*, computed fresh each run. **Unlocks the 6
   dedup Qs in batch mode.** Still deferred: **SetKind** (store the *set* in `view_payload` for lawful
   incremental accumulation) — one subclass + one `KINDS` entry, when incremental is demanded.
3. **✅ Law-1 valid_at guard in `_write_version`** — `ViewKind.lww_register` + `require_newer` (see
   Law 1 above). Stated-total questions are now correct under out-of-order feeds.
4. **σ bucketed keys / δ DATEDIFF helpers** — `window`/`datediff_days` exist; the calendar-bucket
   keying for relative windows lands when the temporal slice is actually run.

Not yet wired: **perception** (episodes → typed `Event`s), the LLM boundary D0 Arm B measured. The
deterministic core above is what the fold needed; perception is a separate, upstream concern.

---

## Appendix: the charter this design fulfilled (2026-07-02)

The original brief asked: *what is the minimal set of deterministic operations from which every
Menhir View can be composed?* — with the trap being to implement COUNT first and accrete a stream
processor one-off by one-off. Evidence base: D0 Arm A (representation collapses retrieval to
rank-1/1-node/~21 tokens at the oracle ceiling) and Arm B (stated-total perception is reliable at
5/5; the ~9 remaining questions need a deterministic fold, not a better model). Full results:
`archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`. Architecture frame:
`.agent/architecture.md` (Event → Fold → disposable View/projection) and `.agent/data_models.md`
(current View kinds and projection contracts). The original frame is retained at
`.agent/archive/plans/event-fold-view-architecture.md`.
Design handoff: `.agent/for-review/HANDOFF-2026-07-02-fold-algebra-design.md`.

---

## Appendix B: deferred partial extensions (from the 2026-07-11 research-gap sweep)

Three partials surfaced by the research-vs-backlog sweep fold into this plan rather than earning their
own. Each is a *seam sharpening* on the existing algebra, not a new capability — recorded here so they
are not lost, none gating.

### B.1 Completeness watermark (`complete_until`) — the missing honesty bit

Law 1 makes reconciles correct under out-of-order arrival (`valid_at` guard), but a consumer still
**cannot distinguish "counter = 3, final" from "counter = 3 so far, two episodes still in the
enrichment queue".** The fix is one View property — `complete_until = last folded event time` (a
watermark: "complete up to T; later data may still revise"). Late events either merge (order-insensitive
reducers) or supersede-only-if-newer (EXTREME/LWW, already guarded). Small; deletes the unstated
"callers must feed events in temporal order" landmine and gives the belief gate + the View-substitution
coverage boundary (`view-summary-substitution-plan.md` step 4) a real 1006-coverage stamp.
*Source:* cross-domain review §A.2 (watermarks) + arch-critique #4.

### B.2 `caused_by` event field + causal Views

Substrate exists — `CAUSED_BY` is already an edge type in `domain/edges.py` — but there is **no
perception discipline of filling a `caused_by: [event_ids]` field at emission time**, and no folds over
it. One field, filled by perception when the text states causation ("because", "after the deploy
failed", retry-of), gated by span-grounding (`span-grounded-extraction-verification-plan.md`), unlocks a
family of causal Views: retry chains (`SUM` over chains), root-cause closure, recurring failure
sequences (gives the directly-follows View true causal edges, not just temporal adjacency). Record the
field now (cheap), exploit the Views on demand. *Falsifier:* sample 50 session events; if <10% have
statable causal parents, the field is too sparse — archive. *Source:* cross-domain review §C.9.

### B.3 Knowledge-compilation registry (fold admission by measurement)

The `REDUCERS` registry above declares each reducer's *idempotency*; it does **not** yet govern *which
Views earn to exist*. Add the query-class governance table Doc §14 calls for: *query class → sufficient
View kind → compilation (fold) cost → answer cost achieved*, checked by the nightly view-entropy probe.
A fold with no registry entry is by definition speculative — this mechanizes the anti-accumulation bias
(every fold must name the query class it compiles). Near-free (a table here + probe wiring). *Falsifier:*
if after a quarter the registry never vetoed or prioritized a fold, it is ceremony — drop it. *Source:*
cross-domain review §E.14.
