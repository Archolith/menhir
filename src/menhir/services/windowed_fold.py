"""σ WINDOW — the READ-TIME half of Lever A: windowed counts over a lossless timeline View.

Design of record: `.agent/plans/perception-window-and-triangulation.md` (Lever A) and the fold-algebra
WINDOW note. The binding invariant: **a relative window ("last month") is NEVER materialized into a
View** — its meaning drifts with time. So the write side stores a lossless, dated TIMELINE
(`event_fold.fold_events_to_timeline`), and a query like "how many X did I acquire in the last month?"
is answered HERE as a pure read-time δ: resolve the relative phrase to absolute bounds against the
QUERY time, σ `window` the timeline, ρ `count`/`distinct_count`. No LLM, no graph — pure arithmetic
over the payload `fetch_timeline` returns, reusing the deterministic `fold_algebra` vocabulary.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from menhir.domain.fold_algebra import Event, count, distinct_count, window

_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _sub_months(d: date, n: int) -> date:
    """d minus n calendar months, clamping the day to the target month's length."""
    m0 = d.month - 1 - n
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    dim = 29 if (m == 2 and _leap(y)) else _DAYS_IN_MONTH[m - 1]
    return date(y, m, min(d.day, dim))


def resolve_window(phrase: str, reference_date: str, *, calendar: bool = False
                   ) -> tuple[str | None, str | None]:
    """Resolve a relative time phrase to absolute ISO `(since, until)` against `reference_date` — the
    QUERY time, never now-at-write (that is what keeps the window out of the stored View). Handles
    'last/past/previous N day|week|month|year(s)' and 'this month|year'. `until` is the reference date;
    unrecognized phrases return `(None, None)` (an unbounded window = count everything).

    `calendar` picks the month/year sense of "last": ROLLING (default) counts back N periods from the
    exact reference date ("last month" = ref−30ish days); CALENDAR snaps `since` to the period start
    ("last month" = the 1st of the prior calendar month). Natural language is usually the calendar
    sense — a user who says "acquired in the last month" means the loose recent span, not a precise
    30-day cutoff (D0 `3a704032`: an item dated 5 days before the rolling boundary counts under
    calendar, matching gold). Default stays rolling; callers opt into the looser reading."""
    ref = date.fromisoformat(str(reference_date)[:10])
    p = (phrase or "").lower().strip()
    until = ref.isoformat()

    m = re.search(r"(?:last|past|previous)\s+(?:(\d+)\s+)?(day|week|month|year)s?", p)
    if m:
        n = int(m.group(1) or 1)
        unit = m.group(2)
        if unit == "day":
            since = ref - timedelta(days=n)
        elif unit == "week":
            since = ref - timedelta(weeks=n)
        elif unit == "month":
            since = _sub_months(ref, n)
            if calendar:
                since = since.replace(day=1)
        else:
            since = _sub_months(ref, 12 * n)
            if calendar:
                since = since.replace(month=1, day=1)
        return since.isoformat(), until
    if "this month" in p:
        return ref.replace(day=1).isoformat(), until
    if "this year" in p:
        return ref.replace(month=1, day=1).isoformat(), until
    return None, None


def _to_events(entries: list[dict[str, Any]]) -> list[Event]:
    """Timeline entries (`{when, what, episode_uuid}`) -> `Event`s the algebra can σ/ρ. The item name
    lives in `what` (put there by the write side); it becomes the DISTINCT identity for read counts."""
    return [Event(when=e.get("when"), kind="entry", identity=e.get("what"),
                  what=e.get("what"), episode_uuid=e.get("episode_uuid"))
            for e in entries if e.get("when")]


def count_in_window(
    entries: list[dict[str, Any]], *, since: str | None = None, until: str | None = None,
    distinct: bool = True,
) -> int:
    """Read-time δ: how many timeline entries fall in `[since, until]` (inclusive, bounds optional).
    `distinct` (default) counts distinct items (dedup by `what`) — the right answer for "how many X",
    where the same item mentioned twice must not double-count; `distinct=False` tallies occurrences."""
    win = window(_to_events(entries), since=since, until=until)
    return distinct_count(win) if distinct else count(win)


def count_in_relative_window(
    entries: list[dict[str, Any]], phrase: str, reference_date: str, *, distinct: bool = True,
) -> int:
    """Convenience: resolve a relative `phrase` against `reference_date`, then `count_in_window`.
    This is the shape a recall-time router (deferred A6) would call for "how many X in <window>"."""
    since, until = resolve_window(phrase, reference_date)
    return count_in_window(entries, since=since, until=until, distinct=distinct)
