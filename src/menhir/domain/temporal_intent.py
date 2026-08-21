"""Temporal-intent detection for recall (Chronostratum Rung 2, transparent baseline).

A query carries temporal intent that should change which facts recall returns:

    "what is X now / currently"            -> CURRENT_BELIEF (expired_at IS NULL)
    "what did we believe / what broke ...  -> AS_KNOWN_AT (include invalidated; belief drift)
       before / used to / originally / was"
    "what was true as of <date>"           -> AS_OF_WORLD (world time at as_of)

This is the deterministic keyword baseline that decides the temporal lens + whether to
include invalidated beliefs. The LLM planner (menhir_agentic_recall) is the richer
variant; this is the falsifiable floor that needs no model. It feeds temporal.matches_query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from menhir.domain import temporal_lexicon
from menhir.domain.temporal import TemporalQuery

# Query-classifier extras. ON PURPOSE audience-specific -- do NOT merge them into the
# shared core (temporal_lexicon), or the TEXT classifier (facet_derivation) would start
# matching them. The shared historical markers are not contiguous here, so they are
# threaded back in at their original positions (see _history_cues) to keep the cue
# order -- and therefore which cue fires first for explainability -- byte-identical.
_HISTORY_EXTRAS_PREFIX: tuple[str, ...] = (
    "originally", "before", "what broke", "what did we believe", "what did i believe",
    "regress", "history", "historically", "earlier", "back when", "at the time", "former",
)
_HISTORY_EXTRAS_SUFFIX: tuple[str, ...] = ("stopped working", "since when")
_CURRENT_EXTRAS: tuple[str, ...] = ("today", "latest", "at present", "these days")


def _history_cues() -> tuple[str, ...]:
    """Full history-cue list with the shared core threaded at its original positions."""
    return (
        temporal_lexicon._SHARED_HISTORICAL_MARKERS[:2]
        + _HISTORY_EXTRAS_PREFIX
        + temporal_lexicon._SHARED_HISTORICAL_MARKERS[2:]
        + _HISTORY_EXTRAS_SUFFIX
    )


def _current_cues() -> tuple[str, ...]:
    return temporal_lexicon._SHARED_CURRENT_MARKERS + _CURRENT_EXTRAS


_HISTORY_CUES: tuple[str, ...] = _history_cues()
# Explicit current cues -> CURRENT_BELIEF (also the default).
_CURRENT_CUES: tuple[str, ...] = _current_cues()

# "as of <date>" / "on <date>" world-time cue.
_AS_OF_RE = re.compile(r"\b(?:as of|on|at)\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class TemporalIntent:
    query: TemporalQuery
    include_invalidated: bool   # surface expired beliefs (history/drift)?
    as_of: str | None = None
    cue: str | None = None      # the phrase that triggered the classification (explainability)


def classify_temporal_intent(text: str) -> TemporalIntent:
    """Classify a query's temporal lens from transparent keyword cues.

    Precedence: an explicit "as of <date>" wins (world-time); else a history cue selects
    AS_KNOWN_AT + include_invalidated; else default to current-belief. Deterministic and
    explainable — the matched cue is returned."""
    lowered = text.lower()

    m = _AS_OF_RE.search(text)
    if m:
        return TemporalIntent(TemporalQuery.AS_OF_WORLD, include_invalidated=True, as_of=m.group(1), cue=m.group(0))

    for cue in _history_cues():
        if cue in lowered:
            return TemporalIntent(TemporalQuery.AS_KNOWN_AT, include_invalidated=True, cue=cue)

    for cue in _current_cues():
        if cue in lowered:
            return TemporalIntent(TemporalQuery.CURRENT_BELIEF, include_invalidated=False, cue=cue)

    # Default: current belief (never leak history unless asked).
    return TemporalIntent(TemporalQuery.CURRENT_BELIEF, include_invalidated=False, cue=None)
