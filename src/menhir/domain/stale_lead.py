"""Stale-lead check: a leading count must not outrank a newer count about the same thing.

A count View leads on a user foundation, but a later statement can land in another slot
("owned_bike_count" = 3 in Feb, "bike_purchase_count" in Oct) and never update it. The lead
would then tell the reader to prefer the stale value. A newer slot is related when its attribute
names every content noun of the lead's; the generic words below never relate slots on their own.
Known false positives on the o500 graphs: page / page_count, leadership_position_count /
women_leadership_position_count (different quantities of the same noun).
"""

from __future__ import annotations

import re

# Only counts: on shard 5 of the o500 run, every non-count match was a false positive
# (energy_level / cholesterol_level, gym_day / admin_day).
DEMOTABLE_VALUE_KINDS = frozenset({"count"})

_GENERIC = frozenset({
    "count", "total", "number", "num", "owned", "own", "owns", "current", "amount", "value",
    "the", "and", "for", "per", "with",
})
_SPLIT = re.compile(r"[^a-z0-9]+")


def attribute_nouns(attribute: str | None) -> frozenset[str]:
    """Content tokens of an attribute name: lowercased, crude singular, generic count words dropped."""
    out: set[str] = set()
    for token in _SPLIT.split((attribute or "").lower()):
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if len(token) >= 3 and token not in _GENERIC:
            out.add(token)
    return frozenset(out)


def related_attributes(lead_attribute: str | None, other_attribute: str | None) -> bool:
    """True when a different attribute names every content noun of the lead's attribute
    (bike -> bike_purchase; not reading_goal -> reading_session)."""
    if not lead_attribute or not other_attribute or lead_attribute == other_attribute:
        return False
    lead = attribute_nouns(lead_attribute)
    return bool(lead) and lead <= attribute_nouns(other_attribute)
