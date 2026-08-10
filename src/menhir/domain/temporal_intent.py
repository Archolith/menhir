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

from menhir.domain.temporal import TemporalQuery

# History / belief-drift / regression cues -> include invalidated, AS_KNOWN_AT lens.
_HISTORY_CUES = (
    "used to", "previously", "originally", "before", "what broke", "what did we believe",
    "what did i believe", "regress", "history", "historically", "earlier",
    "back when", "at the time", "former", "no longer", "stopped working", "since when",
)
# Explicit current cues -> CURRENT_BELIEF (also the default).
_CURRENT_CUES = ("now", "currently", "current", "today", "latest", "at present", "these days")

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

    for cue in _HISTORY_CUES:
        if cue in lowered:
            return TemporalIntent(TemporalQuery.AS_KNOWN_AT, include_invalidated=True, cue=cue)

    for cue in _CURRENT_CUES:
        if cue in lowered:
            return TemporalIntent(TemporalQuery.CURRENT_BELIEF, include_invalidated=False, cue=cue)

    # Default: current belief (never leak history unless asked).
    return TemporalIntent(TemporalQuery.CURRENT_BELIEF, include_invalidated=False, cue=None)
