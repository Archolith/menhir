"""Deterministic v0.1 typed-scalar extraction (pure, shadow-safe, no LLM, no persistence).

This is the deterministic-first candidate extractor designed in
`.agent/plans/menhir-deterministic-first-event-scalar-2026-07-30.md`. It admits explicit scalar
statements without an LLM by matching a CLOSED, versioned registry of surface templates and
routing every constructed raw observation through `typed_scalar_rules.parse_scalar_row` — the
SAME validation pipeline the LLM path uses — so unique-span grounding, kind-typed value
validation, span-local hedge abstention, temporal disposition, duration/frequency normalization,
and source identity cannot fork between extractors.

v0.1 boundaries (deliberately narrow; abstention is the safe default):

- Subject authority is CANONICAL SELF ONLY (`SELF_SUBJECT_DISPLAY`). No named third parties, no
  owned-object inference ("my car has 4 wheels" abstains), no head-noun-to-slot guessing.
- Attribute/slot mapping is the closed `TEMPLATE_REGISTRY`. Unknown property phrases,
  unregistered synonyms, and template collisions abstain. There is no free-form attribute
  naming.
- Supported classes: absolute count / money / measurement / percent, standing-property
  clock_time, duration, normalized frequency, numeric range, deltas whose admitted span names
  an explicit held-slot accumulator ("I added 5 coins to my collection" — a bare "I added
  5 coins" is NOT admitted), expire, previous/current pairs, and correction admissions whose
  grounded span INCLUDES the correction cue (optional "actually," on a safe measurement
  surface). Boolean/status/weekday are NOT supported: weekday names, status adjectives
  (married/employed/...), and true/false surface as unmatched scalar cues so the router sends
  the episode to the LLM.
- One-off purchases/payments/events abstain ("I paid $250", "I met my friend at 3:00 pm").
- Quote contract: `stated_span` is the shortest COMPLETE source fragment containing every
  consumed semantic cue (cue-bearing pronouns and verbs are kept, never stripped; an omitted
  slot noun is never inferred — "now I have 37" without the noun is fallback-only).
- Any candidate drop — duplicate-span ambiguity, hedge/temporal/parse failure, template
  mismatch, or collision — marks the episode NOT fully covered and NOT router-eligible;
  abstentions are explicit with stable reason codes.
- A sentence whose matched spans leave unconsumed semantic text ("I have 37 coins?", "Maybe I
  have 37 coins.", "I have 37 coins for now.") reports `unconsumed_context`: the claim may be
  receipted as admitted, but the episode is NOT router-eligible. Only whitespace, terminal
  punctuation, commas/semicolons, and the connector "and" may remain outside the spans.
- The extractor NEVER fabricates a k-sample consensus, never calls an LLM, and never persists.
  It reports proposals plus per-episode/per-candidate receipts with stable reason codes so a
  later shadow comparison and router-completeness measurement can run offline.

Content coordinate space: episodes arrive as they do in the LLM path (content may carry a
`[YYYY-MM-DD] ` prefix); templates locate quotes against the SAME string the LLM path sees, so
offsets share one coordinate space with `source_key`. The synthetic date prefix is excluded from
scalar-cue detection so it cannot masquerade as an unmatched numeric claim.

Reasons and outcomes are stable strings (constants below); `parse_scalar_row` drop reasons
(`span_not_uniquely_located`, `hedged_value`, `temporal_past_only`, ...) pass through
unchanged so the shadow comparison can attribute every abstention.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from menhir.services.typed_scalar_rules import (
    SELF_SUBJECT_DISPLAY,
    TypedScalarProposal,
    _normalize_interval_frequency,
    classify_absolute_semantics,
    parse_scalar_row,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "TEMPLATE_VERSION",
    "OUTCOME_ADMITTED",
    "OUTCOME_DROPPED",
    "REASON_NON_SELF_SUBJECT",
    "REASON_TEMPLATE_COLLISION",
    "REASON_TEMPLATE_MISMATCH",
    "REASON_FREQUENCY_NORMALIZATION",
    "REASON_UNCONSUMED_CONTEXT",
    "CandidateReceipt",
    "DeterministicExtraction",
    "DeterministicScalarExtractor",
    "EpisodeReceipt",
    "SurfaceTemplate",
    "TEMPLATE_REGISTRY",
]

EXTRACTOR_VERSION = "det-v0.1"
TEMPLATE_VERSION = "templates-v0.1"

OUTCOME_ADMITTED = "admitted"
OUTCOME_DROPPED = "dropped"

REASON_NON_SELF_SUBJECT = "non_self_subject"
REASON_TEMPLATE_COLLISION = "template_collision"
REASON_TEMPLATE_MISMATCH = "template_mismatch"
REASON_FREQUENCY_NORMALIZATION = "frequency_normalization_failed"
REASON_UNCONSUMED_CONTEXT = "unconsumed_context"

#: stable abstention codes for scalar-cued text no template matched (router completeness input).
_UNMATCHED_CUE_CODES: dict[str, str] = {
    "number": "unmatched_numeric_cue",
    "currency": "unmatched_currency_cue",
    "percent": "unmatched_percent_cue",
    "clock": "unmatched_clock_cue",
    "unit": "unmatched_unit_cue",
    "freq": "unmatched_frequency_cue",
    "change": "unmatched_change_verb_cue",
    "usedto": "unmatched_used_to_cue",
    "weekday": "unmatched_weekday_cue",
    "status": "unmatched_status_cue",
    "boolean": "unmatched_boolean_cue",
    "semantic_status": "unmatched_status_cue",
    "semantic_boolean": "unmatched_boolean_cue",
    "numword": "unmatched_numeric_cue",
}

#: possessive nouns that denote SELF attributes (registry-backed; everything else is non-self).
_SELF_ATTRIBUTE_NOUNS: frozenset[str] = frozenset({
    "weight", "height", "commute", "workout", "shift", "bedtime", "wake",
    "balance", "savings", "checking", "degree", "thesis",
})

# ------------------------------------------------------------------------------------------------ #
# Surface template registry (CLOSED, versioned). Every entry maps one literal surface pattern to
# exactly one canonical snake_case slot; unknown surfaces abstain by construction.
# ------------------------------------------------------------------------------------------------ #

_NUM_TOKEN = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NOUNS = r"coins|books|cats|dogs|records|plants|stamps"
_NOUN_GROUP = rf"(?P<noun>{_NOUNS})"
_MASS_UNIT = r"kg|kilos?|kilograms?"
_HEIGHT_UNIT = r"cm|centimetres?|centimeters?"
_CLOCK = r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)?"
_HAVE_PHRASE = r"i(?:'ve\s+got|'ve|\s+have(?:\s+got)?)"


def _parse_number(text: str | None) -> int | float:
    """Strict numeric parse of a template-captured number (commas allowed; int stays int)."""
    cleaned = (text or "").replace(",", "")
    if "." not in cleaned:
        return int(cleaned)
    return float(cleaned)


def _clock_value_or_raise(match: re.Match[str]) -> str:
    """Normalize a captured clock time to zero-padded HH:MM.

    A 12h AM/PM suffix is only valid on hours 1..12; mixed 24h+meridiem forms ("22:15 PM",
    "00:15 am") raise ValueError so the candidate drops as a template mismatch and the episode
    is never router-eligible. 24h-without-meridiem and 12h forms stay distinct and valid.

    The ``_or_raise`` suffix is load-bearing (CF-92): ``structural_scalar_composer`` applies the
    identical rule and returns ``None`` instead, because its caller reports a reason code. The two
    were both named ``_clock_value``, which read as interchangeable and was not.
    """
    hour = int(match.group("hour"))
    minute = match.group("minute")
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"meridiem hour out of range: {hour}")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    return f"{hour:02d}:{minute}"


_MASS_UNIT_TO_CANON = {"kg": "kg", "kilo": "kg", "kilos": "kg",
                       "kilogram": "kg", "kilograms": "kg"}
_HEIGHT_UNIT_TO_CANON = {"cm": "cm", "centimeter": "cm", "centimeters": "cm",
                         "centimetre": "cm", "centimetres": "cm"}
_DURATION_UNIT_TO_CANON = {"minute": "minutes", "minutes": "minutes",
                           "hour": "hours", "hours": "hours"}
_PERIOD_TO_CANON = {"day": "day", "days": "day", "week": "week", "weeks": "week",
                    "month": "month", "months": "month", "year": "year", "years": "year"}
_ACCUMULATOR_PHRASE = r"(?:collection|inventory|holdings)"
_ACTIVITY_TO_SLOT = {"go to the gym": "gym_frequency", "run": "run_frequency",
                     "swim": "swim_frequency"}


def _unit_from(mapping: dict[str, str]) -> Callable[[re.Match[str]], str]:
    return lambda m: mapping.get((m.group("unit") or "").lower(), m.group("unit"))


def _interval_placeholder_value(match: re.Match[str]) -> int:
    """Placeholder for 'every ...' frequency forms; `parse_scalar_row` overwrites it via the
    shared interval normalizer, which is verified before admission (see extract)."""
    return 1


@dataclass(frozen=True)
class SurfaceTemplate:
    """ONE closed surface -> canonical slot mapping. `pattern` must capture the number in a
    named group `n` (or `lo`/`hi` for ranges, `hour`/`minute`/`meridiem` for clock times);
    `value_from`/`attribute_from`/`unit_from` override the default derivations."""

    template_id: str
    class_id: str
    attribute: str
    value_kind: str
    unit: str
    operation: str
    pattern: re.Pattern[str]
    subject: str = SELF_SUBJECT_DISPLAY
    scope: str = ""
    value_from: Callable[[re.Match[str]], Any] | None = field(default=None, compare=False)
    attribute_from: Callable[[re.Match[str]], str] | None = field(default=None, compare=False)
    unit_from: Callable[[re.Match[str]], str] | None = field(default=None, compare=False)


TEMPLATE_REGISTRY: tuple[SurfaceTemplate, ...] = (
    # ---- c_count: explicit self-held tallies -----------------------------------------------
    SurfaceTemplate("count_have", "c_count", "coins", "count", "", "absolute",
                    re.compile(rf"(?<!now\s)\b{_HAVE_PHRASE}\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE), attribute_from=lambda m: m.group("noun")),
    SurfaceTemplate("count_now_have", "c_count", "coins", "count", "", "absolute",
                    re.compile(rf"\bnow\s+{_HAVE_PHRASE}\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE), attribute_from=lambda m: m.group("noun")),
    SurfaceTemplate("count_have_now", "c_count", "coins", "count", "", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+now\s+(?:have|'ve\s+got)\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE), attribute_from=lambda m: m.group("noun")),
    # ---- c_range: closed numeric interval ---------------------------------------------------
    SurfaceTemplate("count_range", "c_range", "coins", "count", "", "absolute",
                    re.compile(rf"\bi\s+have\s+between\s+(?P<lo>{_NUM_TOKEN})\s+and\s+(?P<hi>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun"),
                    value_from=lambda m: [_parse_number(m.group("lo")),
                                          _parse_number(m.group("hi"))]),
    SurfaceTemplate("measure_weight_range", "c_range", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"\bi\s+weigh\s+between\s+(?P<lo>{_NUM_TOKEN})\s+and\s+(?P<hi>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON),
                    value_from=lambda m: [_parse_number(m.group("lo")),
                                          _parse_number(m.group("hi"))]),
    SurfaceTemplate("measure_height_range", "c_range", "height", "measurement", "cm", "absolute",
                    re.compile(rf"\bmy\s+height\s+is\s+between\s+(?P<lo>{_NUM_TOKEN})\s+and\s+(?P<hi>{_NUM_TOKEN})\s+(?P<unit>{_HEIGHT_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_HEIGHT_UNIT_TO_CANON),
                    value_from=lambda m: [_parse_number(m.group("lo")),
                                          _parse_number(m.group("hi"))]),
    # ---- c_money: explicit self balances ----------------------------------------------------
    SurfaceTemplate("money_balance_dollar", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bmy\s+(?P<account>savings|checking)\s+balance\s+is\s+\$(?P<n>{_NUM_TOKEN})\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    SurfaceTemplate("money_balance_dollars", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bmy\s+(?P<account>savings|checking)\s+balance\s+is\s+(?P<n>{_NUM_TOKEN})\s+dollars?\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    SurfaceTemplate("money_account_dollar", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bi\s+have\s+\$(?P<n>{_NUM_TOKEN})\s+in\s+my\s+(?P<account>savings|checking)\s+account\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    SurfaceTemplate("money_account_dollars", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bi\s+have\s+(?P<n>{_NUM_TOKEN})\s+dollars?\s+in\s+my\s+(?P<account>savings|checking)\s+account\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    # ---- c_measurement: self body measurements ----------------------------------------------
    SurfaceTemplate("measure_weight_i", "c_measurement", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"(?<!now\s)(?<!actually,\s)\bi\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_weight_correction", "c_measurement", "weight", "measurement", "kg",
                    "absolute",
                    re.compile(rf"\bactually,\s+i\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_weight_my", "c_measurement", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"\bmy\s+weight\s+is\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_weight_now", "c_measurement", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"\bnow\s+i\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_height_i", "c_measurement", "height", "measurement", "cm", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+am\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_HEIGHT_UNIT})\s+tall\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_HEIGHT_UNIT_TO_CANON)),
    SurfaceTemplate("measure_height_my", "c_measurement", "height", "measurement", "cm", "absolute",
                    re.compile(rf"\bmy\s+height\s+is\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_HEIGHT_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_HEIGHT_UNIT_TO_CANON)),
    # ---- c_percent: explicit self completion ------------------------------------------------
    SurfaceTemplate("percent_complete", "c_percent", "degree_progress", "measurement", "percent",
                    "absolute",
                    re.compile(rf"\bi\s+am\s+(?P<n>{_NUM_TOKEN})\s*%\s+(?:done|complete)\s+with\s+my\s+(?P<work>degree|thesis)\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('work').lower()}_progress"),
    # ---- c_clock_time: standing-property times only (no event times) -------------------------
    SurfaceTemplate("clock_wake_at", "c_clock_time", "wake_time", "clock_time", "", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+wake\s+up\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("clock_wake_my", "c_clock_time", "wake_time", "clock_time", "", "absolute",
                    re.compile(rf"\bmy\s+wake\s+time\s+is\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("clock_bed_at", "c_clock_time", "bed_time", "clock_time", "", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+go\s+to\s+bed\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("clock_bed_my", "c_clock_time", "bed_time", "clock_time", "", "absolute",
                    re.compile(rf"\bmy\s+bedtime\s+is\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    # ---- c_duration: standing self durations -------------------------------------------------
    SurfaceTemplate("duration_commute", "c_duration", "commute_duration", "duration", "minutes",
                    "absolute",
                    re.compile(rf"\bmy\s+commute\s+takes\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>minutes?|hours?)\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_DURATION_UNIT_TO_CANON)),
    SurfaceTemplate("duration_workout", "c_duration", "workout_duration", "duration", "minutes",
                    "absolute",
                    re.compile(rf"\bmy\s+workout\s+lasts\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>minutes?|hours?)\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_DURATION_UNIT_TO_CANON)),
    SurfaceTemplate("duration_shift", "c_duration", "shift_duration", "duration", "hours",
                    "absolute",
                    re.compile(rf"\bmy\s+shift\s+is\s+(?P<n>{_NUM_TOKEN})\s+hours?\b",
                               re.IGNORECASE)),
    SurfaceTemplate("duration_sleep", "c_duration", "sleep_duration", "duration", "hours",
                    "absolute",
                    re.compile(rf"\bi\s+sleep\s+(?P<n>{_NUM_TOKEN})\s+hours?\s+(?:a|per)\s+night\b",
                               re.IGNORECASE)),
    # ---- c_frequency: explicit rates and intervals -------------------------------------------
    SurfaceTemplate("frequency_rate", "c_frequency", "gym_frequency", "frequency", "week",
                    "absolute",
                    re.compile(rf"\bi\s+(?P<activity>go\s+to\s+the\s+gym|run|swim)\s+(?P<n>{_NUM_TOKEN})\s+times?\s+(?:a|per)\s+(?P<period>day|week|month|year)s?\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: _ACTIVITY_TO_SLOT[m.group("activity").lower()],
                    unit_from=lambda m: _PERIOD_TO_CANON[(m.group("period") or "").lower()]),
    SurfaceTemplate("frequency_interval", "c_frequency", "gym_frequency", "frequency", "",
                    "absolute",
                    re.compile(rf"\bi\s+(?P<activity>go\s+to\s+the\s+gym|run|swim)\s+every\s+(?:(?:other|(?P<c>{_NUM_TOKEN}))\s+)?(?P<period>day|week|month|year)s?\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: _ACTIVITY_TO_SLOT[m.group("activity").lower()],
                    value_from=_interval_placeholder_value),
    # ---- c_delta_add / c_delta_sub: held-slot changes with explicit accumulator context ------
    # A bare change verb ("I added 5 coins") is NOT admitted: the grounded span must name the
    # held slot/accumulator the quantity moved into or out of.
    SurfaceTemplate("delta_add", "c_delta_add", "coins", "count", "", "delta",
                    re.compile(rf"\bi\s+added\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\s+to\s+my\s+{_ACCUMULATOR_PHRASE}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun")),
    SurfaceTemplate("delta_sold", "c_delta_sub", "coins", "count", "", "delta",
                    re.compile(rf"\bi\s+sold\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\s+from\s+my\s+{_ACCUMULATOR_PHRASE}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun"),
                    value_from=lambda m: -_parse_number(m.group("n"))),
    SurfaceTemplate("delta_gave_away", "c_delta_sub", "coins", "count", "", "delta",
                    re.compile(rf"\bi\s+gave\s+away\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\s+from\s+my\s+{_ACCUMULATOR_PHRASE}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun"),
                    value_from=lambda m: -_parse_number(m.group("n"))),
    # ---- c_expire: values that ENDED (used-to forms only) ------------------------------------
    SurfaceTemplate("expire_wake", "c_expire", "wake_time", "clock_time", "", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+wake\s+up\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("expire_bed", "c_expire", "bed_time", "clock_time", "", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+go\s+to\s+bed\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("expire_weight", "c_expire", "weight", "measurement", "kg", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("expire_have", "c_expire", "coins", "count", "", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+have\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun")),
)


# ------------------------------------------------------------------------------------------------ #
# Receipts
# ------------------------------------------------------------------------------------------------ #


@dataclass(frozen=True)
class CandidateReceipt:
    """One template match and its outcome: admitted proposal or explicit abstention reason."""

    template_id: str
    class_id: str
    episode_index: int
    source_start: int
    source_end: int
    stated_span: str
    subject_text: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    operation: str
    value: Any
    outcome: str
    drop_reason: str | None
    checks: tuple[str, ...]
    proposal: TypedScalarProposal | None


@dataclass(frozen=True)
class EpisodeReceipt:
    """Per-episode outcome: coverage status plus candidate-level receipts and abstention codes."""

    episode_uuid: str
    episode_index: int
    sentence_count: int
    fully_covered: bool
    reasons: tuple[str, ...]
    candidate_receipts: tuple[CandidateReceipt, ...]


@dataclass(frozen=True)
class DeterministicExtraction:
    """Complete result of one deterministic extraction pass (pure; nothing persisted)."""

    extractor_version: str
    template_version: str
    episode_receipts: tuple[EpisodeReceipt, ...]
    proposals: tuple[TypedScalarProposal, ...]
    fully_eligible_episode_uuids: tuple[str, ...]
    reason_counts: tuple[tuple[str, int], ...]


# ------------------------------------------------------------------------------------------------ #
# Cue detection and sentence analysis (router-completeness reporting only; not routing itself)
# ------------------------------------------------------------------------------------------------ #

_SCALAR_CUE_RE = re.compile(
    r"(?P<currency>[$€£])|(?P<percent>%)|(?P<clock>\d{1,2}:\d{2})|"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)|"
    r"(?P<unit>\b(?:kg|kilos?|kilograms?|cm|centimetres?|centimeters?|minutes?|hours?|seconds?)\b)|"
    r"(?P<freq>\b(?:once|twice|thrice|every)\b|\btimes?\s+(?:a|per)\b)|"
    r"(?P<change>\b(?:added|sold|bought|paid|spent|purchased|gave\s+away|gained|earned)\b)|"
    r"(?P<usedto>\bused\s+to\b)|"
    r"(?P<weekday>\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b)|"
    r"(?P<status>\b(?:married|employed|unemployed|retired)\b)|"
    r"(?P<boolean>\b(?:true|false)\b)|"
    # Semantic claims are cue-completeness signals only. They intentionally have no template
    # and therefore never produce deterministic authority. Exclude already-specific scalar cues
    # so existing weekday/boolean/status receipts remain stable (e.g. "my day off is Monday").
    r"(?P<semantic_status>\b(?:"
    r"(?:my|our|his|her|their)\s+[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)?\s+"
    r"(?:is|are|was|were)|"
    r"(?:i|we|you|they)\s+(?:am|are|is|was|were)"
    r")\s+(?!(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"true|false|married|employed|unemployed|retired)\b)[a-z][a-z0-9_-]*\b)|"
    r"(?P<semantic_boolean>\b(?:i|we|they)\s+(?:have|has|had)\s+"
    r"(?:finished|completed|read|watched|seen|visited)\b)|"
    r"(?P<numword>\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen)\b)",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
_POSSESSIVE_RE = re.compile(r"\bmy\s+([a-z][a-z0-9]*)\b", re.IGNORECASE)


def _prefix_len(content: str) -> int:
    match = _PREFIX_RE.match(content)
    return match.end() if match else 0


def _split_sentences(content: str, prefix_len: int) -> list[tuple[int, int]]:
    """Split content after the synthetic date prefix into (start, end) absolute offsets.

    Boundaries come from `finditer` match positions (the whitespace separator is consumed, the
    preceding terminal punctuation stays inside the prior sentence), so absolute offsets stay
    EXACT across any number of sentences and any amount of separator whitespace.
    """
    sentences: list[tuple[int, int]] = []
    start = prefix_len
    for match in _SENTENCE_SPLIT_RE.finditer(content):
        if match.start() < prefix_len:
            continue
        sentences.append((start, match.start()))
        start = match.end()
    if start < len(content):
        sentences.append((start, len(content)))
    return sentences


def _sentence_index(position: int, sentences: list[tuple[int, int]]) -> int | None:
    for index, (start, end) in enumerate(sentences):
        if start <= position < end:
            return index
    return None


def _scalar_cues(content: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """(reason_code, start, end) for every scalar cue inside [start, end).

    Numeric cues match a COMPLETE numeric token (never one hit per digit), and a number
    adjacent to a currency/percent token is folded into that single semantic cue
    ("$250", "75%" are one cue each, not two).
    """
    raw: list[tuple[str, int, int]] = []
    for match in _SCALAR_CUE_RE.finditer(content, start, end):
        label = match.lastgroup or "number"
        raw.append((label, match.start(), match.end()))
    hits: list[tuple[str, int, int]] = []
    for label, cue_start, cue_end in raw:
        if label == "number":
            folded_into = any(
                _other != "number"
                and (other_end == cue_start or other_start == cue_end)
                for _other, other_start, other_end in raw
            )
            if folded_into:
                continue
        hits.append((_UNMATCHED_CUE_CODES.get(label, "unmatched_numeric_cue"),
                     cue_start, cue_end))
    return hits


def _collision_indices(items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]]) -> set[int]:
    """Indices of matches that overlap any other match (strict overlap; adjacency is fine)."""
    bad: set[int] = set()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1] > items[j][0]:
                bad.add(i)
                bad.add(j)
            else:
                break
    return bad


#: terminal/joining punctuation allowed OUTSIDE the matched spans of an eligible sentence.
_UNCONSUMED_ALLOWED_CHARS = frozenset(".,!;…")
#: the ONLY connector word allowed between matched spans (multi-claim forms like
#: "I have 12 books and I wake up at 7:30." and previous/current pairs joined by commas).
_CONNECTOR_WORD_RE = re.compile(r"\band\b", re.IGNORECASE)


def _has_unconsumed_context(
    content: str,
    s_start: int,
    s_end: int,
    sentence_items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]],
) -> bool:
    """True when text OUTSIDE this sentence's template matches carries semantic content the
    templates did not consume ("I have 37 coins?", "Maybe I have 37 coins.", "I have 37 coins
    for now."). Only whitespace, terminal punctuation (`.`, `!`, `…`), commas/semicolons, and
    the connector "and" may remain; anything else (including `?`) means the deterministic
    parse did not cover the whole claim and the episode must not be router-eligible. General
    by construction: the residual set is a closed contract, not per-phrase."""
    segments: list[tuple[int, int]] = [(s_start, s_end)]
    for ms, me, _t, _m in sorted(sentence_items, key=lambda item: (item[0], item[1])):
        next_segments: list[tuple[int, int]] = []
        for a, b in segments:
            if me <= a or ms >= b:
                next_segments.append((a, b))
                continue
            if a < ms:
                next_segments.append((a, ms))
            if me < b:
                next_segments.append((me, b))
        segments = next_segments
    for a, b in segments:
        text = _CONNECTOR_WORD_RE.sub(" ", content[a:b]).strip()
        if not text:
            continue
        if any(ch not in _UNCONSUMED_ALLOWED_CHARS for ch in text):
            return True
    return False


# ------------------------------------------------------------------------------------------------ #
# Extractor
# ------------------------------------------------------------------------------------------------ #


@dataclass(frozen=True)
class DeterministicScalarExtractor:
    """Pure v0.1 deterministic scalar extractor. Stateless; every call replays identically.

    `templates` defaults to the closed `TEMPLATE_REGISTRY`; tests may inject a bespoke registry.
    """

    extractor_version: str = EXTRACTOR_VERSION
    template_version: str = TEMPLATE_VERSION
    templates: tuple[SurfaceTemplate, ...] = TEMPLATE_REGISTRY

    def extract(self, episodes: list[Any]) -> DeterministicExtraction:
        """Parse all episodes; return admitted proposals plus full abstention receipts."""
        episode_receipts: list[EpisodeReceipt] = []
        proposals: list[TypedScalarProposal] = []
        eligible_uuids: list[str] = []
        reasons: Counter[str] = Counter()

        for index, episode in enumerate(episodes):
            content = str(getattr(episode, "content", "") or "")
            uuid = str(getattr(episode, "uuid", "") or "").strip()
            prefix_len = _prefix_len(content)
            sentences = _split_sentences(content, prefix_len)

            matches: list[tuple[int, int, SurfaceTemplate, re.Match[str]]] = []
            for template in self.templates:
                for match in template.pattern.finditer(content):
                    matches.append((match.start(), match.end(), template, match))
            matches.sort(key=lambda item: (item[0], item[1], item[2].template_id))

            per_sentence: list[list[tuple[int, int, SurfaceTemplate, re.Match[str]]]] = [
                [] for _ in sentences
            ]
            for item in matches:
                si = _sentence_index(item[0], sentences)
                if si is not None:
                    per_sentence[si].append(item)

            episode_reason_codes: list[str] = []
            candidate_receipts: list[CandidateReceipt] = []

            for si, (s_start, s_end) in enumerate(sentences):
                sentence_items = per_sentence[si]
                collided = _collision_indices(sentence_items)
                sentence_candidates: list[CandidateReceipt] = []

                for item_index, (start, end, template, match) in enumerate(sentence_items):
                    receipt = self._candidate_receipt(
                        template, match, index, episodes, start, end,
                        collided=item_index in collided,
                    )
                    sentence_candidates.append(receipt)
                    if receipt.outcome == OUTCOME_ADMITTED and receipt.proposal is not None:
                        proposals.append(receipt.proposal)
                    elif receipt.drop_reason is not None:
                        # Any candidate drop makes the episode NOT fully covered and NOT
                        # router-eligible. Collisions are counted ONCE per collided sentence
                        # below, not per candidate; every other drop counts per candidate.
                        if receipt.drop_reason != REASON_TEMPLATE_COLLISION:
                            reasons[receipt.drop_reason] += 1
                            episode_reason_codes.append(receipt.drop_reason)

                # previous/current composition: an absolute alongside an expire on the same slot.
                expire_attributes = {
                    c.attribute for c in sentence_candidates
                    if c.outcome == OUTCOME_ADMITTED and c.class_id == "c_expire"
                }
                if expire_attributes:
                    sentence_candidates = [
                        replace(c, class_id="c_prev_current")
                        if (c.outcome == OUTCOME_ADMITTED and c.class_id != "c_expire"
                            and c.attribute in expire_attributes)
                        else c
                        for c in sentence_candidates
                    ]
                candidate_receipts.extend(sentence_candidates)

                # Coverage: every scalar cue must lie inside a template match; collisions abstain.
                if collided:
                    episode_reason_codes.append(REASON_TEMPLATE_COLLISION)
                    reasons[REASON_TEMPLATE_COLLISION] += 1
                cue_codes = self._uncovered_cue_codes(content, s_start, s_end, sentence_items)
                for code in cue_codes:
                    episode_reason_codes.append(code)
                    reasons[code] += 1
                if self._non_self_possessive(content, s_start, s_end, sentence_items):
                    episode_reason_codes.append(REASON_NON_SELF_SUBJECT)
                    reasons[REASON_NON_SELF_SUBJECT] += 1
                if sentence_items and _has_unconsumed_context(
                        content, s_start, s_end, sentence_items):
                    episode_reason_codes.append(REASON_UNCONSUMED_CONTEXT)
                    reasons[REASON_UNCONSUMED_CONTEXT] += 1

            episode_receipts.append(EpisodeReceipt(
                episode_uuid=uuid,
                episode_index=index,
                sentence_count=len(sentences),
                fully_covered=not episode_reason_codes,
                reasons=tuple(episode_reason_codes),
                candidate_receipts=tuple(candidate_receipts),
            ))
            if not episode_reason_codes and uuid:
                eligible_uuids.append(uuid)

        return DeterministicExtraction(
            extractor_version=self.extractor_version,
            template_version=self.template_version,
            episode_receipts=tuple(episode_receipts),
            proposals=tuple(proposals),
            fully_eligible_episode_uuids=tuple(eligible_uuids),
            reason_counts=tuple(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        )

    # -- helpers -------------------------------------------------------------------------------

    def _candidate_receipt(
        self,
        template: SurfaceTemplate,
        match: re.Match[str],
        episode_index: int,
        episodes: list[Any],
        start: int,
        end: int,
        *,
        collided: bool,
    ) -> CandidateReceipt:
        """Build the row `parse_scalar_row` consumes, run the shared pipeline, and receipt it."""
        span = match.group(0).strip()
        checks: list[str] = ["exact_span", "self_subject", "closed_slot_mapping"]
        try:
            subject_text = template.subject
            attribute = (template.attribute_from(match) if template.attribute_from else
                         template.attribute)
            unit = (template.unit_from(match) if template.unit_from else template.unit)
            value = (template.value_from(match) if template.value_from else
                     _parse_number(match.group("n")))
        except (AttributeError, IndexError, ValueError, KeyError):
            return CandidateReceipt(
                template_id=template.template_id, class_id=template.class_id,
                episode_index=episode_index, source_start=start, source_end=end,
                stated_span=span, subject_text=template.subject, attribute=template.attribute,
                scope=template.scope, value_kind=template.value_kind, unit=template.unit,
                operation=template.operation, value=None, outcome=OUTCOME_DROPPED,
                drop_reason=REASON_TEMPLATE_MISMATCH, checks=tuple(checks), proposal=None,
            )

        if collided:
            return CandidateReceipt(
                template_id=template.template_id, class_id=template.class_id,
                episode_index=episode_index, source_start=start, source_end=end,
                stated_span=span, subject_text=subject_text, attribute=attribute,
                scope=template.scope, value_kind=template.value_kind, unit=unit,
                operation=template.operation, value=value, outcome=OUTCOME_DROPPED,
                drop_reason=REASON_TEMPLATE_COLLISION, checks=tuple(checks), proposal=None,
            )

        # Interval-frequency forms must normalize through the SHARED interval normalizer; a span
        # that would silently fall back to the placeholder value is an abstention, never a value.
        if (template.value_kind == "frequency" and template.operation == "absolute"
                and template.unit == ""):
            if _normalize_interval_frequency(span) is None:
                return CandidateReceipt(
                    template_id=template.template_id, class_id=template.class_id,
                    episode_index=episode_index, source_start=start, source_end=end,
                    stated_span=span, subject_text=subject_text, attribute=attribute,
                    scope=template.scope, value_kind=template.value_kind, unit=unit,
                    operation=template.operation, value=value, outcome=OUTCOME_DROPPED,
                    drop_reason=REASON_FREQUENCY_NORMALIZATION, checks=tuple(checks),
                    proposal=None,
                )
            checks.append("frequency_normalized")

        row: dict[str, Any] = {
            "episode": episode_index,
            "subject": subject_text,
            "attribute": attribute,
            "scope": template.scope,
            "unit": unit,
            "value_kind": template.value_kind,
            "operation": template.operation,
            "value": value,
            "when": None,
            "stated_span": span,
            "display": "",
        }
        captured: list[str] = []

        def _drop(reason: str) -> None:
            captured.append(reason)

        proposal = parse_scalar_row(row, episodes, _drop)
        if proposal is None:
            drop_reason = captured[0] if captured else "parse_failed"
            return CandidateReceipt(
                template_id=template.template_id, class_id=template.class_id,
                episode_index=episode_index, source_start=start, source_end=end,
                stated_span=span, subject_text=subject_text, attribute=attribute,
                scope=template.scope, value_kind=template.value_kind, unit=unit,
                operation=template.operation, value=value, outcome=OUTCOME_DROPPED,
                drop_reason=drop_reason, checks=tuple(checks), proposal=None,
            )

        class_id = template.class_id
        if (proposal.operation == "absolute"
                and classify_absolute_semantics(proposal.stated_span, "absolute") == "correction"):
            class_id = "c_correction"
        return CandidateReceipt(
            template_id=template.template_id, class_id=class_id,
            episode_index=episode_index, source_start=start, source_end=end,
            stated_span=span, subject_text=subject_text, attribute=attribute,
            scope=template.scope, value_kind=template.value_kind, unit=unit,
            operation=template.operation, value=value, outcome=OUTCOME_ADMITTED,
            drop_reason=None, checks=tuple(checks), proposal=proposal,
        )

    @staticmethod
    def _uncovered_cue_codes(
        content: str,
        s_start: int,
        s_end: int,
        sentence_items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]],
    ) -> list[str]:
        """Reason codes for scalar cues no template match covers in this sentence."""
        codes: list[str] = []
        for code, cue_start, cue_end in _scalar_cues(content, s_start, s_end):
            covered = any(
                ms <= cue_start and cue_end <= me
                for ms, me, _t, _m in sentence_items
            )
            if not covered:
                codes.append(code)
        return codes

    @staticmethod
    def _non_self_possessive(
        content: str,
        s_start: int,
        s_end: int,
        sentence_items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]],
    ) -> bool:
        """True when a scalar-cued sentence carries a possessive OUTSIDE every template match
        whose noun is not a registry self attribute ("my car has 4 wheels", "my battery is at
        75%") — an owned-object claim the v0.1 authority refuses, so the caller routes the
        episode to the LLM. Possessives consumed by a template (the accumulator phrase in
        "I added 5 coins to my collection") are matched regions and are never flagged."""
        if not _scalar_cues(content, s_start, s_end):
            return False
        for match in _POSSESSIVE_RE.finditer(content, s_start, s_end):
            noun = match.group(1).lower()
            if noun in _SELF_ATTRIBUTE_NOUNS:
                continue
            covered = any(
                ms <= match.start() and match.end() <= me
                for ms, me, _t, _m in sentence_items
            )
            if not covered:
                return True
        return False
