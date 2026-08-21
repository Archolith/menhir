"""Deterministic typed-scalar extraction, gating, temporal, and binding rules."""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from menhir.domain.typed_assertion import (
    NUMERIC_KINDS,
    OPERATIONS,
    TypedAssertion,
    VALUE_KINDS,
    build_source_key,
    normalize_scalar,
    validate_value,
)
from menhir.services.seam_types import LlmComplete
#: canonical snake_case for the REQUIRED `attribute` (state family): lowercase start letter, then
#: lowercase/digits/underscores. It must match or the proposal fails closed — we never silently
#: normalize a malformed state-family key, because it would fork durable slot identity. `scope` and
#: `unit` are OPTIONAL modifiers and are instead DETERMINISTICALLY normalized by `_canon_modifier`
#: (lowercase; runs of whitespace/hyphens -> single underscore), so "work days", "work-days" and
#: "work_days" collapse to ONE durable slot rather than three.
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MODIFIER_SEP_RE = re.compile(r"[\s\-]+")
_DURATION_M_SS_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<minutes>\d+):(?P<seconds>[0-5]\d(?:\.\d+)?)$"
)
_DURATION_H_MM_SS_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<hours>\d+):(?P<minutes>[0-5]\d):"
    r"(?P<seconds>[0-5]\d(?:\.\d+)?)$"
)
_TEMPORAL_ATTRIBUTE_PREFIXES: dict[str, tuple[str, ...]] = {
    "current": ("current", "currently"),
    "latest": ("latest",),
    "previous": ("previous", "previously"),
    "prior": ("prior",),
}
_FREQUENCY_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "once": 1,
    "twice": 2,
    "thrice": 3,
}
_FREQUENCY_NUMBER_PATTERN = (
    r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"once|twice|thrice)"
)
_FREQUENCY_INTERVAL_RE = re.compile(
    rf"\b(?:(?P<count>{_FREQUENCY_NUMBER_PATTERN})(?:\s+times?)?\s+)?"
    rf"every\s+(?:(?P<interval>other|{_FREQUENCY_NUMBER_PATTERN})\s+)?"
    r"(?P<period>day|week|month|year)s?\b",
    re.IGNORECASE,
)


def _canon_modifier(s: str) -> str:
    """Deterministic canonical form for an optional slot modifier (scope/unit): trim, lowercase, and
    collapse any run of whitespace or hyphens to a single underscore. Idempotent; '' stays ''."""
    return _MODIFIER_SEP_RE.sub("_", s.strip().lower())


def _canon_attribute(attribute: str, stated_span: str) -> str:
    """Remove an ungrounded source-time prefix from a stable state-family name.

    Scalar history carries *when* a value held, so model-invented names such as
    ``previous_account_balance`` must not fork a second slot when the grounded quote merely says
    ``account balance``. A prefix that is actually present in the quote is preserved: it may classify
    a distinct population (for example ``previous employers``) rather than version the state.
    """
    prefix, separator, base = attribute.partition("_")
    cues = _TEMPORAL_ATTRIBUTE_PREFIXES.get(prefix)
    if not separator or not base or cues is None:
        return attribute
    cue_re = re.compile(rf"\b(?:{'|'.join(re.escape(cue) for cue in cues)})\b", re.IGNORECASE)
    return attribute if cue_re.search(stated_span or "") else base


def _frequency_number(text: str | None, *, default: float) -> float:
    if not text:
        return default
    token = text.strip().lower()
    if token == "other":
        return 2.0
    if token in _FREQUENCY_NUMBER_WORDS:
        return float(_FREQUENCY_NUMBER_WORDS[token])
    return float(token)


def _normalize_interval_frequency(stated_span: str) -> tuple[int | float, str] | None:
    """Normalize exact interval wording to a rate per one base period.

    The model often emits ``1/week`` for both "every week" and "every other week". The source text
    is authoritative and deterministic here: ``every other week`` is ``0.5/week``,
    ``every three days`` is ``1/3/day``, and ``twice every three months`` is ``2/3/month``.
    Multiple interval phrases are left untouched because one row would not identify which one it
    describes.
    """
    matches = list(_FREQUENCY_INTERVAL_RE.finditer(stated_span or ""))
    if len(matches) != 1:
        return None
    match = matches[0]
    count_text = match.group("count")
    interval = _frequency_number(match.group("interval"), default=1.0)
    if interval <= 0:
        return None
    if count_text is None:
        # The count group requires its number to sit IMMEDIATELY before "every", so an
        # intervening noun defeats it -- "I read 2 books every week" captures no count, and
        # the fabricated 1 below then OVERWRITES the model's own extracted 2 at
        # `parse_scalar_row` (there is no fallback and no drop; the wrong number is persisted).
        #
        # Abstain when the source carries a number ahead of "every" that we could not
        # attribute: returning None leaves the model's value intact, which is strictly better
        # than asserting a guess. A phrase with no number at all ("every week", "every other
        # week", "every three days") is genuinely count-1 and still normalizes as before.
        if re.search(
            rf"\b{_FREQUENCY_NUMBER_PATTERN}\b",
            stated_span[: match.start()],
            re.IGNORECASE,
        ):
            return None
    count = _frequency_number(count_text, default=1.0)
    rate = count / interval
    value: int | float = int(rate) if rate.is_integer() else rate
    return value, match.group("period").lower()

logger = logging.getLogger(__name__)

#: (system, user) -> completion text. Injected so this boundary is decoupled from any specific LLM,
#: exactly like `perception.LlmComplete`.

