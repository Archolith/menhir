"""Grounded event dating (#155): resolve the valid time of an occurrence from the source sentence
that states it. Shared by read-time occurrence event timing (``occurrence_event_time``).

Extracted verbatim from the archived event-history perception module so the counting lane keeps its
deterministic date grounding.
"""

from __future__ import annotations

import re
from datetime import timedelta

from menhir.domain.temporal import parse_iso8601
from menhir.services.typed_scalar_rules import (
    MONTHS,
    SRC_MONTH_DAY_RE,
    parse_source_date,
    same_calendar_day,
)

#: Sentence terminators that delimit the grounding-context sentence (#155).
_SENTENCE_BREAKS = ".!?\n"


def _sentence_context(content: str, span_start: int, span_end: int) -> str:
    """The sentence of ``content`` containing the located span (#155): from the last ``.``/``!``/``?``
    /newline before ``span_start`` to the first one at/after ``span_end``, inclusive of the span;
    whitespace-trimmed. Deterministic and span-local, so a date stated in a DIFFERENT sentence of the
    same episode is never attributed to this claim."""
    if not content:
        return ""
    start = 0
    for i in range(span_start - 1, -1, -1):
        if content[i] in _SENTENCE_BREAKS:
            start = i + 1
            break
    end = len(content)
    for i in range(span_end, len(content)):
        if content[i] in _SENTENCE_BREAKS:
            end = i + 1
            break
    return content[start:end].strip()


#: Supported relative date phrases for grounded event resolution (#155). Word counts one..ten only;
#: anything else ("last week", "recently") is deliberately unsupported -> no relative grounding.
_RELATIVE_WORD_NUMBERS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_RELATIVE_RE = re.compile(
    r"\b(?:(?P<days_num>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+days?\s+ago|"
    r"(?P<weeks_num>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+weeks?\s+ago|"
    r"(?P<today>today|tonight)|(?P<yesterday>yesterday)\b)",
    re.IGNORECASE,
)


def _relative_days_ago(text: str) -> int | None:
    """Days before the reference date stated by a supported relative phrase, or None when the text
    carries none. ``today``/``tonight`` -> 0, ``yesterday`` -> 1, ``N day(s) ago`` -> N,
    ``N week(s) ago`` -> 7*N. N is a digit 1-60 or a word one..ten."""
    m = _RELATIVE_RE.search(text or "")
    if m is None:
        return None
    if m.group("today"):
        return 0
    if m.group("yesterday"):
        return 1
    raw = m.group("days_num") or m.group("weeks_num") or ""
    count = _RELATIVE_WORD_NUMBERS.get(raw.lower())
    if count is None:
        count = int(raw)
        if not 1 <= count <= 60:
            return None
    return count * 7 if m.group("weeks_num") else count


def _stated_month_day(text: str) -> "tuple[int, int] | None":
    """The (month, day) the text states as a specific date with NO year, reusing the scalar side's
    month/day pattern so the two resolvers cannot drift; None when none is stated."""
    m = SRC_MONTH_DAY_RE.search(text or "")
    if m is None:
        return None
    parts = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})", m.group(0))
    if parts is None:
        return None
    month = MONTHS.get(parts.group(1)[:3].lower())
    if month is None:
        return None
    return month, int(parts.group(2))


def _resolve_event_valid_time(
    when: str | None, when_context: str, episode_reference_time: str | None,
) -> "tuple[str | None, str | None, str | None]":
    """Grounded (valid_at, time_basis, note) resolution (#155), precision-first and NEVER using ingest
    time. ``when_context`` is the source sentence containing the claim span; all comparisons are by
    calendar date in the episode reference's timezone. Rules, in order:

      a. ``parse_source_date(when_context, ...)`` resolves a fully-explicit source date;
      b. a full source date ON OR BEFORE the reference date (more than 1 day after the reference is
         future; 1 day is timezone tolerance) wins -> (that date, 'explicit'), noting
         ``when_conflict_used_source`` when the model gave a different day. A source date AFTER the
         reference cannot be a completed acquisition -> treated as no source date, note
         ``future_source_date_ignored``;
      c. a month+day with no year: the stated month/day (extracted deterministically from
         ``when_context``) is used when the model's ``when`` is absent OR carries the SAME month/day,
         in the most recent year on or before the reference date -> 'explicit'; otherwise fall back,
         note ``undated_month_day_unmatched``;
      d. a supported relative phrase (today/tonight, yesterday, N day(s)/week(s) ago) computes the
         date deterministically from the reference -> that date, 'explicit', noting
         ``when_conflict_used_relative`` when the model gave a different day (a blank or matching
         model ``when`` notes None);
      e. no stated date: any model ``when`` is UNgrounded and ignored (note
         ``ungrounded_when_ignored``) and we fall back.

    Fallback is the prior behavior: the episode reference time -> ('episode_reference'); an
    unparseable reference -> (None, None): abstention. ``learned_at`` is deliberately NOT a fallback —
    event authority orders by world/source time only, and an ungrounded ingest stamp must never
    masquerade as an occurrence time. The scalar side's expiration/temporal-disposition policy is NOT
    copied here; only its deterministic date parsing is reused.
    """
    note: str | None = None
    ref = parse_iso8601(episode_reference_time)
    ref_date = ref.date() if ref else None

    def _fallback() -> "tuple[str | None, str | None, str | None]":
        if when and note is None:
            return_note: str | None = "ungrounded_when_ignored"
        else:
            return_note = note
        if episode_reference_time and ref is not None:
            return episode_reference_time, "episode_reference", return_note
        return None, None, return_note

    src, unresolvable = parse_source_date(when_context, episode_reference_time)
    if src is not None:
        src_dt = parse_iso8601(src)
        # #155 K2: more than 1 day after the reference is future (1 day is timezone tolerance)
        if (
            src_dt is not None and ref_date is not None
            and src_dt.date() > ref_date + timedelta(days=1)
        ):
            note = "future_source_date_ignored"   # future dates cannot be completed acquisitions
        else:
            conflict = bool(when) and not same_calendar_day(when, src)
            return src, "explicit", ("when_conflict_used_source" if conflict else None)

    when_dt = parse_iso8601(when) if when else None
    if unresolvable:
        stated = _stated_month_day(when_context)
        if (
            ref is not None and ref_date is not None and stated is not None
            and (when_dt is None or (when_dt.month, when_dt.day) == stated)
        ):
            candidate = None
            base = when_dt or ref.replace(hour=0, minute=0, second=0, microsecond=0)
            for year in (ref.year, ref.year - 1):
                try:
                    cand = base.replace(year=year, month=stated[0], day=stated[1])
                except ValueError:
                    break  # e.g. Feb 29 in a non-leap year: not resolvable
                if cand.date() <= ref_date:
                    candidate = cand
                    break
            if candidate is not None:
                return candidate.isoformat(), "explicit", None
        if note is None:
            note = "undated_month_day_unmatched"
        return _fallback()

    days_ago = _relative_days_ago(when_context)
    if days_ago is not None and ref is not None:
        computed = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        computed = (computed - timedelta(days=days_ago)).isoformat()
        if not when or same_calendar_day(when, computed):
            return computed, "explicit", None
        return computed, "explicit", "when_conflict_used_relative"

    return _fallback()


#: Public aliases of the #155 grounding helpers, shared with read-time occurrence event timing.
sentence_context = _sentence_context
resolve_event_valid_time = _resolve_event_valid_time
