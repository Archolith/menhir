"""Deterministic temporal resolution (when-discipline) for the typed-scalar boundary.

Span-local past/current/bounded framing detection and fully-explicit source-date parsing; decides
drop/keep and whether a source-derived timestamp may be trusted. Moved verbatim from
``typed_scalar_rules``; the facade module re-exports every name so existing import sites keep
working unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# --- deterministic temporal resolution (when-discipline) ------------------------------------------
# Target: no temporal value may affect LWW ordering unless its timing is source-grounded and
# deterministically resolved. The model's `when` is ADVISORY, not authoritative. This span-local
# resolver runs at EXTRACTION (pre-gate, like hedged-abstention), so a past-only/unresolvable claim
# never votes and a hallucinated date never reaches valid_at. It decides ONLY drop/keep + whether to
# TRUST the model date; the concrete valid_at value is still assigned at bind by `_resolve_valid_time`
# (episode reference when the date is not trusted). All k samples of one claim decide identically.
#
# RELATIVE future/date ARITHMETIC ("next Monday" -> an exact date from the episode reference) is
# deliberately NOT done here -- getting it subtly wrong is worse than abstaining -- so a relative future
# with no explicit boundary ABSTAINS (drops), matching the owner spec's "abstain unless an exact
# boundary is stated". That resolution is a scoped follow-on.

#: RELATIVE future framing with no exact boundary -> abstain (drop). Narrow on purpose (over-detecting
#: future would drop good CURRENT data); the as_of fold filter is the real future-safety, so a missed
#: future cue is harmless.
_FUTURE_REL_RE = re.compile(
    r"\b(?:starting|beginning|as of)\s+(?:next|this)\b|\bnext\s+(?:week|month|year|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
#: clearly-PAST framing -> a past-only observation, NOT a current standing value.
_PAST_RE = re.compile(
    r"\b(?:yesterday|formerly|previously|back\s+then)\b|\ba\s+while\s+ago\b|"
    r"\blast\s+(?:week|month|year|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago\b", re.I)
#: CURRENT framing -> the value is current even next to a past clause ("...; now I have 37").
_CURRENT_RE = re.compile(
    r"\b(?:now|currently|nowadays|today|right\s+now|these\s+days|at\s+the\s+moment|as\s+of\s+today)\b",
    re.I)
#: BOUNDED / temporary framing -> the value holds only for a limited window and must NOT become an
#: unconditional current View. Deterministic durable bounds (`valid_until`) are Phase C (deferred), so
#: until then a bounded value ABSTAINS (drops), per the owner spec.
_BOUNDED_RE = re.compile(
    r"\bfor\s+(?:this|the|now|today|tonight|the\s+time\s+being)\b|\bfor\s+the\s+(?:day|week|month)\b|"
    r"\buntil\s+\w+|\bthrough\s+(?:friday|monday|tuesday|wednesday|thursday|saturday|sunday|next)\b|"
    r"\bthis\s+week\s+only\b|\btemporarily\b|\bfor\s+now\b", re.I)
#: months for deterministic date parsing (3-letter prefix key).
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
#: FULLY-EXPLICIT calendar dates the resolver can DETERMINISTICALLY parse -> the persisted timestamp is
#: derived from the SOURCE, never the model. Narrow on purpose: ISO `YYYY-MM-DD` and `Month D, YYYY`.
_SRC_ISO_RE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
_SRC_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})\b", re.I)
#: a SPECIFIC month+day with NO year -> a specific date claim we will NOT silently year-infer (owner
#: spec: "Month/day without a resolvable year abstains"). A bare weekday is NOT matched here -> it is
#: treated as "no resolvable date" (a recurrence/scope), so it never authorizes a model date.
_SRC_MONTH_DAY_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b",
    re.I)


def _to_dt(s: Any) -> "datetime | None":
    """Lenient ISO parse (tolerates 'Z'); None on failure/absence."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_source_date(span: str, episode_reference: str | None) -> tuple[str | None, bool]:
    """Deterministically resolve a FULLY-EXPLICIT calendar date from the span. Returns
    ``(iso_or_None, specific_but_unresolvable)``:
      * a parsed ISO timestamp (date at 00:00 in the episode-reference timezone, else UTC) when the span
        carries `YYYY-MM-DD` or `Month D, YYYY`;
      * ``(None, True)`` when it carries a SPECIFIC month+day with no year (a dated claim we refuse to
        year-infer -> the caller abstains);
      * ``(None, False)`` when there is no resolvable explicit date (dateless, weekday-only, or a
        year-only phrase we do not back-date here)."""
    s = (span or "").strip()
    if not s:
        return (None, False)
    ref = _to_dt(episode_reference)
    tz = ref.tzinfo if (ref and ref.tzinfo) else timezone.utc
    m = _SRC_ISO_RE.search(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return (datetime(y, mo, d, tzinfo=tz).isoformat(), False)
        except ValueError:
            return (None, True)   # e.g. 2026-02-30 -> a stated-but-impossible date -> abstain
    m = _SRC_MONTH_DAY_YEAR_RE.search(s)
    if m:
        mo = _MONTHS.get(m.group(1)[:3].lower())
        if mo:
            try:
                return (datetime(int(m.group(3)), mo, int(m.group(2)), tzinfo=tz).isoformat(), False)
            except ValueError:
                return (None, True)
    if _SRC_MONTH_DAY_RE.search(s):
        return (None, True)   # specific month+day, no year -> abstain (no year inference)
    return (None, False)


def _same_calendar_day(a: str | None, b: str | None) -> bool:
    da, db = _to_dt(a), _to_dt(b)
    return da is not None and db is not None and da.date() == db.date()


def resolve_temporal_disposition(
    stated_span: str, model_when: str | None, operation: str,
    episode_reference: str | None = None,
) -> tuple[str, bool, str | None]:
    """Deterministically classify a proposal's temporal grounding from its span. Returns
    ``(disposition, keep, trusted_when)``:
      * ``keep=False`` -> DROP the proposal (it must not vote / persist);
      * ``trusted_when`` -> the DETERMINISTIC source-derived timestamp to persist, or None to fall to the
        episode reference. The model's `when` is a CROSS-CHECK ONLY -- it is NEVER the persisted value.

    Applies to every value/timing-affecting operation (``absolute``, ``delta``, AND ``expire`` -- an
    expire's timing is authoritative fold input, so a hallucinated date on it must not alter expiry
    ordering). Dispositions:
      * ``bounded_unsupported``  temporary window ("for this week", "until Friday") -> DROP (abstain
                                 until durable `valid_until`, Phase C);
      * ``past_only``            clearly-past framing, no current cue -> DROP (not a current value);
      * ``explicit_time``        a FULLY-EXPLICIT source date resolves deterministically -> persist THAT
                                 date. If the model gave a date that disagrees at day granularity -> DROP;
      * ``date_conflict``        (a sub-case of the above) model date contradicts the source date -> DROP;
      * ``unresolved_date``      a SPECIFIC dated claim (month+day, no year) we refuse to infer -> DROP;
      * ``future_unresolved``    relative future, no explicit boundary -> DROP (abstain);
      * ``unsupported_date``     no resolvable source date but the model supplied one -> IGNORE it
                                 (-> episode reference), so a hallucinated date never reaches valid_at;
      * ``current_observation``  dateless present-state -> episode reference (trusted_when=None)."""
    if operation not in ("absolute", "delta", "expire"):
        return ("current_observation", True, None)   # unknown op: never trust a model date
    span = (stated_span or "").strip()
    if not span:
        return ("current_observation", True, None)
    if _BOUNDED_RE.search(span):
        return ("bounded_unsupported", False, None)   # temporary value; abstain until valid_until (Phase C)
    if _PAST_RE.search(span) and not _CURRENT_RE.search(span):
        return ("past_only", False, None)
    # DETERMINISTIC source date wins; the model's date is only a cross-check.
    resolved, specific_unresolvable = _parse_source_date(span, episode_reference)
    if resolved is not None:
        if model_when and not _same_calendar_day(model_when, resolved):
            return ("date_conflict", False, None)     # model contradicts the source date -> drop
        return ("explicit_time", True, resolved)      # persist the SOURCE-derived timestamp
    if _FUTURE_REL_RE.search(span):
        return ("future_unresolved", False, None)     # relative future, no explicit date -> abstain
    if specific_unresolvable:
        return ("unresolved_date", False, None)       # a specific date we won't year-infer -> abstain
    if model_when:
        return ("unsupported_date", True, None)       # keep the value, drop the unsupported model date
    return ("current_observation", True, None)


def _resolve_valid_time(
    when: str | None, episode_reference: str | None, learned_at: str,
) -> tuple[str, str]:
    """Choose (valid_at, time_basis) for the persisted assertion, precision-first:
      * an explicit parsed `when` (from C.4.1) -> ('explicit', when);
      * else the episode's own reference time when known -> ('episode_reference', that time);
      * else ingest time -> ('learned_fallback', learned_at).
    So a value with no stated world-time still gets a defensible `valid_at` for LWW ordering, tagged
    with HOW it was determined (`time_basis` provenance) rather than silently pretending precision."""
    if when:
        return when, "explicit"
    if episode_reference:
        return episode_reference, "episode_reference"
    return learned_at, "learned_fallback"