TYPED_SCALAR_SYSTEM_PROMPT = (
    "You convert a user's memory episodes into TYPED STATE OBSERVATIONS for a deterministic memory "
    "system. Read the numbered episodes and emit one observation per concrete fact that states or "
    "changes a CURRENT PROPERTY of some subject the user might later ask about (a status, a count, a "
    "time, an amount, a duration). Do NOT emit an observation for a one-off happening that is not a "
    "standing property. Each observation is an object: "
    "{\"subject\": <WHOSE/WHICH thing has this property. Decide carefully: "
    "if the property belongs to the SPEAKER THEMSELF — 'I', 'me', or 'my <personal attribute>' such as "
    "'my height', 'my wake time', 'my day off', 'my commute' — use exactly 'user'. But if 'my'/'their' "
    "modifies a SEPARATE OWNED OBJECT (my car, my house, my phone, my dog), the subject is THAT OBJECT "
    "('my car'), NOT 'user', and the attribute is the object's property ('my car is red' -> "
    "subject='my car', attribute='color', NOT subject='user'). Never move an owned object's property "
    "onto the user>, "
    "\"attribute\": <stable snake_case state family, e.g. 'wake_time', 'owned', 'weight'. Source-time "
    "versions belong in provenance/history, NOT the family name: an earlier and a later balance both "
    "use 'account_balance', never 'previous_account_balance' or 'current_account_balance'>, "
    "\"scope\": <optional differentiator, e.g. 'work_days', 'pre_1920'; \"\" if none>, "
    "\"value_kind\": one of \"boolean\"|\"status\"|\"count\"|\"duration\"|\"frequency\"|\"money\"|"
    "\"measurement\"|\"clock_time\"|\"weekday\", "
    "\"unit\": <unit for numeric kinds, e.g. 'hours', 'usd', 'kg'; \"\" for non-numeric or unitless>, "
    "\"operation\": \"absolute\" (a stated current value), \"delta\" (a signed change to it), or "
    "\"expire\" (a value that ENDED with no stated replacement -- see EXPIRY), "
    "\"value\": the typed value — true/false for boolean; a short string for status; a day name for "
    "weekday; \"HH:MM\" for clock_time; a number (or [low, high] range) for count/duration/frequency/"
    "money/measurement; a signed number for a delta. SPECIAL CASE: when an elapsed duration is written "
    "as M:SS or H:MM:SS, preserve that exact colon string as value and use unit=\"\"; the deterministic "
    "parser converts it to seconds, "
    "\"when\": <ISO date the value became true — ONLY if the episode EXPLICITLY states a calendar date "
    "('as of July 18', '2026-07-18', 'since 2024'); otherwise \"\" (empty). NEVER infer, guess, or use "
    "today's date, and NEVER turn a relative phrase ('yesterday', 'next Monday', 'a while ago') into a "
    "date>, "
    "\"stated_span\": <the SHORTEST exact source substring that states THIS value. Quote ONLY the clause "
    "for this value -- never the whole sentence when it also states other facts, and never other clauses. "
    "Do NOT begin the quote with a conjunction ('but', 'and', 'however'): 'yesterday I had 20, but now I "
    "have 37' -> quote 'now I have 37' for the current value. Keep the quote identical each time you read "
    "the same fact so repeated readings agree exactly>}. "
    "Rules: use operation=absolute for a stated current value ('I wake at 7:30', 'I have 37 coins', "
    "'my car is red'); use operation=delta for an explicit CHANGE to a numeric quantity, signed by "
    "its direction -- ADDITIVE wording is POSITIVE ('I added 5 coins' -> +5, 'I bought 3 more' -> "
    "+3), SUBTRACTIVE wording is NEGATIVE ('I sold 2 coins' -> -2, 'I gave away 4' -> -4). The test "
    "is WHAT THE NUMBER COUNTS: if it counts the entire quantity now on hand it is absolute; if it "
    "counts only the amount that MOVED it is a delta. A change to a quantity the user still holds "
    "IS a standing property -- do not discard it as a one-off happening. "
    "An ACCUMULATED PROGRESS total is also a current standing count even when phrased as a completed "
    "action: 'I've completed 12 lessons so far' -> count 12, operation=absolute; 'we have closed 40 "
    "tickets to date' -> count 40, operation=absolute. Cues such as 'so far', 'to date', and 'up to "
    "now' make the cumulative total current; do not discard it as a one-off event or reduce it to a "
    "completion boolean. "
    "ACTIVITY TOTALS vs OCCURRENCES vs FREQUENCY -- an explicit completed tally of an activity is a "
    "CURRENT CUMULATIVE COUNT when the source reports a total ('I've worn my black Converse six times', "
    "'I've visited the museum six times', 'I have run 12 races'). Emit value_kind=count, unit='', "
    "operation=absolute, and a stable activity-family attribute ending in '_count' (for example "
    "'wear_count', 'visit_count', 'race_count'). If a distinct activity object is grounded, keep that "
    "object as subject ('my black Converse' -> subject='my black Converse'; 'the museum' -> "
    "subject='the museum'); otherwise bind the activity to the actor. Do NOT emit six event observations "
    "and do NOT convert an explicit "
    "total into a frequency. A SINGLE OCCURRENCE ('I wore them yesterday', 'I visited the museum') is "
    "an event, not a scalar total; emit NOTHING for it here. A RECURRING RATE ('I wear them twice a "
    "week', 'I visit monthly') is value_kind=frequency with the period as unit, never count. An explicit "
    "additive/subtractive activity change ('I wore them two more times', 'I visited three more times') "
    "is operation=delta, signed by direction, only when the same activity/object accumulator lane is "
    "clear; do not do arithmetic. Tentative, hypothetical, approximate, vague, or alternative totals "
    "('I might wear them six times', 'about six times', 'a few times', 'six or seven times') emit "
    "NOTHING rather than an exact scalar. "
    "EXPIRY -- when the text says a value USED TO hold but is no longer current ('I used to wake up at "
    "9:00', 'my day off used to be Monday', 'I no longer live in Dallas', \"I don't smoke anymore\"), "
    "emit operation=expire for that slot with value=the OLD value that ended (so 'I used to wake up at "
    "9:00' -> operation=expire, value_kind=clock_time, value '09:00'). An expire ENDS the old value "
    "without asserting a new one -- do NOT also emit an absolute for the old value, and do NOT invent a "
    "replacement. If the SAME text states BOTH the end and a new value ('I used to live in Dallas, now "
    "Austin'), emit the expire for the old AND an absolute for the new. Do NOT emit expire for: a "
    "QUESTION ('did you use to smoke?'); a past NEGATION ('I didn't use to smoke' -- asserts past false, "
    "not a value that ended); the idiom 'I am used to X' (a current habit, unrelated); or a RESTORATION "
    "where the value is current again ('I used to run, and I do again now' -> emit absolute, NOT expire). "
    "KIND ROUTING — choose value_kind by the value's MEANING and shape, not merely its topic or "
    "punctuation, and apply these in order: "
    "(a) weekday — a day-of-week name (Monday..Sunday) is ALWAYS weekday, NEVER status, even when phrased "
    "as a state ('my day off is Wednesday' -> value_kind=weekday, value 'Wednesday'). "
    "(b) clock_time versus duration — punctuation alone NEVER determines the kind: a colon value can "
    "be either. Use clock_time only when the surrounding words make it a TIME OF DAY, such as 'meet at "
    "8:30', 'wake at 07:30', or 'the appointment starts at 10:15 PM' -> clock_time, value 'HH:MM'. "
    "Use duration when the words describe ELAPSED or PERFORMANCE time, such as 'finished in 25:50', "
    "'race time was 27:12', 'personal best time of 25:50', or 'the task took 1:02:03'. Preserve an "
    "elapsed colon value as its exact M:SS or H:MM:SS string with unit=\"\"; do not reject it merely "
    "because 25:50 would be an invalid wall-clock time. A bare ambiguous colon value with no semantic "
    "time-of-day or elapsed-time context is not enough to emit either kind. "
    "(c) money — a monetary amount ('$250', '250 usd') -> money, unit 'usd'. "
    "(d) duration — an elapsed span of time ('45 minutes', '2 hours') -> duration, unit the time unit. "
    "(e) frequency — a RATE of recurrence normalized per ONE base period: '3 times a week' -> value "
    "3, unit 'week'; 'every other week' -> value 0.5, unit 'week'; 'every three days' -> value 1/3, "
    "unit 'day'; 'twice every three months' -> value 2/3, unit 'month'. "
    "(f) measurement — a physical measurement of the subject with a unit ('height is 180 centimeters', "
    "'weighs 70 kg') -> measurement, unit the measuring unit. "
    "(g) count — a tally of discrete things ('37 coins', '12 books') -> count, unitless. "
    "(h) boolean — a yes/no or done/not-done property, INCLUDING completion or possession states "
    "('I have finished reading Dune' -> boolean true; 'I have a passport' -> boolean true; 'I no longer "
    "smoke' -> boolean false) -> boolean, value true/false. "
    "(i) status — a categorical NON-boolean, NON-weekday state ('married', 'red', 'employed') -> status. "
    "Only fall through to status when none of (a)-(h) fit. "
    "PAST-ONLY vs CURRENT — emit a value only for a CURRENT standing state. A purely-past observation "
    "('yesterday I had 20 coins', 'a while ago I lived in Dallas') is NOT current -> emit NOTHING for it. "
    "When the text pairs a past value with a current one ('yesterday I had 20, now I have 37'), emit ONLY "
    "the current value (37). A recency phrase that describes the PRESENT ('I recently moved to Austin' -> "
    "currently in Austin) IS current -> emit it, with when=\"\" (do not invent the move date). "
    "ACROSS NUMBERED EPISODES, judge whether a statement is current at THAT EPISODE'S source time. Do "
    "not retroactively drop an explicit current observation from an earlier episode merely because a "
    "later episode updates it. For example, an earlier 'I have tried 3 restaurants' and a later 'I have "
    "tried 4 restaurants' produce TWO absolute observations; provenance time lets the fold and history "
    "order them. This does not admit a past-only clause such as 'yesterday I had 3' within an episode. "
    "A past-tense ACTION that changed a quantity the user still has is NOT a purely-past observation: "
    "'I sold 2 coins' / 'I added 5 coins' changed the CURRENT total and must be emitted as a delta. "
    "Likewise, a past-tense achievement can establish a standing record: 'I set a personal best in a "
    "5K with a time of 27:12' -> duration 27:12, operation=absolute, because the personal best remains "
    "a property until replaced. A future intention can mention an existing current record: 'I'm hoping "
    "to beat my personal best time of 25:50' -> duration 25:50, operation=absolute. The hope does NOT "
    "make the existing record future, expired, or a delta; use expire only when the text says the old "
    "value ended or no longer holds. If earlier and later episodes explicitly state different personal "
    "best records, emit BOTH absolute source-time observations rather than only the newest one. "
    "Only a past TOTAL ('yesterday I had 20 coins') is purely-past and dropped. "
    "UNDERDETERMINED VALUES — do NOT invent an exact value the source did not state. If the text gives a "
    "closed numeric interval ('between 30 and 40', '30 to 40'), emit a [low, high] RANGE for a numeric "
    "kind. But if it gives DISCRETE alternatives ('30 or 40', 'maybe 1500 or 1600'), an APPROXIMATION "
    "('about 30', 'around 7:30'), a VAGUE quantity ('a few times', 'several'), or MULTIPLE contextual "
    "values ('usually 7, sometimes 7:30'), emit NOTHING for that value -- it has no single current "
    "scalar. Never turn '30 or 40' into a range: discrete alternatives are not an interval. "
    "CONSISTENCY (critical — the same fact is read several times and only agreeing readings are kept): "
    "for a given fact ALWAYS produce the SAME subject, the SAME concise snake_case attribute, the SAME "
    "value_kind, the SAME canonical unit ('cm' every time, not 'centimeters'), and the SAME when. Prefer "
    "the shortest natural attribute ('finished_reading', not 'finished_reading_dune'); do not add optional "
    "scope/unit unless the fact truly needs them. "
    "FINAL COMPLETENESS AND GROUNDING CHECK (mandatory): before answering, inspect EACH numbered "
    "episode independently and make a row checklist. Do not stop after finding one version of a "
    "property, and do not let a later episode erase an earlier source-time observation. An existing "
    "record named inside a future goal is still a current property and MUST be emitted: 'hoping to "
    "beat my personal best time of 25:50' states the user's current personal-best duration as 25:50, "
    "operation=absolute. Make stated_span repeatable: quote the minimal property-to-value phrase, "
    "excluding framing words, leading pronouns/verbs/adverbs, and terminal punctuation when the "
    "property phrase alone uniquely states the fact. For a personal-best statement, start the quote "
    "at 'personal best time' and end at the numeric value; use 'personal best time in a charity 5K "
    "run with a time of 27:12' and 'personal best time of 25:50'. Return every qualifying checklist "
    "row in its episode's observations array. "
    "Emit ONLY grounded observations: EVERY observation object MUST contain all of subject, attribute, "
    "value_kind, operation, value, and stated_span. Copy stated_span verbatim from the numbered episode "
    "before emitting the object; if you cannot copy an exact source substring, omit that observation. "
    "Emit each source fact only once — never duplicate an observation. Do NOT do "
    "arithmetic; emit the atomic observation and let the system fold. Output ONLY a JSON array with "
    "EXACTLY ONE envelope for EVERY numbered episode, in the same order: "
    "{\"episode\": <number>, \"observations\": [<zero or more observation objects>]}. Use an empty "
    "observations array when that episode has no qualifying state fact. Do not omit an episode, and "
    "do not repeat the episode number inside each observation."
)


