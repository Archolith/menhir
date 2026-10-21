"""Read-time event timing for counting occurrences (pure, no I/O).

An occurrence row's ``valid_at`` is the session time (time_basis ``episode_reference``), and its short
``stated_span`` often drops the time phrase. This recovers the event date from the source sentence around
the quote in the stored turn text, reusing the #155 grounded resolver. Nothing is stored.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.infrastructure.anchored_time import CUES
from menhir.services.event_history_perception import resolve_event_valid_time, sentence_context
from menhir.services.typed_scalar_rules import _ground_span

_SENTENCE_CAP = 240
_CUE_KINDS = ("explicit", "speech_relative", "vague")


@dataclass(frozen=True)
class OccurrenceTime:
    sentence: str | None = None
    event_at: str | None = None
    event_basis: str | None = None


def _has_cue(text: str) -> bool:
    return any(CUES[kind].search(text or "") for kind in _CUE_KINDS)


def _trim_around(sentence: str, quote: str) -> str:
    if len(sentence) <= _SENTENCE_CAP:
        return sentence
    idx = sentence.lower().find(quote.lower())
    if idx < 0:
        return sentence[:_SENTENCE_CAP]
    mid = idx + len(quote) // 2
    start = max(0, min(mid - _SENTENCE_CAP // 2, len(sentence) - _SENTENCE_CAP))
    return sentence[start:start + _SENTENCE_CAP].strip()


def occurrence_event_time(
    stated_span: str, turn_text: str | None, mentioned_at: str | None,
) -> OccurrenceTime:
    quote = (stated_span or "").strip()
    if not quote or not turn_text:
        return OccurrenceTime()
    located = _ground_span(turn_text, quote)
    if located is None:
        return OccurrenceTime()
    sentence = sentence_context(turn_text, located[0], located[1])
    if not sentence:
        return OccurrenceTime()
    valid_at, basis, _note = resolve_event_valid_time(None, sentence, mentioned_at)
    event_at = valid_at if basis == "explicit" else None
    event_basis = basis if basis == "explicit" else None
    shown: str | None = None
    if len(sentence) > len(quote) and _has_cue(sentence) and not _has_cue(quote):
        shown = _trim_around(sentence, quote)
    return OccurrenceTime(sentence=shown, event_at=event_at, event_basis=event_basis)
