"""A6 — recall-time windowed-count resolver: answer "how many X (in <window>)?" from a timeline View.

The read-time entry point for Lever A (`windowed_fold` is the pure δ; this binds it to the graph).
Given a natural-language count query and a namespace, it detects windowed-count INTENT, finds the
matching acquisition timeline View, resolves the query's relative window against the query date, and
returns the deterministic count over the timeline payload — the whole point of storing a lossless
timeline instead of a baked-in counter.

Scope (per the plan): this is the standalone capability + its intent detection, provable end-to-end
against the graph. Whether a synthesized count should be INJECTED into `RecallService.recall`'s ranked
results (and out-rank retrieved nodes) is a separate product decision, left as a documented seam —
a caller invokes `answer_windowed_count` and decides how to present the answer.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from menhir.services.windowed_fold import count_in_window, resolve_window

# "how many <noun> did/do/have i ..." — the count-intent stem; the noun is matched to a timeline subject.
_COUNT_INTENT = re.compile(r"how many\s+(?P<noun>[a-z][\w\s'-]*?)\s+(?:did|do|have|are|is)\b", re.I)
_WINDOW_PHRASE = re.compile(
    r"(?:last|past|previous)\s+(?:\d+\s+)?(?:day|week|month|year)s?|this\s+(?:month|year)", re.I)
_STOP = {"the", "a", "an", "my", "of", "in", "on", "i", "own", "have", "currently", "did", "acquire"}


class _TimelineViews(Protocol):
    def list_views(self, *, kind: str | None = None, namespace: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]: ...
    def fetch_timeline(self, *, subject: str, namespace: str | None = None) -> dict[str, Any] | None: ...


def detect_windowed_count(query: str) -> dict[str, Any] | None:
    """Parse a count query into `{noun, window}` or None. `window` is the relative phrase if present
    (else None = all-time count). Deterministic; a non-count query returns None (no routing)."""
    m = _COUNT_INTENT.search(query or "")
    if not m:
        return None
    w = _WINDOW_PHRASE.search(query.lower())
    return {"noun": m.group("noun").strip().lower(), "window": w.group(0) if w else None}


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[\s_]+", (s or "").lower()) if t and t not in _STOP}


def _best_subject(noun: str, subjects: list[str]) -> str | None:
    """Match the query noun to a timeline subject by token overlap (e.g. 'plants' -> 'plants_acquired').
    Singular/plural tolerated crudely (trailing 's'). Returns the best-overlap subject, or None."""
    want = _tokens(noun)
    want |= {t[:-1] for t in want if t.endswith("s")} | {t + "s" for t in want}
    best, best_score = None, 0
    for subj in subjects:
        score = len(want & _tokens(subj))
        if score > best_score:
            best, best_score = subj, score
    return best if best_score > 0 else None


def answer_windowed_count(
    views: _TimelineViews, *, namespace: str, query: str, reference_date: str,
    calendar: bool = True, distinct: bool = True,
) -> dict[str, Any] | None:
    """End-to-end: intent -> matching timeline View -> windowed δ. Returns
    `{subject, count, window, since, until, timeline_uuid, n_entries}` or None when the query isn't a
    windowed count or no timeline matches (caller then falls through to normal recall). `calendar`
    defaults to the natural-language sense of "last month" (see `resolve_window`)."""
    intent = detect_windowed_count(query)
    if intent is None:
        return None
    timelines = views.list_views(kind="timeline", namespace=namespace)
    subject = _best_subject(intent["noun"], [t.get("subject") for t in timelines if t.get("subject")])
    if subject is None:
        return None
    tl = views.fetch_timeline(subject=subject, namespace=namespace)
    if not tl:
        return None
    entries = tl.get("entries") or []
    since = until = None
    if intent["window"]:
        since, until = resolve_window(intent["window"], reference_date, calendar=calendar)
    return {
        "subject": subject, "window": intent["window"], "since": since, "until": until,
        "count": count_in_window(entries, since=since, until=until, distinct=distinct),
        "timeline_uuid": tl.get("uuid"), "n_entries": len(entries),
    }