@dataclass(frozen=True)
class TypedScalarProposal:
    """One UNBOUND, well-typed, grounded typed-scalar observation from a single extraction pass.

    It carries everything a `TypedAssertion` needs EXCEPT the resolved `subject_uuid` (binding is
    post-finalization, C.4.3) and the gate verdict (C.4.2). `subject_text` is the raw extracted
    subject that binding will resolve against the episode's surviving entities. A proposal only
    exists when its `stated_span` was UNIQUELY located in the episode text, so offsets are always
    known (>=0) and `claim_ordinal` is always 0 — two proposals citing the same source span share a
    `source_key` and are competing interpretations of ONE claim, never order-numbered siblings.
    `when` is the parsed/normalized ISO world-time, or None when the model gave none (C.4.3 then
    assigns an episode_reference / learned_fallback basis)."""

    subject_text: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    operation: str
    value: Any
    stated_span: str
    episode_uuid: str
    span_start: int
    span_end: int
    when: str | None = None
    display: str = ""
    claim_ordinal: int = 0

    @property
    def slot_key(self) -> tuple[str, str, str, str]:
        """(attribute, scope, value_kind, unit) — the ScalarStateView slot this proposal targets,
        independent of subject (subject is resolved at binding time)."""
        return (
            self.attribute.strip().lower(),
            self.scope.strip().lower(),
            self.value_kind.strip().lower(),
            (self.unit or "").strip().lower(),
        )

    @property
    def source_key(self) -> str:
        """BINDING-STABLE claim locator, computed by the SAME domain helper the `TypedAssertion`
        persists (`build_source_key`), so the durable identity this proposal predicts cannot drift
        from the one the store writes. Independent of model output order (offsets are located, not
        counted) and of subject binding."""
        return build_source_key(
            self.episode_uuid, self.span_start, self.span_end, self.claim_ordinal)

    @property
    def normalized_value(self) -> str:
        """Stable normalized value string — the vote key the C.4.2 consistency gate concentrates on
        (mirrors `TypedAssertion` / `ScalarStateKind` normalization)."""
        return normalize_scalar(self.value)


def _parse_json_array(text: str) -> "tuple[list[dict], str | None]":
    """Deterministic parse of the model's JSON response; tolerant of code fences and a leading '+'
    on numbers (LLMs emit '+1', invalid JSON).

    Returns `(rows, failure)`. `failure` is None on a clean parse -- INCLUDING a well-formed empty
    array, which is the model legitimately saying "no scalar claims here". A bare object is accepted
    ONLY when it has the episode-envelope shape (`episode` plus `observations`), because models
    commonly omit the outer array for a single input episode. Arbitrary bare observation objects
    remain invalid. A failure is `malformed_json` (truncation at max_tokens is the common cause,
    since a cut-off array never closes) or `not_a_json_array` (valid JSON of the wrong shape).

    Returning [] for BOTH cases is what the caller used to do, and it made a whole-sample parse
    failure indistinguishable from genuine model silence -- the exact ambiguity the per-row `on_drop`
    seam was added to remove, left open one level up. A truncated sample is not an abstention: it is
    k-1 effective samples, which at threshold=1.0 silently vetoes every claim in that pass."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    text = re.sub(r"(:\s*)\+(\d)", r"\1\2", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], "malformed_json"
    if isinstance(data, dict) and "episode" in data and "observations" in data:
        data = [data]
    if not isinstance(data, list):
        return [], "not_a_json_array"
    return data, None


def _expand_episode_envelopes(
    rows: list[Any],
    episode_count: int,
    drop: "Callable[[str], None]",
) -> list[Any]:
    """Flatten the completeness-enforcing response shape while accepting legacy flat rows.

    Envelope mode is machine-checkable: one object per input episode, including an explicit empty
    list when no scalar exists. Legacy flat arrays remain valid for frozen captures, experiments,
    and callers using the parser boundary directly.
    """
    envelope_mode = any(isinstance(row, dict) and "observations" in row for row in rows)
    if not envelope_mode:
        return rows

    expanded: list[Any] = []
    seen: set[int] = set()
    for envelope in rows:
        if not isinstance(envelope, dict):
            drop("bad_episode_envelope")
            continue
        index = _req_episode_index(envelope, episode_count)
        observations = envelope.get("observations")
        if index is None or not isinstance(observations, list):
            drop("bad_episode_envelope")
            continue
        if index in seen:
            drop("duplicate_episode_envelope")
            continue
        seen.add(index)
        for observation in observations:
            if not isinstance(observation, dict):
                expanded.append(observation)  # parse_scalar_row records the precise row-shape drop
                continue
            row = dict(observation)
            supplied = row.get("episode")
            if supplied is not None and supplied != index:
                drop("episode_envelope_mismatch")
                continue
            row["episode"] = index
            expanded.append(row)

    for _missing in range(episode_count - len(seen)):
        drop("missing_episode_envelope")
    return expanded


def _coerce_value(value_kind: str, operation: str, raw: Any) -> Any:
    """Light coercion of a JSON value into its kind-typed shape BEFORE `validate_value` enforces it.
    Native JSON types mostly arrive correct (bool, number, [lo, hi], string); this only rescues the
    common near-misses — a numeric kind quoted as a string, a range given as a list — and normalizes
    strings. Returns the coerced value (which `validate_value` then accepts or rejects); it never
    fabricates a value it wasn't given."""
    if value_kind == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        return raw  # let validate_value reject anything else
    if value_kind in NUMERIC_KINDS:
        if operation == "delta":
            return _as_number(raw)
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return [_as_number(raw[0]), _as_number(raw[1])]
        return _as_number(raw)
    # status / weekday / clock_time: string kinds
    if isinstance(raw, str):
        return raw.strip()
    return raw


def _as_number(raw: Any) -> Any:
    """Best-effort numeric coercion: an int stays int, a float stays float, a numeric string becomes
    a number; anything else is returned unchanged so `validate_value` can reject it."""
    if isinstance(raw, bool):
        return raw  # a bool is not a number here; validate_value will reject it for a numeric kind
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        s = raw.strip().replace("$", "").replace(",", "")
        try:
            f = float(s)
        except (TypeError, ValueError):
            return raw
        return int(f) if f.is_integer() else f
    return raw


