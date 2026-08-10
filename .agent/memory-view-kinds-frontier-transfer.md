# View Kinds — Frontier Transfer from Process Control

Status: design exploration (not a plan). Ranked most → least useful.
Source discipline: industrial process control. Transfer is of *mechanisms*
(monoids, supersession policies, composition shapes), never vocabulary.

A View is a deterministic fold over a keyed event stream whose output is a
query-sufficient graph node (`domain/fold_algebra.py`, `infrastructure/view_repository.py`).
The four lawful reducers today are SUM, COUNT, DISTINCT_COUNT, LATEST/TIMELINE
(`fold_algebra.REDUCERS`). This doc asks: what other View shapes does a mature
fold-heavy discipline use, and which are lawful transfers vs. counterexamples
that test the boundary?

The fold law's real content, seen through this lens:

> A View may compress the past, but it may not predict the future or
> differentiate the present. Anything that does is a read-time δ, not a stored
> View.

Every ranking below is judged against that law plus one practical criterion:
*does this fill a query class the current ScalarState/Counter/Timeline Views
leave unanswered?*

---

## Ranking

### 1. SetpointView — `latest` over intent events

**Usefulness: high.** Fills the biggest gap in the current design.

ScalarState answers "what is X" but has no partner for "what should X be."
Without the pair, *drift* is uncomputable as a View — and drift is the query
class the system most needs ("is the user's actual spend tracking against
their stated budget?"). Process control calls the target the *setpoint* and
treats it as a first-class signal, distinct from the *process variable*.

- **Reducer**: `latest` (LWW register), identical machinery to ScalarState.
- **Supersession**: new intent supersedes prior setpoint; prior kept as the
  history of goals (same atomic create-and-supersede path as
  `view_repository.py:649`).
- **Identity**: `(subject_uuid, attribute, scope, value_kind, unit, intent=true)`
  — same slot shape as ScalarState with one discriminator bit. Cheap to add.
- **Lawful?** Yes. `latest` is idempotent, associative, has identity `⊥`.
- **Cost**: low. Reuses `ScalarStateKind` almost verbatim; the only new code
  is the `intent=true` discriminator and a `fetch_setpoint_state` mirror of
  `fetch_scalar_state`.
- **Why rank 1**: the rest of this list (Error, Integral, Cascade) depends on
  having a setpoint to compare against. This is the keystone.

### 2. DeadbandView — a supersession *policy*, not a new kind

**Usefulness: high.** Cheapest fix for a real problem.

A controller refuses to act within a tolerance band to avoid chatter. Cross-
fade: a modifier on ScalarState supersession — `record_scalar_state` refuses
to create a new version when `|new_value − current| < ε` (or, for non-numeric
ValueKinds, when `signature()` is unchanged — which already happens via
`ScalarStateKind.signature` at `view_repository.py:385`).

- **Reducer**: none (it's a gate, not a fold).
- **Supersession**: suppresses the write, not a new supersession shape.
- **Lawful?** Yes — it preserves the fold; it only prunes the version chain.
- **Cost**: very low. One threshold check in `_write_version` before the
  CREATE-and-supersede statement.
- **Why rank 2**: noisy perception currently produces supersession chains of
  near-identical values, polluting version history and inflating
  `list_scalar_state_views` reconciliation work. Deadband is the standard fix.
- **Caveat**: the `value|valid_at` signature already suppresses same-value
  same-anchor writes. Deadband extends that to *near*-value writes, which the
  signature cannot catch. Needed only when perception emits noisy numerics.

### 3. ErrorView — derived View over two Views

**Usefulness: high, but only after SetpointView exists.**

`setpoint − process variable`, folded over time. Cross-fade: a **derived**
View whose inputs are *two other Views* (SetpointView + ScalarStateView),
not raw episodes. Answers "how off-target has this been." This is the
handoff's "composable — a View can fold over facts that came from other
Views" claim made concrete.

- **Reducer**: signed difference, accumulated via `sum_` over the error
  stream. Or `latest` for instantaneous error.
- **Supersession**: a new ErrorView version is written whenever *either*
  input View supersedes.
- **Lawful?** Yes over the derived stream. The strain is on the *trigger*,
  not the fold: the supersession graph becomes a DAG, not a chain.
- **Cost**: medium. Requires a re-fold trigger when inputs supersede —
  infrastructure the current code does not have. Today every View is folded
  from raw episodes at ingest; ErrorView is folded from other Views at
  *supersession time*.
- **Why rank 3**: makes drift queryable as a stored answer, not a read-time
  computation. But it is the first View that forces the DAG-supersession
  question, so it's not free.

### 4. IntegralView — with anti-reset-windup

**Usefulness: medium.** The transfer that most directly encodes the
asymmetry principle.

The running sum of ErrorView. Answers "is the system cumulatively drifting
off-spec." Maps onto the handoff's asymmetry principle: an unbounded integral
*over-commits* to a wrong setpoint (windup), exactly like a wrong stored
aggregate misleads. Anti-reset-windup cross-fades to a **supersession cap**:
an IntegralView refuses to accumulate beyond a clamped magnitude.

- **Reducer**: `sum_` (non-idempotent — Law 2 applies, batch re-fold or
  `exclude_folded`).
- **Supersession**: append-only like Counter; no LWW.
- **Lawful?** Yes. `sum_` is a monoid.
- **Cost**: medium. Reuses counter machinery. The windup clamp is a new
  concept — a max-magnitude gate on the fold, applied at write time.
- **Why rank 4**: the windup clamp is the process-control cure for "a wrong
  stored aggregate actively misleads" (theory doc §2). It's the most honest
  transfer of the discipline's safety thinking, but it's narrow — only
  useful once ErrorView exists and only for signed-error accumulation.
- **Caveat**: depends on ErrorView, which depends on SetpointView. Three-
  deep dependency chain.

### 5. RemanenceView — leaky integrator

**Usefulness: medium.** Fills a query class the hard-supersede model cannot.

A ferromagnetic material retains magnetization after the field is removed.
Cross-fade: a View whose value decays *slowly* after its source events stop,
rather than vanishing. Answers "did Alice *used* to care about X" with
graded confidence, not a hard supersede. Reducer: a leaky integrator with a
long time constant — `value_new = max(0, value_old − λ·Δt + input)`.

- **Reducer**: **not a standard monoid.** Leaky integration is not
  associative in the presence of time gaps — `fold([a, b])` ≠
  `fold(fold([a]), b)` when wall-clock time elapses between the two calls.
- **Lawful?** Borderline. Order-sensitive (violates the commutative claim),
  and the decay depends on *wall clock at fold time*, not on event `when`.
- **Cost**: high. Requires a time-aware fold and a decay parameter per slot.
- **Why rank 5**: the query class is real — "stale but still somewhat true"
  is exactly what hard supersession throws away. But the law violation means
  it cannot use the existing fold path; it needs a dedicated reducer and a
  re-validation of the determinism contract.
- **Caveat**: this is what `lifecycle_service` sharpness decay already does
  at the *node* level. A RemanenceView would push that into the *value*
  level. Likely redundant with lifecycle, not additive.

### 6. CascadeView — composition shape

**Usefulness: medium-low.** Same DAG question as ErrorView, less query value.

An outer loop's output is the inner loop's setpoint. Cross-fade: a View whose
setpoint is *another View's* current value. Models "Alice's high-level goal
(lose weight) drives her tactical preference (gym over drinks)."

- **Reducer**: `latest` over the upstream View's supersession events.
- **Lawful?** Yes, but only if the upstream View is treated as an event
  source — which collapses CascadeView into "a SetpointView whose input is a
  View instead of an episode."
- **Cost**: medium. Mostly a wiring change, not a new kind.
- **Why rank 6**: once SetpointView accepts View-derived inputs (which
  ErrorView already requires), CascadeView is not a separate kind — it's a
  usage pattern of SetpointView. Listing it separately overstates the work.

### 7. DisturbanceView — filtered timeline

**Usefulness: low.** Requires perception it doesn't have.

An exogenous input that perturbs the process variable, distinct from control
action. Cross-fade: a specialized Timeline that filters for events tagged
`external` (environment changed, not the user acted). Answers "what knocked
X off its trajectory" — separable from "what did the user do."

- **Reducer**: `timeline` with a kind filter.
- **Lawful?** Yes — `timeline` is already idempotent and lawful.
- **Cost**: low in the fold; high in perception. Requires the perception
  pass to tag exogeneity, which it currently does not.
- **Why rank 7**: lawful and cheap to fold, but blocked on a perception
  change that has no other motivation. Not worth it until exogeneity tagging
  is needed for its own sake.

### 8. DisturbanceFeedforwardView — counterexample

**Usefulness: none as a View.** Useful as a boundary test.

A controller applies a correction from a *predicted* disturbance before the
PV reacts. Cross-fade: a View whose value is a *projection* from a leading
indicator, not a lagging measurement.

- **Lawful?** **No.** Feedforward admits prediction, which is probabilistic,
  and the theory doc (§3) forbids probability in the fold.
- **Cost**: N/A — should not be built as a View.
- **Why rank 8**: its existence in the source discipline tells you what the
  abstraction cannot do. Predictive state belongs at read time, computed
  from a TimelineView + an external model, never stored as a folded View.
  Keep this in the doc as the counterexample that validates the law.

### 9. DerivativeView — counterexample

**Usefulness: none as a View.** Useful as a boundary test.

`d(measured)/dt`. Answers "how fast is X changing right now."

- **Lawful?** **No.** Derivative is not associative, not a monoid, has no
  identity. A `fold([a, b, c])` for a derivative is undefined — you need
  exactly two points and an ordering.
- **Cost**: N/A — should not be built as a View.
- **Why rank 9**: this is the cleanest illustration that some query classes
  belong at read time as a δ over a TimelineView, exactly where
  `datediff_days` already lives in `fold_algebra.py:222`. A DerivativeView
  would duplicate that and break the fold contract. Keep as the canonical
  "this is why we have read-time δ" example.

---

## What the transfer teaches

The lawful transfers (Setpoint, Deadband, Error, Integral, Remanence,
Cascade, Disturbance) all share one property: their reducer is a monoid over
*observed* events. The two that break (Derivative, Feedforward) are the ones
that reach *outside* the event stream — into rates-of-change or into the
future.

The single most useful transfer is **SetpointView**, because it exposes a
gap in the current design: ScalarState answers "what is X" but has no
partner for "what should X be." Without the pair, drift is uncomputable as a
View, and drift is the query class the system most needs. Build that first;
the rest of the lawful transfers either depend on it (Error, Integral,
Cascade) or are independent and lower-value (Remanence, Disturbance).

## Dependency order if any of this is ever built

```
SetpointView (keystone)
  ├── DeadbandView (independent, cheap, do anytime)
  ├── ErrorView (needs Setpoint)
  │     └── IntegralView (needs Error, needs anti-windup)
  ├── CascadeView (collapses into Setpoint once Setpoint accepts View inputs)
  ├── RemanenceView (independent, but likely redundant with lifecycle decay)
  └── DisturbanceView (blocked on perception exogeneity tagging)

DerivativeView, FeedforwardView — do not build. Read-time δ only.
```

## Open questions

1. Does the supersession graph need to become a DAG to support View-over-
   View folds (ErrorView, CascadeView)? Today it's a chain
   (`SUPERSEDES` only links versions of the same `view_key`). A View that
   folds from another View's supersession events needs a cross-key edge.
2. Is RemanenceView actually just `lifecycle_service` sharpness decay
   pushed into the value? If so, it's a duplicate, not a new kind.
3. Does SetpointView need its own `value_kind` allowlist, or does it reuse
   `ScalarStateKind.VALUE_KINDS`? Intent and measurement probably share
   types (a budget setpoint is money; a budget actual is money).
