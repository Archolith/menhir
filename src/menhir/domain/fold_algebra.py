"""Fold Algebra — the small deterministic vocabulary behind Event -> Fold -> View.

Design of record: `.agent/plans/fold-algebra.md`. The minimal-fold-algebra question collapsed to a
three-stage pipeline whose middle stage is FOUR monoid reducers:

    View  =  δ? ∘ ρ ∘ σ?        over the keyed event stream for one view_key
             │     │    └ SHAPE  : [Event] -> [Event]   filter / window  (WINDOW)
             │     └── REDUCE     : [Event] -> Accumulator                (the four monoids)
             └──────── DERIVE     : Accumulator -> Answer   pure scalar arithmetic (DATEDIFF, ‖·‖, DELTA)

This module is ρ + δ + σ as PURE functions — no LLM, no graph. The LLM's only role is producing
typed `Event`s at the boundary (perception); nothing here calls a model. Every reducer is a monoid
(associative combine + identity), which is what makes a fold deterministic, order-insensitive where
it should be, unit-testable, and legally evaluable in batch OR incrementally.

Which laws bind which reducer (see `REDUCERS`):
  - SUM/COUNT are NON-idempotent -> replay double-counts. Two lawful evaluations (Law 2):
    (a) BATCH re-fold — recompute over the full event set, store the ABSOLUTE value; replay-safe by
        construction (a re-run yields the same total). The simpler, always-correct default.
    (b) INCREMENTAL — accumulate only new events, using the View's already-folded episode set as the
        ledger: `exclude_folded(events, already)` drops events whose episode already contributed.
  - EXTREME/SET/LIST are idempotent -> replay-safe either way; may reconcile freely.
  - EXTREME(when) is the LWW register; its ordering law (compare world time, never arrival) lives in
    `ViewRepository` (`lww_register`), the stateful half of the same monoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class Event:
    """A typed, dated, provenance-bearing record — perception's output at the Event boundary.

    `when` is world time (ISO date/datetime). `identity` is the dedup/aggregation key for SET and
    LIST (e.g. a normalized item name). `category` is a coarser aggregation key — the theme a total is
    asked about (e.g. 'biking' over a helmet + lights + chain), a stable per-item tag that lets a
    HETEROGENEOUS group fold to one measure where the item name alone can't. `episode_uuid` is the
    provenance / Law-2 replay ledger key.
    """

    when: str
    kind: str
    value: float | None = None
    identity: str | None = None
    what: str | None = None
    episode_uuid: str | None = None
    category: str | None = None


# ---------------------------------------------------------------------------- σ  SHAPE

def window(events: Iterable[Event], *, since: str | None = None, until: str | None = None
           ) -> list[Event]:
    """Keep events whose `when` falls in [since, until] (inclusive, either bound optional).
    RELATIVE windows ("last month") must be resolved to absolute bounds by the caller and never
    materialized into a View — see the design's WINDOW note."""
    lo, hi = _parse(since), _parse(until)
    out = []
    for e in events:
        w = _parse(e.when)
        if w is None:
            continue
        if lo is not None and w < lo:
            continue
        if hi is not None and w > hi:
            continue
        out.append(e)
    return out


def exclude_folded(events: Iterable[Event], already_folded: set[str]) -> list[Event]:
    """Law-2 INCREMENTAL guard: drop events whose `episode_uuid` already contributed to the View
    (its MENTIONS set is the ledger), so a non-idempotent reducer accumulates without double-counting
    across runs. NOT needed for batch re-fold — recomputing the absolute value from the full set is
    the simpler always-correct path. Distinct events *within* one episode are preserved (dedup is by
    episode-already-folded, not by collapsing an episode's own events)."""
    return [e for e in events if e.episode_uuid is None or e.episode_uuid not in already_folded]