def _duration_colon_seconds(raw: Any, *, allow_signed: bool = False) -> int | float | None:
    """Normalize an elapsed ``M:SS`` or ``H:MM:SS`` string to seconds.

    A colon is ambiguous in untyped prose, but it is not ambiguous after the model has classified
    the value as ``duration``. Canonicalizing here keeps the domain contract numeric, makes
    equivalent renderings vote together, and preserves the original text in ``stated_span`` for
    provenance. Seconds (and, for the three-part form, minutes) must be 00-59; malformed values
    return None and continue through the ordinary fail-closed validator.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    match = _DURATION_M_SS_RE.fullmatch(text)
    hours = 0
    if match is None:
        match = _DURATION_H_MM_SS_RE.fullmatch(text)
        if match is None:
            return None
        hours = int(match.group("hours"))
    sign = match.group("sign")
    if sign and not allow_signed:
        return None
    seconds_text = match.group("seconds")
    seconds: int | float = (
        float(seconds_text) if "." in seconds_text else int(seconds_text)
    )
    total = hours * 3600 + int(match.group("minutes")) * 60 + seconds
    if sign == "-":
        total = -total
    return int(total) if isinstance(total, float) and total.is_integer() else total


def _normalize_colon_duration_value(raw: Any, operation: str) -> tuple[Any, bool]:
    """Return ``(value, normalized)`` for a complete colon-duration value or range.

    Absolute/expire values accept an unsigned point or two-endpoint range. Deltas accept one signed
    point. A mixed range (one colon endpoint and one ordinary number) is deliberately not converted:
    changing its unit to seconds would silently reinterpret the numeric endpoint.
    """
    if operation != "delta" and isinstance(raw, (list, tuple)) and len(raw) == 2:
        normalized_range = [
            _duration_colon_seconds(endpoint, allow_signed=False) for endpoint in raw
        ]
        if all(endpoint is not None for endpoint in normalized_range):
            return normalized_range, True
        return raw, False
    normalized = _duration_colon_seconds(raw, allow_signed=operation == "delta")
    return (normalized, True) if normalized is not None else (raw, False)


def _req_str(row: dict, key: str) -> str | None:
    """A REQUIRED field: return the trimmed string only if the raw value is an actual non-blank
    string. A missing key, a blank string, or a non-string (object/list/number/bool) fails closed to
    None so the caller drops the row — a required field must never acquire a coerced or default
    meaning in this precision-first boundary."""
    raw = row.get(key)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s or None


def _opt_str(row: dict, key: str) -> str | None:
    """An OPTIONAL textual field: '' when absent/null, the trimmed string when a string is present,
    or None (drop signal) when present-but-non-string. So an omitted `scope` is fine, but a
    list/object `scope` fails closed rather than being stringified into a bogus value."""
    if key not in row or row.get(key) is None:
        return ""
    raw = row.get(key)
    if not isinstance(raw, str):
        return None
    return raw.strip()


def _req_episode_index(row: dict, n: int) -> int | None:
    """The `episode` pointer must be a REAL integer in range — a bool (`true`) or a fractional number
    (`0.5`) is not a valid index and fails closed. (`bool` is a subclass of `int`, so it is excluded
    explicitly; a JSON integer parses as `int`, a JSON `0.5` as `float`.)"""
    raw = row.get("episode")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 0 <= raw < n else None


def _parse_iso_strict(s: str) -> datetime | None:
    """STRICT ISO parse for the precision-first boundary — deliberately NOT the tolerant fold reader
    (`fold_algebra._parse`, which truncates to the first 10 chars and stringifies non-strings for
    historical data). Normalizes a trailing 'Z' and slash dates, then requires the WHOLE string to
    parse via `datetime.fromisoformat` (no prefix fallback), so 'valid-date + garbage suffix' or an
    impossible time is rejected, not silently clamped to midnight. Naive -> UTC (matches the
    comparison basis the fold uses at read time)."""
    t = s.strip().replace("Z", "+00:00").replace("/", "-")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _normalize_when(row: dict) -> tuple[bool, str | None]:
    """Validate/normalize the optional world-time. Returns (ok, when):
      * absent/blank -> (True, None): C.4.3 assigns an episode_reference / learned_fallback basis;
      * a REAL string that parses in full as ISO -> (True, normalized-isoformat);
      * present but non-string, or a string that does not fully parse -> (False, None): DROP the row
        (a malformed date must not reach `valid_at`, where it would corrupt LWW ordering)."""
    raw = row.get("when")
    if raw is None:
        return (True, None)
    if not isinstance(raw, str):
        return (False, None)  # a numeric/structured date is not a valid explicit time -> drop
    if not raw.strip():
        return (True, None)
    dt = _parse_iso_strict(raw)
    if dt is None:
        return (False, None)
    return (True, dt.isoformat())


def _ground_span(content: str, stated_span: str) -> tuple[int, int] | None:
    """Locate the quote in the episode text and return (span_start, span_end) char offsets, or None
    when it does NOT occur EXACTLY ONCE. Zero matches -> ungrounded (drop); multiple matches ->
    ambiguous which occurrence is meant (drop). Case-insensitive, but matched via regex over the
    ORIGINAL text (not `content.lower()`), so the returned offsets stay correct even where
    lowercasing would change string length (some Unicode)."""
    quote = (stated_span or "").strip()
    if not quote or not content:
        return None
    matches = list(re.finditer(re.escape(quote), content, re.IGNORECASE))
    if len(matches) != 1:
        return None
    m = matches[0]
    return (m.start(), m.end())


def extract_typed_scalars_once(
    episodes: list[Any], llm_complete: LlmComplete,
    *, on_drop: "Callable[[str], None] | None" = None,
) -> list[TypedScalarProposal]:
    """One typed-scalar extraction pass: prose -> validated, grounded `TypedScalarProposal`s.

    `episodes` are objects with `.uuid` and `.content` (e.g. `perception.Episode`). A row survives
    ONLY if every required field is an actual non-blank string (`subject`, `attribute`, `operation`,
    `stated_span`), `operation` is explicitly present, `episode` is a real in-range integer,
    `attribute` is canonical snake_case, the value passes the domain `validate_value`, any supplied
    `when` parses as ISO, and `stated_span` occurs EXACTLY ONCE in the episode text (unique grounding
    -> located offsets; zero/multiple -> dropped as ungrounded/ambiguous). Everything else fails
    closed to omission â€” malformed model output never acquires a semantic default. `claim_ordinal`
    is always 0: identity comes from the located span, not model output order, so re-ordering rows
    (across k samples or a retry) cannot change a claim's durable `source_key`. Pure given the
    injected `llm_complete`; writes nothing.

    `on_drop` is an OPTIONAL observability seam: when supplied it is called once per DISCARDED row
    with a short reason, and ONCE for a whole-response parse failure (`malformed_json` /
    `not_a_json_array`, both of which yield zero rows). A discarded row is indistinguishable
    downstream from a claim the model never emitted, which is what made the measured 72% "absence"
    band uninterpretable -- this makes the two separable. The response-level case matters more than
    the row-level one: a truncated pass is not an abstention but a lost sample, and at threshold=1.0
    a lost sample vetoes every claim in the batch. It is injected rather than emitted from here so
    the function stays pure by default (25 call sites, almost all tests, are unaffected) and so the
    audit emit lives with the other emits in the caller."""
    if not episodes:
        return []

    def _drop(reason: str) -> None:
        if on_drop is not None:
            on_drop(reason)
    log = "\n".join(f"[{i}] {getattr(e, 'content', '')}" for i, e in enumerate(episodes))
    raw_rows, parse_failure = _parse_json_array(llm_complete(TYPED_SCALAR_SYSTEM_PROMPT, log))
    if parse_failure is not None:
        _drop(parse_failure)
    else:
        raw_rows = _expand_episode_envelopes(raw_rows, len(episodes), _drop)

    out: list[TypedScalarProposal] = []
    for row in raw_rows:
        proposal = parse_scalar_row(row, episodes, _drop)
        if proposal is not None:
            out.append(proposal)
    return out


# ---------------------------------------------------------------------------- (2) k-sample gate

#: perception proposals are LLM-derived, so a committed typed-scalar decision is 'agent' tier — the
#: lowest, advisory tier. Higher tiers (user/manual/trusted_tool) come from other paths, NEVER from
#: probabilistic extraction. C.4.3 stamps this onto the persisted :TypedAssertion; the fold's
#: effective-tier is still the weakest required contributor at read time (C.2), never trusted here.
PERCEPTION_EVIDENCE_TIER = "agent"

#: structured veto labels (telemetry), mirroring `perception.VETO_*`: which guard produced a decision.
TS_VETO_COMMIT = "commit"
TS_VETO_SELF_CONSISTENCY = "self_consistency"   # no single interpretation reached the threshold
TS_VETO_TIE = "tie"                             # two interpretations share the modal count (no unique winner)
TS_VETO_UNGROUNDED = "ungrounded"               # defense-in-depth: winner lacks a located span


def _validate_threshold(threshold: float) -> None:
    """A commit threshold must be a real fraction in (0, 1]. Reject bool, non-finite (NaN/inf), and
    out-of-range values EXPLICITLY rather than letting them fail open: `agreement < NaN` is always
    False, so an unchecked NaN/zero/negative threshold would commit a scattered claim. Fail closed."""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError(f"threshold must be a real number in (0, 1], got {threshold!r}")
    if not math.isfinite(threshold) or not (0.0 < threshold <= 1.0):
        raise ValueError(f"threshold must be finite and in (0, 1], got {threshold!r}")

#: vote sentinels — a sample that did NOT cast a single clean vote for a source claim. Both stay in
#: the denominator (k) but can never WIN, so they can only push a claim toward abstention. They are
#: control-prefixed so a real interpretation label can never collide with them.
_VOTE_ABSENT = "\x00absent"          # the sample did not perceive this source claim at all
_VOTE_CONFLICTED = "\x00conflicted"  # the sample emitted >=2 DIFFERENT interpretations of it


@dataclass(frozen=True)
class TypedScalarDecision:
    """Committed-or-abstained verdict for ONE source claim (`source_key`), with the full
    deterministic vote trail so abstention is observable (never a silent skip). `proposal` is the
    representative interpretation when committed — it carries the typed value, located span, episode
    provenance, and raw subject_text that C.4.3 binding + persistence consume; None on an abstention.
    No persistence happens here."""

    source_key: str
    committed: bool
    reason: str
    veto: str
    agreement: float
    k: int
    distribution: dict[str, int]
    proposal: TypedScalarProposal | None = None
    evidence_tier: str = PERCEPTION_EVIDENCE_TIER


def _interpretation_label(
    p: TypedScalarProposal, *, include_attribute: bool = True,
    include_scope: bool = True, include_subject: bool = True,
    canonical_self: bool = False,
) -> str:
    """The vote key WITHIN a source claim: the fully-interpreted reading, independent of source
    LOCATION (that is the `source_key`) but INCLUDING subject and effective world-time. Two samples
    agree only if they read the SAME subject, slot, operation, value, AND when â€” agreement on a value
    with a different date is NOT agreement on the same assertion. subject_text is normalized here
    (strip+lower) purely for the vote; the representative proposal keeps its raw subject_text for
    C.4.3 binding. Joined with a control separator so free-text subject_text cannot forge a field
    boundary.

    The three IDENTITY fields -- subject, attribute, scope -- can each be dropped from the vote and
    reconciled after grouping (see `gate_typed_scalars`). They are separable switches because they
    carry different risk, but they describe ONE defect: the model distributes a fact's identity
    across them in whatever order it likes. Read the residual gate losses and the same span comes
    back decomposed three ways --

        attr='watched'            scope='last_3_months'
        attr='watched'            scope='mcu_films'
        attr='mcus_watched_count' scope=''

    -- with subject, value, unit, operation, `when` and the span itself identical. Exact-matching
    each field independently turns one agreed fact into three single-vote claims. Measured on the
    LME panel: the attribute name ALONE vetoed ~11% of trials where all k samples emitted the asked
    value against the same episode; scope accounts for most of what survives that.

    `canonical_self` folds first-person subjects to `SELF_SUBJECT_DISPLAY` via the same
    `_is_self_reference` predicate the BINDER uses. Without it the vote key contradicts the binder:
    two samples that both correctly identify the self, one writing "I" and one writing "user", are
    counted as disagreeing about the subject even though binding resolves them to one entity.

    value_kind / unit / operation / when are never dropped -- they come from constrained
    vocabularies where disagreement is a genuine difference of reading, and dropping them measured
    worth ~0-2% each. `operation` in particular MUST stay: "I've added 25 postcards" (delta) and "I
    have 25 postcards" (absolute) fold into completely different Views."""
    subject = p.subject_text.strip().lower()
    if canonical_self and _is_self_reference(subject):
        subject = SELF_SUBJECT_DISPLAY
    fields = []
    if include_subject:
        fields.append(subject)
    if include_attribute:
        fields.append(p.attribute)
    if include_scope:
        fields.append(p.scope)
    fields.extend((
        p.value_kind, p.unit, p.operation,
        p.normalized_value,
        p.when or "",
    ))
    return "\x1f".join(fields)


