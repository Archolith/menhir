"""Hedged-value abstention and activity-count admission for the typed-scalar boundary.

Span-local underdetermined-value detection (`is_ambiguous_exact`) and the structural activity-count
admission guards. Moved verbatim from ``typed_scalar_rules``; the facade module re-exports every
name so existing import sites keep working unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from menhir.services.typed_scalar_rules_temporal import _SRC_ISO_RE, _SRC_MONTH_DAY_YEAR_RE

# --- hedged-value abstention (span-local admission validation) -------------------------------------
# The unanimous k-sample gate is necessary but NOT sufficient: on an UNDERDETERMINED statement the 3
# samples can agree on the SAME over-confident exact value ("about 30 or 40 coins" -> all say 30), so the
# gate admits it. This deterministic post-extraction check drops an EXACT value-bearing proposal whose
# grounded span states an underdetermined value, so it fails closed to abstention. Span-local (matched
# ONLY against the value's own stated_span, never the wider episode), so "or" elsewhere in the sentence
# never governs, and all k samples of one claim decide identically.
#: discrete alternatives -- "30 or 40" is one-of-several, NEVER a definite value or a real interval.
_AMBIG_DISCRETE_RE = re.compile(r"\b(?:or|either)\b", re.I)
#: approximation / vagueness -- an exact value would be over-confident ("about 30", "a few", "~7:30").
_AMBIG_APPROX_RE = re.compile(
    r"\b(?:about|around|roughly|approx(?:imately)?|maybe|possibly|perhaps|sometimes|"
    r"several|a\s+few|a\s+couple|or\s+so)\b|~|-ish\b", re.I)
#: ``around`` is also part of a non-quantifying discourse/temporal idiom ("the next time around").
#: Remove only that grammatical use before hedge detection. All other uses remain conservative and
#: fail closed, including open-world quantities such as "around a year" or "around a kilogram".
_NON_QUANTIFYING_AROUND_RE = re.compile(
    r"\b(?:(?:this|that|another|next|last|each|every|first|second|third)\s+time|"
    r"the\s+(?:first|second|third|next|last)\s+time)\s+around\b",
    re.I,
)
#: interval framing -- an interval was stated; a [lo,hi] RANGE value captures it, an EXACT value does not
#: (also forces abstention for kinds with no range support, e.g. "between 7:00 and 7:30" clock_time).
_AMBIG_RANGE_RE = re.compile(r"\bbetween\b|\bfrom\b.*\bto\b|\d\s*(?:-|–|—|to)\s*\d", re.I)

# Activity totals are intentionally admitted from the typed scalar path, but only when the source
# actually reports a completed/current total.  The LLM can still emit an otherwise well-formed row
# for a modal or one-off activity clause, so these checks run after span grounding and before the
# proposal can vote.  They are deliberately structural (not a verb/benchmark registry): ``times``
# identifies an activity-count expression while ``_count`` identifies the requested activity lane.
_ACTIVITY_COUNT_ATTRIBUTE_RE = re.compile(r"_count$")
_ACTIVITY_TOTAL_TOKEN_RE = re.compile(r"\b(?:times|once|twice|thrice)\b", re.I)
_ACTIVITY_MODAL_RE = re.compile(
    r"\b(?:if|unless|maybe|perhaps|possibly|might|could|would|should|will|shall|can(?!-)|"
    r"cannot|must(?!-)|"
    r"want(?:s|ed)?\s+to|plan(?:s|ned|ning)?\s+to|hope(?:s|d|ing)?\s+to|"
    r"expect(?:s|ed)?\s+to|going\s+to|gonna)\b",
    re.I,
)
_ACTIVITY_EXACT_COUNT_CUE_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|dozen|"
    r"once|twice|thrice)\b",
    re.I,
)
_ACTIVITY_PRESENT_PERFECT_RE = re.compile(
    r"(?:\b(?:i|we|you|they|he|she|it)\s+(?:have|has)\b|"
    r"\b(?:i|we|you|they|he|she|it)['’](?:ve|s)\b)", re.I,
)
# A prepositional event window such as ``once at the concert`` or ``six times during the trip``.
# This is intentionally generic; the surrounding ``_count`` + activity-total shape supplies the
# semantic context without a brittle list of activity verbs or event names.
_ACTIVITY_EVENT_BOUND_RE = re.compile(
    r"\b(?:at|during|on)\s+(?:the|a|an)\s+\w+(?:\s+\w+){0,3}\b", re.I,
)


def _activity_source_context(content: str, span_start: int, span_end: int) -> str:
    """Return the grounded clause prefix, including a controlling prefix but no trailing clause."""
    text = content or ""
    left_candidates = [text.rfind(mark, 0, span_start) for mark in ".?!;,:\n"]
    left = max(left_candidates, default=-1) + 1
    return text[left:span_end].strip()


def _activity_clause_context(content: str, span_start: int, span_end: int) -> str:
    """Return the punctuation-bounded clause containing the grounded span.

    This wider context is used only for activity tokens and event-window cues. Modal detection stays
    on ``_activity_source_context`` so a trailing independent clause cannot veto the grounded claim.
    """
    text = content or ""
    left_candidates = [text.rfind(mark, 0, span_start) for mark in ".?!;,:\n"]
    left = max(left_candidates, default=-1) + 1
    right_candidates = [text.find(mark, span_end) for mark in ".?!;,:\n"]
    right_candidates = [i for i in right_candidates if i >= 0]
    right = min(right_candidates, default=len(text))
    return text[left:right].strip()


def _activity_scalar_admission_reason(
    *, attribute: str, value_kind: str, operation: str, source_context: str,
    event_context: str | None = None,
) -> str | None:
    """Reject unsafe activity-count absolutes while leaving ordinary counts untouched.

    The source context is already grounded to the proposal's clause.  Present-perfect totals (the
    normal ``I've worn ... six times`` form) remain valid unless an explicit event window is present.
    A simple-past ``once`` or event-window occurrence is not promoted to a cumulative scalar, and
    modal/conditional totals fail closed.
    """
    if (
        operation not in {"absolute", "delta"}
        or value_kind != "count"
        or not _ACTIVITY_COUNT_ATTRIBUTE_RE.search(attribute or "")
    ):
        return None
    context = (source_context or "").strip()
    modal = _ACTIVITY_MODAL_RE.search(context)
    count_cue = _ACTIVITY_EXACT_COUNT_CUE_RE.search(context)
    if modal is not None and count_cue is not None and modal.start() < count_cue.start():
        return "activity_modal_or_hypothetical"
    if operation != "absolute":
        return None
    activity_context = (event_context or context).strip()
    if not _ACTIVITY_TOTAL_TOKEN_RE.search(activity_context):
        return None  # ordinary scalar counts ("I have 3 books") are unaffected by occurrence checks
    # A present-perfect clause reports a current cumulative total even when the total is one.  If
    # there is an explicit event window, it is not a durable total even when expressed in the
    # present perfect (the window/epoch slot identity is not available here). If there is no perfect
    # auxiliary, ``once`` or an event window describes an occurrence, not a durable total. The
    # temporal resolver independently handles ``yesterday``/other past-only clauses; this guard
    # covers dateless/event-bounded simple past wording.
    if _ACTIVITY_EVENT_BOUND_RE.search(activity_context):
        return "activity_event_bounded_occurrence"
    if not _ACTIVITY_PRESENT_PERFECT_RE.search(context):
        if re.search(
            r"\bonce\b|\bone\s+time\b|\ba\s+single\s+time\b|\b1\s+times?\b",
            activity_context,
            re.I,
        ):
            return "activity_single_occurrence"
    return None


def is_ambiguous_exact(stated_span: str, value: Any, operation: str) -> bool:
    """True when the grounded span states an UNDERDETERMINED value that must not become an exact scalar
    View, so the proposal is dropped (-> abstention). Deterministic + span-local. A genuine ``[lo, hi]``
    RANGE value is exempt from the approximation/interval signals (the range already expresses the
    uncertainty); DISCRETE alternatives ("30 or 40") are rejected even when formatted as a range, because
    they are not a real interval. Only current value-bearing operations (absolute/delta) are checked --
    an expire carries the OLD value as provenance only. Conservative: a missed hedge merely admits a
    value the unanimous gate already vetted; a false reject would silently drop a good value, so the
    signals are word-boundary and span-local to avoid firing on ordinary prose."""
    if operation not in ("absolute", "delta"):
        return False
    span = (stated_span or "").strip()
    if not span:
        return False
    if _AMBIG_DISCRETE_RE.search(span):
        return True
    is_range = isinstance(value, (list, tuple))
    if not is_range:
        # A calendar date is never a VALUE range; strip explicit dates first so an ISO date
        # ("2026-03-04") or "Month D, YYYY" does not trip the numeric-range detector via its internal
        # digit-dash-digit ("03-04"). Dates are handled by the temporal resolver, not hedged-abstention.
        span_no_date = _SRC_ISO_RE.sub(" ", _SRC_MONTH_DAY_YEAR_RE.sub(" ", span))
        span_for_approx = _NON_QUANTIFYING_AROUND_RE.sub(" ", span_no_date)
        if (
            _AMBIG_APPROX_RE.search(span_for_approx)
            or _AMBIG_RANGE_RE.search(span_no_date)
        ):
            return True
    return False