def _event_signature(e: Event) -> tuple[str, str, str]:
    """Coreference key for the CERTAIN case only: what makes two events unmistakably the SAME
    real-world occurrence — `kind`, the value (to the cent, or '' if none), the day, and the item's
    normalized WORDING (`identity` else `what`). We deliberately do NOT key on `category`: category is
    a coarse theme the extractor tags on every item, so keying the merge on it silently collapses two
    genuinely-distinct same-day, same-value items ($40 lights + $40 pump, both 'biking') into one — a
    directional wrong-low commit with no judge involved, which the design (§4d) forbids: safety is
    ABSTENTION, not an undercount bet. Same-kind/value/day plus identical wording is the certain
    re-narration and still merges here; the ambiguous same-category/value/day-but-different-wording
    case is routed to `coreference_candidates` -> the semantic judge instead."""
    item = (e.identity or e.what or "").strip().lower()
    val = f"{float(e.value):.2f}" if e.value is not None else ""
    day = str(e.when)[:10].replace("/", "-") if e.when else ""
    return (f"{e.kind}|{val}|{item}", day, "")


def dedup_events(events: Iterable[Event], *, key: "Callable[[Event], Any]" = _event_signature
                 ) -> list[Event]:
    """Law-2 CROSS-EPISODE guard: collapse events that describe ONE real-world occurrence narrated in
    several episodes (e.g. "bike lights, $40" mentioned in two different episodes → one $40, not $80)
    to a single representative. The default `key` is `_event_signature` (kind + value + item + day):
    events matching it are treated as the same occurrence. This is what `exclude_folded` (dedup by
    *episode already folded*) cannot catch — one event, two episodes, both in the SAME batch.

    Bias is PRECISION-FIRST FOR A SUM: when two mentions might be one occurrence, merging (undercount)
    is safer than the confident over-count that pollutes a total (FP ≫ FN, and a wrong-high current
    total out-ranks truth). Note the asymmetry vs DISTINCT, whose precision-first bias is to keep items
    SEPARATE — because there over-merging under-counts. Order-preserving (keeps the first mention).
    Events with the same signature but genuinely distinct occurrences (retries of an identical action)
    are the accepted residual — separate them by giving them distinct `identity`/`when` upstream."""
    seen: set[Any] = set()
    out: list[Event] = []
    for e in events:
        k = key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def coreference_candidates(events: Iterable[Event]) -> list[list[Event]]:
    """Deterministic CANDIDATE generation for coreference (Lever C3, the read side of the 'determinism
    finds candidates, the LLM judges, confidence gates' design). Returns clusters of events that look
    like they COULD be one purchase re-narrated more than once but that determinism CANNOT safely
    resolve on its own — the population handed to a semantic judge. A cluster is a set sharing a
    coreference group (`category` else `identity`) AND the same value (to the cent) with ≥2 members,
    flagged when EITHER:
      * ≥2 DISTINCT days — the cross-date case (same-purchase-re-narrated vs a recurring $5 coffee), OR
      * the SAME day but ≥2 DISTINCT wordings (`identity` else `what`) — the case the narrowed
        `_event_signature` deliberately no longer merges (two $40 'biking' items the same day might be
        one purchase re-quoted, or two distinct items; determinism can't tell, so the judge decides).
    Same-day IDENTICAL-wording dups are NOT candidates — exact `_event_signature`/`dedup_events` owns
    that certain merge. Singletons and single-day/single-wording groups are not candidates (nothing
    ambiguous to resolve). Order-preserving within and across clusters."""
    groups: dict[tuple[str, str], list[Event]] = {}
    order: list[tuple[str, str]] = []
    for e in events:
        if e.value is None:
            continue
        item = (e.category or e.identity or "").strip().lower()
        if not item:
            continue
        gkey = (item, f"{float(e.value):.2f}")
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append(e)
    out: list[list[Event]] = []
    for gkey in order:
        evs = groups[gkey]
        if len(evs) < 2:
            continue
        days = {str(e.when)[:10].replace("/", "-") for e in evs if e.when}
        wordings = {(e.identity or e.what or "").strip().lower() for e in evs}
        if len(days) >= 2 or len(wordings) >= 2:
            out.append(evs)
    return out