def parse_scalar_row(
    row: Any, episodes: list[Any], drop: "Callable[[str], None]",
) -> "TypedScalarProposal | None":
    """Validate ONE raw model row into a grounded proposal, or None with a `drop` reason.

    Lifted verbatim out of `extract_typed_scalars_once` so experimental extraction protocols (see
    .agent/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md) admit rows through the
    IDENTICAL rules -- required fields, canonical attribute, kind-typed value, unique span grounding,
    hedged-value abstention, temporal disposition. A second implementation of these rules would drift
    from this one and silently make experiment arms incomparable to the baseline they are scored
    against.

    Pure. `drop` is called at most once, immediately before returning None.
    """
    if not isinstance(row, dict):
        drop("not_an_object")
        return None
    # required non-blank strings â€” no defaults, no coercion from non-string types.
    subject_text = _req_str(row, "subject")
    attribute = _req_str(row, "attribute")
    operation = _req_str(row, "operation")
    stated_span = _req_str(row, "stated_span")
    if subject_text is None or attribute is None or operation is None or stated_span is None:
        drop("missing_required_field")
        return None
    attribute = attribute.lower()
    operation = operation.lower()
    if not _SNAKE_RE.match(attribute) or operation not in OPERATIONS:
        drop("bad_attribute_or_operation")
        return None
    attribute = _canon_attribute(attribute, stated_span)
    value_kind = _req_str(row, "value_kind")
    if value_kind is None or value_kind.lower() not in VALUE_KINDS:
        drop("bad_value_kind")
        return None
    value_kind = value_kind.lower()

    # optional textual fields: '' when absent, dropped when present-but-non-string.
    scope = _opt_str(row, "scope")
    unit = _opt_str(row, "unit")
    display = _opt_str(row, "display")
    if scope is None or unit is None or display is None:
        drop("bad_optional_field")
        return None

    idx = _req_episode_index(row, len(episodes))
    if idx is None:
        drop("bad_episode_index")
        return None
    episode_uuid = str(getattr(episodes[idx], "uuid", "") or "").strip()
    if not episode_uuid:
        drop("unresolvable_episode")
        return None  # unresolvable provenance -> cannot ground

    normalized_frequency = (
        _normalize_interval_frequency(stated_span)
        if value_kind == "frequency" and operation != "delta"
        else None
    )
    normalized_duration, duration_was_normalized = (
        _normalize_colon_duration_value(row.get("value"), operation)
        if value_kind == "duration"
        else (row.get("value"), False)
    )
    if normalized_frequency is not None:
        value, unit = normalized_frequency
    elif duration_was_normalized:
        value = normalized_duration
        unit = "seconds"
    else:
        value = _coerce_value(value_kind, operation, row.get("value"))
    try:
        validate_value(value_kind, operation, value)
    except (ValueError, OverflowError):
        drop("value_failed_validation")
        # not well-typed for its kind -> drop. OverflowError is defense-in-depth: the domain
        # validator already fails an oversized int closed with ValueError, but catching it here
        # too guarantees one malformed proposal can never abort the whole extraction pass.
        return None
    if value_kind == "clock_time":
        # canonicalize to zero-padded HH:MM so "7:30" and "07:30" are ONE value, not vote scatter
        # at the C.4.2 interpretation key. Safe: validate_value has proven it a real 00:00-23:59.
        hh, mm = str(value).strip().split(":")
        value = f"{int(hh):02d}:{int(mm):02d}"

    when_ok, when = _normalize_when(row)
    if not when_ok:
        drop("malformed_when")
        return None  # a supplied-but-malformed date must not reach valid_at

    content = str(getattr(episodes[idx], "content", "") or "")
    span = _ground_span(content, stated_span)
    if span is None:
        drop("span_not_uniquely_located")
        return None  # not uniquely located in the source -> ungrounded/ambiguous -> does not exist
    span_start, span_end = span

    # Activity-count rows need one additional source-shape guard.  The model may correctly fill the
    # typed schema for a hypothetical total or a single event, but those are not durable cumulative
    # scalars.  Use only the punctuation-delimited clause prefix ending at the grounded span so a
    # modal in trailing text cannot veto an otherwise valid observation.
    activity_rejection = _activity_scalar_admission_reason(
        attribute=attribute,
        value_kind=value_kind,
        operation=operation,
        source_context=_activity_source_context(content, span_start, span_end),
        event_context=_activity_clause_context(content, span_start, span_end),
    )
    if activity_rejection is not None:
        drop(activity_rejection)
        return None

    # Hedged-value abstention: an underdetermined statement ("about 30 or 40", "around 7, sometimes
    # 7:30") must not become an exact scalar. Drop it here (span-local + deterministic) so the gate
    # sees no vote for it and the claim abstains, rather than admitting an over-confident value the k
    # samples happened to agree on. A genuine [lo, hi] range value is exempt.
    if is_ambiguous_exact(stated_span, value, operation):
        drop("hedged_value")
        return None

    # When-discipline: deterministically resolve temporal grounding from the span. A past-only,
    # unresolvable, bounded, or model-conflicting claim is DROPPED (never votes / persists). When an
    # explicit source date resolves, `when` becomes that SOURCE-derived timestamp (never the model's);
    # otherwise `when` is None and bind falls to the episode reference -- so a hallucinated model date
    # can never reach valid_at, on any operation (incl expire). All k samples decide identically.
    _ep_ref = getattr(episodes[idx], "reference_time", None)
    _disp, _keep, when = resolve_temporal_disposition(stated_span, when, operation, _ep_ref)
    if not _keep:
        drop(f"temporal_{_disp}")
        return None

    return TypedScalarProposal(
        subject_text=subject_text,
        attribute=attribute,
        scope=_canon_modifier(scope),
        value_kind=value_kind,
        unit=_canon_modifier(unit),
        operation=operation,
        value=value,
        stated_span=stated_span,
        episode_uuid=episode_uuid,
        span_start=span_start,
        span_end=span_end,
        when=when,
        display=display,
        claim_ordinal=0,
    )


def _reconciled_identity(
    members: "list[TypedScalarProposal]", fields: "tuple[str, ...]",
) -> "tuple[str, ...]":
    """Pick ONE value for each of `fields` for a group of proposals that agreed on everything else.

    Votes on the field COMBINATION rather than each field independently, because the fields are not
    independent: `attr='watched' scope='mcu_films'` and `attr='mcus_watched_count' scope=''` are two
    spellings of one slot, and choosing the modal attribute and the modal scope separately can
    synthesize a pair (`mcus_watched_count` + `mcu_films`) that NO sample proposed. Voting on the
    tuple also guarantees a representative proposal carrying the winning combination exists, which
    keeps `slot_key` internally coherent.

    Modal first â€” if two of three samples said `postcard_count`, that is the reading. Ties break to
    the LONGEST combination, then lexicographically. Length is a deliberate bias toward specificity:
    when samples split 1-1-1 between `count`, `postcard_count`, and `zebra_count`, all three are
    truthful but `count` is a strictly worse ScalarStateView slot -- it collides with every other
    tally the subject owns. Both tie-breaks are pure functions of the candidate SET, never of sample
    order, so the chosen slot cannot change when the same samples arrive in a different sequence."""
    votes = Counter(tuple(str(getattr(p, f)) for f in fields) for p in members)
    top = max(votes.values())
    return sorted(
        (combo for combo, n in votes.items() if n == top),
        key=lambda c: (-sum(len(v) for v in c), c),
    )[0]


def _reconciled_attribute(members: "list[TypedScalarProposal]") -> str:
    """Attribute-only reconciliation. Retained as the published single-field entry point; the
    behaviour is `_reconciled_identity` restricted to one field."""
    return _reconciled_identity(members, ("attribute",))[0]


