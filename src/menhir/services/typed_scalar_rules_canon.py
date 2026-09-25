"""Canonicalization and row-parsing primitives for the typed-scalar boundary.

Attribute/scope/unit canonical forms, frequency-interval normalization, deterministic JSON
response parsing, required/optional row-field readers, strict ISO ``when`` parsing, and unique
span grounding. Moved verbatim from ``typed_scalar_rules``; the facade module re-exports every
name so existing import sites keep working unchanged.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

#: canonical snake_case for the REQUIRED `attribute` (state family): lowercase start letter, then
#: lowercase/digits/underscores. It must match or the proposal fails closed — we never silently
#: normalize a malformed state-family key, because it would fork durable slot identity. `scope` and
#: `unit` are OPTIONAL modifiers and are instead DETERMINISTICALLY normalized by `_canon_modifier`
#: (lowercase; runs of whitespace/hyphens -> single underscore), so "work days", "work-days" and
#: "work_days" collapse to ONE durable slot rather than three.
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MODIFIER_SEP_RE = re.compile(r"[\s\-]+")

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
        # Preserve decimal tokens until their value_kind is known. Money keeps Decimal exactly;
        # other numeric kinds are converted back to their historical int/float representation in
        # `_as_number`, so this does not broaden Decimal semantics beyond currency.
        data = json.loads(text, parse_float=Decimal)
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