# ---------------------------------------------------------------------------- ρ  REDUCE (4 monoids)

def sum_(events: Iterable[Event]) -> float:
    """SUM monoid — identity 0, combine +. NON-idempotent (Law 2). Ignores None values."""
    return float(sum(float(e.value) for e in events if e.value is not None))


def count(events: Iterable[Event]) -> int:
    """COUNT = SUM ∘ map(1). NON-idempotent (Law 2)."""
    return sum(1 for _ in events)


def extreme(events: Iterable[Event], *, key: Callable[[Event], Any] = lambda e: e.when,
            direction: str = "max") -> Event | None:
    """EXTREME(key, dir) monoid — identity ⊥ (None), combine = pick by key. Idempotent.
    Subsumes MIN/MAX(value) and — with key=when, dir=max — LATEST/CURRENT (the LWW register)."""
    best: Event | None = None
    best_k: Any = None
    for e in events:
        k = key(e)
        if k is None:
            continue
        if best is None or (k > best_k if direction == "max" else k < best_k):
            best, best_k = e, k
    return best


def latest(events: Iterable[Event]) -> Event | None:
    """EXTREME(when, max) — the last-writer-wins register. Ordering law enforced at the View layer."""
    return extreme(events, key=lambda e: _parse(e.when) or _MIN, direction="max")


def distinct(events: Iterable[Event]) -> set[str]:
    """SET(identity) monoid — identity ∅, combine ∪. Idempotent. Events without identity are dropped."""
    return {e.identity for e in events if e.identity is not None}


def distinct_count(events: Iterable[Event]) -> int:
    """δ‖·‖ ∘ SET — the dedup-count answer (e.g. '3 fish tanks' from separately-mentioned items)."""
    return len(distinct(events))


def timeline(events: Iterable[Event]) -> list[dict[str, Any]]:
    """LIST — the free monoid: identity [], combine merge-sort ∪, dedup by (when, identity|what).
    Lossless; any unanticipated query is a read-time δ over this. Sorted ascending by `when`."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in sorted(events, key=lambda x: (str(x.when), str(x.what or ""))):
        ident = (str(e.when), str(e.identity if e.identity is not None else (e.what or "")))
        if ident in seen:
            continue
        seen.add(ident)
        out.append({"when": e.when, "what": e.what, "episode_uuid": e.episode_uuid})
    return out


# ---------------------------------------------------------------------------- δ  DERIVE (pure arith)

def datediff_days(events: Iterable[Event]) -> int:
    """LIST span in days: last.when − first.when. δ over ρ output; never touches raw episodes."""
    ds = sorted(w for w in (_parse(e.when) for e in events) if w is not None)
    if len(ds) < 2:
        return 0
    return (ds[-1] - ds[0]).days


def delta(current: float | None, previous: float | None) -> float | None:
    """current − previous over the supersession chain (read-time; the version history IS the input)."""
    if current is None or previous is None:
        return None
    return float(current) - float(previous)


# ---------------------------------------------------------------------------- registry (laws)

#: name -> (reducer, idempotent). Idempotent reducers are replay-safe; non-idempotent ones must
#: re-fold or dedup on provenance (Law 2). Lets a fold declare which reducer it is, hence its laws.
REDUCERS: dict[str, tuple[Callable[..., Any], bool]] = {
    "sum": (sum_, False),
    "count": (count, False),
    "distinct_count": (distinct_count, True),
    "latest": (latest, True),
    "timeline": (timeline, True),
}


# ---------------------------------------------------------------------------- helpers

_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _parse(s: Any) -> datetime | None:
    if not s:
        return None
    # tolerate slash dates (2023/05/07) — common from source data / LLM echoes of them — so a
    # windowed fold never silently drops an event whose only sin is a "/" separator.
    t = str(s).strip().replace("Z", "+00:00").replace("/", "-")
    try:
        d = datetime.fromisoformat(t)
    except Exception:
        try:
            d = datetime.fromisoformat(t[:10])  # date-only fallback
        except Exception:
            return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