def _claim_groups(
    samples: "list[list[TypedScalarProposal]]", *, align_spans: bool,
) -> "list[list[tuple[int, TypedScalarProposal]]]":
    """Partition the k samples' proposals into CLAIM groups, in deterministic first-seen order.

    Default (`align_spans=False`): group by exact `source_key`, the durable claim locator. Two
    samples that quoted the same fact with spans differing by one character are then two claims, each
    seen by one sample, and each vetoed for want of k votes.

    `align_spans=True`: group by same episode + a COMMON span intersection, at most one proposal per
    sample. Common intersection rather than chained pairwise overlap -- chaining drags
    non-overlapping A and C together through a long B that touches both. Ambiguous groups (two
    proposals from ONE sample in the same component) are left ungrouped, so the worst case is the
    status quo, never a wrong merge. Singletons pass through unchanged."""
    flat = [(si, p) for si, sample in enumerate(samples) for p in sample]
    if not align_spans:
        order: list[str] = []
        by_key: dict[str, list[tuple[int, TypedScalarProposal]]] = {}
        for si, p in flat:
            if p.source_key not in by_key:
                by_key[p.source_key] = []
                order.append(p.source_key)
            by_key[p.source_key].append((si, p))
        return [by_key[k] for k in order]

    by_episode: dict[str, list[int]] = defaultdict(list)
    for i, (_si, p) in enumerate(flat):
        by_episode[p.episode_uuid].append(i)

    groups: list[list[tuple[int, TypedScalarProposal]]] = []
    grouped: set[int] = set()
    for _episode, idxs in by_episode.items():
        edges: dict[int, set[int]] = defaultdict(set)
        for a in idxs:
            sa, pa = flat[a]
            for b in idxs:
                if b <= a:
                    continue
                sb, pb = flat[b]
                if sa == sb:  # a sample never overlaps itself into one claim
                    continue
                if pa.span_start < pb.span_end and pb.span_start < pa.span_end:
                    edges[a].add(b)
                    edges[b].add(a)
        seen: set[int] = set()
        for start in idxs:
            if start in seen:
                continue
            stack, component = [start], []
            seen.add(start)
            while stack:
                cur = stack.pop()
                component.append(cur)
                for nb in edges.get(cur, ()):
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            if len(component) < 2:
                continue
            spans = [(flat[i][1].span_start, flat[i][1].span_end) for i in component]
            sample_ids = [flat[i][0] for i in component]
            if max(s for s, _ in spans) >= min(e for _, e in spans):
                continue  # no span common to ALL members
            if len(set(sample_ids)) != len(sample_ids):
                continue  # a sample appears twice -- ambiguous, leave it alone
            common_start = max(s for s, _ in spans)
            common_end = min(e for _, e in spans)
            verifiable_text: str | None = None
            for i in component:
                proposal = flat[i][1]
                if len(proposal.stated_span) != proposal.span_end - proposal.span_start:
                    continue
                offset = common_start - proposal.span_start
                verifiable_text = proposal.stated_span[offset:offset + (common_end - common_start)]
                break
            if verifiable_text is not None and not re.search(r"\w", verifiable_text, re.UNICODE):
                continue  # punctuation/whitespace overlap is not a grounded claim anchor
            groups.append([(flat[i][0], flat[i][1]) for i in sorted(component)])
            grouped.update(component)
    for i, (si, p) in enumerate(flat):
        if i not in grouped:
            groups.append([(si, p)])
    return groups


def _canonical_aligned_proposal(
    proposal: TypedScalarProposal,
    candidates: list[TypedScalarProposal],
) -> TypedScalarProposal:
    """Return the winner grounded to the deterministic intersection shared by its quote variants.

    The intersection is independent of sample order and is still an exact source substring, so one
    k-sample decision cannot pick a different durable ``source_key`` merely because sample order
    changed. Directly-constructed test proposals may carry synthetic offsets that do not match their
    quote length; those use the shortest candidate's location while preserving the already-selected
    winning interpretation.
    """
    if len(candidates) < 2:
        return proposal
    start = max(candidate.span_start for candidate in candidates)
    end = min(candidate.span_end for candidate in candidates)
    fallback = min(
        candidates,
        key=lambda candidate: (
            candidate.span_end - candidate.span_start,
            candidate.span_start,
            candidate.span_end,
            candidate.source_key,
        ),
    )
    if start >= end:
        return replace(
            proposal,
            stated_span=fallback.stated_span,
            span_start=fallback.span_start,
            span_end=fallback.span_end,
        )
    for candidate in sorted(candidates, key=lambda item: item.source_key):
        if len(candidate.stated_span) != candidate.span_end - candidate.span_start:
            continue
        offset = start - candidate.span_start
        quote = candidate.stated_span[offset:offset + (end - start)]
        if quote and re.search(r"\w", quote, re.UNICODE):
            return replace(proposal, stated_span=quote, span_start=start, span_end=end)
    return replace(
        proposal,
        stated_span=fallback.stated_span,
        span_start=fallback.span_start,
        span_end=fallback.span_end,
    )


def _readable_vote(label: str) -> str:
    """Render one vote key for the audit trail. Pure; used only for observability.

    `_interpretation_label` joins its 8 fields with \\x1f, and non-votes are the \\x00-prefixed
    sentinels. Both are control characters that survive JSON but are unreadable in a details blob,
    so votes become `subject|attribute|scope|kind|unit|op|value|when` and sentinels become plain
    words. Unknown shapes are returned unchanged rather than mangled.
    """
    if label == _VOTE_ABSENT:
        return "(absent)"
    if label == _VOTE_CONFLICTED:
        return "(conflicted)"
    return label.replace("\x1f", "|")


def gate_typed_scalars(
    samples: list[list[TypedScalarProposal]], *, threshold: float = 1.0,
    reconcile_attribute: bool = False, reconcile_scope: bool = False,
    reconcile_subject: bool = False, canonical_self: bool = False,
    align_spans: bool = False,
) -> list[TypedScalarDecision]:
    """SOURCE-CLAIM-FIRST k-sample consistency gate. For each distinct `source_key` seen across the k
    proposal samples, commit ONE interpretation only if it is concentrated:

      1. GROUP by `source_key` (the binding-stable claim locator), so unrelated unbound proposals â€”
         e.g. two episodes each stating a different subject owns 2 cats â€” stay separate claims and are
         never collapsed before C.4.3 resolves identity.
      2. Within a source claim, each sample casts AT MOST ONE vote for its `interpretation_label`
         (subject + slot + operation + value + when). A sample that read the claim TWO different ways
         is internally conflicted and casts NO vote (`_VOTE_CONFLICTED`); a sample that did not
         perceive the claim at all is `_VOTE_ABSENT`. Neither can win, but both remain in the
         denominator â€” the denominator is always the configured `k`, so omissions and conflicts count
         AGAINST agreement rather than vanishing from it.
      3. `agreement` = (votes for the modal real interpretation) / k. Commit iff `agreement >=
         threshold` (default 1.0 = unanimous). Sentinels are excluded from being the winner but not
         from `k`.
      4. Defense-in-depth: never commit a winner without a located source span (C.4.1 already
         guarantees this, so it should not fire â€” but it fails closed if a caller injects proposals
         directly).

    Returns one `TypedScalarDecision` per source claim (first-seen order, deterministic), each with
    its full vote distribution so abstention is observable. Committed decisions carry the
    representative `proposal` for C.4.3; nothing is persisted or bound here. Pure and deterministic.

    Commits ONLY a UNIQUE modal interpretation: if two interpretations share the top real count, the
    winner is ambiguous and the claim abstains (`TS_VETO_TIE`) even if that count meets `threshold` â€”
    a 2-2 split must never resolve to whichever the sort happened to place first. `threshold` is
    validated to (0, 1] up front so a NaN/zero/negative value cannot fail open.

    TWO EXPLICIT RELAXATIONS default off at this pure boundary. The production perception service
    always requests conservative span grounding; the free-text identity reconciliations remain
    configured separately. Both address the same measured defect: on the LME panel the model emits
    the asked value in ~100% of namespace-trials and all k samples emit it in ~81%, but only 23%
    commit. Much of that loss is DISAGREEMENT ABOUT THE KEY rather than about the value.

    CORRECTION -- an earlier revision of this docstring claimed lowering `threshold` recovered
    nothing. That was a measurement error: the sweep used 0.67, and 2/3 = 0.6666... is strictly less
    than 0.67, so a 2-of-3 majority could never clear it and every configuration scored identically.
    Re-measured at exactly 2/3 on the same frozen panel, recovery of the asked value is:

                        threshold 1.0   threshold 2/3
        both off                   23              51
        reconcile_attribute        34              72
        align_spans                33              54
        both                       45              74

    So the unanimity requirement is the LARGEST single lever, larger than either relaxation, and the
    two are complementary rather than alternatives. It is not free: commits rise 64 -> 298 while the
    share matching the benchmark's labelled current value falls 41% -> 32%, and slots holding two
    contradictory values in one pass rise 22 -> 68. `threshold` is a validated caller argument,
    supplied by `personal_memory_scalar_threshold` (default 1.0).

      * `reconcile_attribute` â€” drop the free-text attribute NAME from the vote and choose it after
        grouping (`_reconciled_attribute`). Samples routinely agree on subject/slot/value/when and
        disagree only on what to call the attribute. Replayed on the frozen panel: 23 -> 34 of 100.
      * `align_spans` â€” group claims by common span intersection instead of exact `source_key`, so a
        one-character difference in the quoted span is no longer two separate single-vote claims.
        Replayed: 23 -> 33 of 100; with both, 45 of 100.
        The committed span is the exact common source substring, independent of sample order.
        Re-perception with a materially different set of quote boundaries can still select a
        different substring, so callers must retain normal namespace reset/idempotency discipline."""
    _validate_threshold(threshold)
    k = len(samples)
    if k == 0:
        return []

    # Partition into claim groups, then index each group as a per-sample SET of distinct
    # interpretation labels. rep maps (group index, label) -> every proposal that read it, first-seen
    # first, so the committed representative is deterministic regardless of value type (37 vs 37.0
    # normalize equal) and attribute reconciliation has the full candidate set to choose from.
    groups = _claim_groups(samples, align_spans=align_spans)
    # The identity fields dropped from the vote, in label order, reconciled together afterwards.
    reconciled_fields = tuple(field for field, enabled in (
        ("subject_text", reconcile_subject),
        ("attribute", reconcile_attribute),
        ("scope", reconcile_scope),
    ) if enabled)
    per_source: list[list[set[str]]] = [[set() for _ in range(k)] for _ in groups]
    rep: dict[tuple[int, str], list[TypedScalarProposal]] = defaultdict(list)
    for gi, members in enumerate(groups):
        for si, p in members:
            label = _interpretation_label(
                p,
                include_attribute=not reconcile_attribute,
                include_scope=not reconcile_scope,
                include_subject=not reconcile_subject,
                canonical_self=canonical_self,
            )
            per_source[gi][si].add(label)
            rep[(gi, label)].append(p)

    decisions: list[TypedScalarDecision] = []
    for gi, members in enumerate(groups):
        sk = members[0][1].source_key
        votes: list[str] = []
        for interps in per_source[gi]:
            if len(interps) == 0:
                votes.append(_VOTE_ABSENT)
            elif len(interps) == 1:
                votes.append(next(iter(interps)))
            else:
                votes.append(_VOTE_CONFLICTED)  # >=2 readings in one sample -> no clean vote
        dist = Counter(votes)

        # the winner is the top REAL interpretation (sentinels can dilute but never win).
        real = [(lbl, c) for lbl, c in dist.most_common() if lbl not in (_VOTE_ABSENT, _VOTE_CONFLICTED)]
        top_label, top_n = (real[0] if real else (None, 0))
        agreement = top_n / k

        if top_label is None or agreement < threshold:
            decisions.append(TypedScalarDecision(
                source_key=sk, committed=False,
                reason=f"scattered: modal interpretation holds {top_n}/{k} (threshold {threshold})",
                veto=TS_VETO_SELF_CONSISTENCY, agreement=agreement, k=k, distribution=dict(dist),
            ))
            continue

        # unique-winner guard: a tie at the modal count has no unambiguous interpretation, so abstain
        # even when that count meets threshold (a 2-2 split at threshold=0.5 must NOT commit whichever
        # the sort ordered first). Only reached when agreement >= threshold, so it never masks scatter.
        if len(real) >= 2 and real[1][1] == top_n:
            decisions.append(TypedScalarDecision(
                source_key=sk, committed=False,
                reason=f"tie: {len(real)} interpretations share the modal {top_n}/{k}",
                veto=TS_VETO_TIE, agreement=agreement, k=k, distribution=dict(dist),
            ))
            continue

        # All winners read the claim identically on every voted field; when `attribute` was excluded
        # from that vote they may still differ on the NAME, so reconcile it and commit the proposal
        # that actually carries the winning name -- its slot_key must stay internally coherent.
        candidates = rep[(gi, top_label)]
        proposal = candidates[0]
        if reconciled_fields:
            winning = _reconciled_identity(candidates, reconciled_fields)
            proposal = next(
                p for p in candidates
                if tuple(str(getattr(p, f)) for f in reconciled_fields) == winning
            )
        if align_spans:
            proposal = _canonical_aligned_proposal(proposal, candidates)
        # Under span alignment the proposal is grounded to the deterministic common source span, so
        # the durable identity is independent of which sample happened to be seen first.
        sk = proposal.source_key

        if proposal.span_start < 0 or proposal.span_end < 0:  # defense-in-depth (C.4.1 guarantees >=0)
            decisions.append(TypedScalarDecision(
                source_key=sk, committed=False,
                reason="ungrounded: winning interpretation has no located source span",
                veto=TS_VETO_UNGROUNDED, agreement=agreement, k=k, distribution=dict(dist),
            ))
            continue

        decisions.append(TypedScalarDecision(
            source_key=sk, committed=True,
            reason="unanimous" if agreement >= 1.0 else f"concentrated {agreement:.2f}",
            veto=TS_VETO_COMMIT, agreement=agreement, k=k, distribution=dict(dist),
            proposal=proposal,
        ))
    return decisions


