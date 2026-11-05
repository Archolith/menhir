"""Deterministic hint for count questions that are anchored on a reference event.

"How many properties did I view before making an offer on the townhouse?" counts the
properties viewed *before* the offer. The offer's own property is the anchor, not an
answer. This module spots that shape in the query text and returns a one-line hint.
It is pure: no I/O, no settings, no memory access.
"""

from __future__ import annotations

import re

_HOW_MANY = re.compile(r"\bhow\s+many\b", re.IGNORECASE)

_ANCHOR_NOUNS = (
    "offer|bid|deposit|purchase|booking|reservation|application|order|"
    "down\\s+payment|signing|closing"
)

# before/after/prior to + up to three filler words + anchor noun + preposition + object.
_ANCHOR = re.compile(
    r"\b(?:before|after|prior\s+to)\s+"
    r"(?P<phrase>(?:[\w'-]+\s+){0,3}?(?:" + _ANCHOR_NOUNS + r")\s+"
    r"(?:on|for|of|at)\s+[^?.!,;]+)",
    re.IGNORECASE,
)

_MAX_PHRASE_CHARS = 120


def count_anchor_hint(query: str | None) -> str | None:
    """Return a hint when ``query`` is a count question anchored on a reference event."""
    if not query or not _HOW_MANY.search(query):
        return None
    match = _ANCHOR.search(query)
    if match is None:
        return None
    phrase = " ".join(match.group("phrase").split())[:_MAX_PHRASE_CHARS].rstrip()
    if not phrase:
        return None
    return (
        f"This count is anchored on '{phrase}'. That event is the reference point: "
        "the item it concerns is not one of the items to count."
    )