# ------------------------------------------------------- (3) binding + persistence + rebuild (C.4.3)
#
# A committed `TypedScalarDecision` is still UNBOUND: its representative proposal carries the raw
# `subject_text` the model extracted, not a resolved entity. Binding happens POST-FINALIZATION —
# after Graphiti has correlated/merged the episode's entities — so we bind against the SURVIVING
# entities linked to the episode, never a pre-merge guess. The rule is fail-closed: a UNIQUE
# name match is authority (persist a fully-bound assertion that can materialize a View); ZERO or
# MULTIPLE matches abstain from authority (persist an ADVISORY event-log entry with a sentinel
# subject the store flags `binding_pending`, so NO display-text-keyed View is ever built — that
# would recreate exactly the lexical sidecar this design rejected).

#: advisory (unbindable) assertions carry a deterministic, per-source-claim sentinel subject_uuid.
#: It (a) satisfies TypedAssertion's non-blank identity rule, (b) can never collide with a real
#: Graphiti Entity uuid, so the repository's write-time `Entity IS NULL` check sets
#: `binding_pending=true` and the fold (materializable_only) never turns it into a UUID-keyed View,
#: and (c) is source_key-stable, so re-perceiving the same unbindable claim lands on the SAME head
#: rather than forking a duplicate. A later orphan-rebind pass (C.4.4) resolves it to a survivor.
_ADVISORY_UUID_PREFIX = "unbound:"


def advisory_subject_uuid(source_key: str) -> str:
    """Deterministic sentinel subject_uuid for an assertion that could not be uniquely bound."""
    return f"{_ADVISORY_UUID_PREFIX}{source_key}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: correction cues (lowercase), matched span-locally against the grounded stated_span ONLY (never the
#: wider episode), so a conversational "actually" elsewhere never grants correction authority. Kept to
#: UNAMBIGUOUS phrases -- a false correction grants unearned authority, whereas a missed one merely
#: leaves the tie AMBIGUOUS_ANCHOR (safe), so precision beats recall here. `_CORRECTION_NOT_RE` matches
#: the "not X, Y" shape (e.g. "not 180, 182") without the false positives of a bare "not" substring.
_CORRECTION_CUES: tuple[str, ...] = (
    "actually,", "actually ", "correction:", "i meant", "meant to say", "i was wrong",
    "let me correct", "to correct that", "i misspoke",
)
_CORRECTION_NOT_RE = re.compile(r"\bnot\s+\S+\s*,")


def classify_absolute_semantics(stated_span: str, operation: str = "absolute") -> str:
    """DETERMINISTICALLY classify an absolute's framing from its grounded source span -- 'correction'
    when the span itself carries an explicit correction cue, else 'ordinary'. Pure code (NOT an LLM
    output), a function of the span alone, so all k samples of one claim classify identically and the
    modifier can stay OUT of assertion identity. Only `absolute` operations can be corrections; deltas
    and expires are always 'ordinary'. Conservative on purpose: a missed correction merely leaves the
    tie AMBIGUOUS_ANCHOR (safe), whereas a false correction would grant unearned authority."""
    if operation != "absolute":
        return "ordinary"
    span = (stated_span or "").strip().lower()
    if not span:
        return "ordinary"
    if any(cue in span for cue in _CORRECTION_CUES) or _CORRECTION_NOT_RE.search(span):
        return "correction"
    return "ordinary"


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


#: Exact first-person self tokens (C.4.3 canonical-self binding). The extraction PROMPT already
#: normalizes "I"/"me"/"my" to subject="user" (see the extract instruction), so "user" is the common
#: case; the rest are defensive. This is an EXACT allowlist, NEVER a prefix — a possessed object like
#: "my car" (subject="my car") and every NAMED third party fall through to ordinary entity binding and
#: can therefore never bind to self.
SELF_TOKENS = frozenset({"user", "the user", "i", "me", "myself"})

#: Canonical display for a self-bound subject. Matches the extraction prompt's subject convention so a
#: materialized View reads naturally ("user's coins: 37"); the authority is the bound self UUID.
SELF_SUBJECT_DISPLAY = "user"

#: A seam that maps a namespace to its ONE canonical self subject: returns (subject_uuid, display) for
#: the namespace's stable self :Entity (ensuring it exists), or None when self-binding is unavailable
#: (e.g. no namespace). Injected so the pure binders stay offline-testable.
ResolveSelfSubject = Callable[["str | None"], "tuple[str, str] | None"]


def _is_self_reference(subject_text: str) -> bool:
    """True when `subject_text` denotes the first-person self (the namespace's canonical user)."""
    return subject_text.strip().lower() in SELF_TOKENS


#: Leading determiners the perceiver sometimes keeps on an extracted subject where the resolved
#: entity does not carry them ("my boots" against an entity named "boots"). Stripped only as a
#: FALLBACK spelling of the query, never on the first attempt, and never enough on its own to make a
#: match non-unique -- see `_bind_subject`.
_SUBJECT_DETERMINERS = ("my ", "your ", "our ", "his ", "her ", "their ", "its ", "the ")
#: The POSSESSIVE subset of `_SUBJECT_DETERMINERS`. Incidental ``new`` is stripped ONLY after one of
#: these ("my new black shoes" -> "black shoes"); the plain article "the" never triggers a ``new``
#: strip ("the new house" keeps "new house"), because "the <new> <name>" is far more often a proper
#: name than a possessed object, and collapsing it risks the same referent-ambiguity the fail-closed
#: binder exists to refuse.
_POSSESSIVE_DETERMINERS = ("my ", "your ", "our ", "his ", "her ", "their ", "its ")


#: A seam that resolves a subject against entities OUTSIDE the current episode — the same-namespace
#: repository — by a bounded list of normalized subject spellings. Injected so the pure binder stays
#: offline-testable. Returns candidate `{uuid, name}` ROWS (the repository already enforces
#: same-namespace/group_id, nonblank uuid, and excludes derived Views); the caller applies the SAME
#: unique fail-closed binder to them. None/absent means the fallback is disabled (local-only binding).
LookupNamespaceEntities = Callable[["str | None", "list[str]"], "list[dict[str, Any]]"]


def _subject_variants(target: str) -> list[str]:
    """Fallback spellings of an extracted subject, in priority order, excluding `target` itself.

    Two syntactic normalizations only, both of which rewrite HOW the same subject is written rather
    than WHICH subject is meant:
      * drop a leading determiner ("my boots" -> "boots")
      * a CONSTRAINED POSSESSIVE + incidental ``new``: only when a leading POSSESSIVE determiner was
        dropped ("my new black shoes") does a following ``new`` become optional, yielding "new black
        shoes" then "black shoes". The ``new``-keeping spelling is always emitted BEFORE the
        ``new``-stripped one, ``new`` is NEVER stripped from the bare target (an arbitrary name like
        "New York" with no determiner is untouched), and it is NOT stripped after the plain article
        "the new house" (which stays "new house") — the article case is far more often a proper name.
      * treat `_`/`-` as word separators ("shift_schedule" -> "shift schedule"), which is the
        perceiver emitting a slot key where a surface form belongs

    Deliberately NOT stemming, synonyms, or substring matching: those change the referent and would
    reintroduce exactly the ambiguity the fail-closed rule exists to refuse."""
    seen: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate != target and candidate not in seen:
            seen.append(candidate)

    stripped = target
    for determiner in _SUBJECT_DETERMINERS:
        if target.startswith(determiner):
            stripped = target[len(determiner):].strip()
            _add(stripped)
            # Constrained possessive + incidental "new": only after a POSSESSIVE determiner was
            # dropped, so "the new house" keeps "new house" and a bare "New York" is untouched.
            if determiner in _POSSESSIVE_DETERMINERS and stripped.startswith("new "):
                _add(stripped[len("new "):])
            break
    for base in (target, stripped):
        if "_" in base or "-" in base:
            _add(base.replace("_", " ").replace("-", " "))
    return seen


def _subject_spellings(subject_text: str) -> list[str]:
    """The bounded, priority-ordered list of NORMALIZED spellings used for an exact repository lookup:
    the target itself first, then its syntactic variants, deduplicated. No fuzzy/substring/stem/
    synonyms — every spelling is an exact normalized name form, and the binder still requires a unique
    match."""
    target = subject_text.strip().lower()
    if not target:
        return []
    out = [target]
    for variant in _subject_variants(target):
        if variant not in out:
            out.append(variant)
    return out


def _bind_from_candidates(
    subject_text: str, candidates: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """The UNIQUE fail-closed binder over a candidate row set: match each normalized spelling, in
    priority order, by exact name equality; bind on a single match with a nonblank uuid; abstain
    (None, None) on zero, multiple, or a blank-uuid match. `candidates` is either the episode's linked
    entities or the result of a same-namespace repository lookup — both are fed through this SAME
    binder so authority never depends on which candidate source won."""
    target = subject_text.strip().lower()
    if not target:
        return None, None

    def _unique(candidate: str) -> tuple[str | None, str | None]:
        matches = [
            e for e in candidates
            if str(e.get("name", "") or "").strip().lower() == candidate
        ]
        if len(matches) != 1:
            return None, None
        uuid = str(matches[0].get("uuid") or "").strip()
        if not uuid:
            return None, None
        return uuid, (str(matches[0].get("name") or "").strip() or subject_text)

    uuid, display = _unique(target)
    if uuid is not None:
        return uuid, display

    for variant in _subject_variants(target):
        if variant in SELF_TOKENS:
            continue
        uuid, display = _unique(variant)
        if uuid is not None:
            return uuid, display
    return None, None


def _bind_subject(
    subject_text: str, entities: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Bind `subject_text` to a UNIQUE surviving entity by exact normalized-name equality. Returns
    (subject_uuid, subject_display) on a single match; (None, None) on zero OR multiple (abstain from
    authority) OR a match whose uuid is blank. Deliberately NOT fuzzy: asserting authority on an
    ambiguous or approximate identity is precisely what the fail-closed binding rule forbids.

    The exact spelling is tried FIRST and always wins, so this is behaviour-preserving for every
    subject that already bound. Only when it matches nothing do syntactic variants
    (`_subject_variants`) get a turn, and each variant must ITSELF produce exactly one match --
    ambiguity in a fallback abstains just as it does in the primary. A variant that resolves to a
    first-person token is refused outright: self binds through the canonical-self seam and MUST NOT
    be reachable by lexical name-match, or a per-episode `user` node could become the self subject."""
    return _bind_from_candidates(subject_text, entities)


def _resolve_subject(
    subject_text: str, entities: list[dict[str, Any]], namespace: str | None,
    resolve_self_subject: ResolveSelfSubject | None,
    lookup_namespace_entities: LookupNamespaceEntities | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a subject to (subject_uuid, subject_display), preferring the canonical self identity.

    Ordering is deliberately conservative and additive:
      1. CANONICAL SELF first: a first-person subject binds to the namespace's ONE stable self entity
         via the injected `resolve_self_subject` seam — never to a per-episode node and never through
         lexical name-match, so replay is idempotent and no `I`/`user` entity is ever minted per
         episode. Named third parties never reach this branch (SELF_TOKENS is an exact allowlist).
      2. EXACT LOCAL EPISODE matching next: bind against the episode's SURVIVING linked entities by
         exact normalized-name equality (`_bind_from_candidates`). Authority is asserted ONLY on a
         unique local match.
      3. OPTIONAL same-namespace repository lookup, ONLY after the local pass fails: when
         `lookup_namespace_entities` is supplied AND `namespace` is nonblank, ask the repository for
         same-namespace entities matching the bounded normalized spellings, then feed those rows
         through the SAME unique fail-closed binder. No fuzzy/substring/stem/synonyms anywhere.

    When the self seam is absent, unavailable, or the subject is not first-person, and neither the
    local nor (when enabled) namespace pass yields a unique match, binding abstains to (None, None) —
    the caller records an advisory, never an unverified owner."""
    if resolve_self_subject is not None and _is_self_reference(subject_text):
        bound = resolve_self_subject(namespace)
        if bound is not None and bound[0]:
            return bound
        # CF-131: the seam was CONSULTED for a real namespace and failed (ensure_self_entity
        # raised and was swallowed). Refuse the lexical fallback -- self binds through the
        # canonical seam or not at all.
        #
        # Scoped to `namespace` truthiness on purpose, because that is exactly the condition
        # under which the fallback is dangerous. `_make_self_seam` returns None in two shapes:
        #
        #   ns is blank    -> it declines WITHOUT touching the adapter. ensure_self_entity never
        #                     runs, so _absorb_self_entity_forks never runs for that namespace,
        #                     so a per-episode `user` node is NOT doomed. Legacy lexical binding
        #                     stays, which is what test_resolve_falls_through_when_no_seam and
        #                     the coordinator tests pin.
        #   ns is present  -> the MERGE was attempted and failed. The self machinery IS live here,
        #                     so the next successful ensure_self_entity runs
        #                     _absorb_self_entity_forks, which ends in DETACH DELETE and orphans
        #                     any assertion bound to the twin onto a dead uuid. Refuse.
        if namespace:
            return None, None
    local = _bind_from_candidates(subject_text, entities)
    if local[0] is not None:
        return local
    if lookup_namespace_entities is not None and namespace:
        try:
            rows = lookup_namespace_entities(namespace, _subject_spellings(subject_text))
        except Exception:  # noqa: BLE001 - the repository fallback is best-effort; never crash binding
            rows = []
        namespace_match = _bind_from_candidates(subject_text, rows or [])
        if namespace_match[0] is not None:
            return namespace_match
    return None, None


def _assertion_from_proposal(
    p: TypedScalarProposal, *, subject_uuid: str, subject_display: str, valid_at: str,
    learned_at: str, time_basis: str, evidence_tier: str, namespace: str | None,
    perceiver_version: str, name_embedding: "list[float] | None" = None,
    embed_version: str | None = None,
) -> TypedAssertion:
    """Build the durable `TypedAssertion` from a committed proposal + its resolved (or sentinel)
    subject. The proposal's span/ordinal/episode flow through unchanged, so the assertion's
    binding-stable `source_key` is IDENTICAL to the one the proposal predicted (shared
    `build_source_key`) — the whole point of C.4.1's identity contract."""
    return TypedAssertion(
        subject_uuid=subject_uuid, subject_display=subject_display,
        attribute=p.attribute, scope=p.scope, value_kind=p.value_kind, unit=p.unit,
        operation=p.operation, value=p.value, stated_span=p.stated_span,
        episode_uuid=p.episode_uuid, valid_at=valid_at, learned_at=learned_at,
        span_start=p.span_start, span_end=p.span_end, claim_ordinal=p.claim_ordinal,
        time_basis=time_basis, evidence_tier=evidence_tier,
        perceiver_version=perceiver_version, namespace=namespace,
        # deterministic, span-local correction classification (NOT an LLM field; out of identity keys)
        absolute_semantics=classify_absolute_semantics(p.stated_span, p.operation),
        name_embedding=name_embedding, embed_version=embed_version,
    )
