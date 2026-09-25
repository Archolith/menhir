"""Shared Cypher predicates and helpers for the consolidation query modules.

Lives outside the facade so the sibling mixin modules can import the lifecycle
protection predicates without going through ``consolidation_queries``, which
imports them right back. Every symbol here is re-exported by the facade.
"""

from __future__ import annotations

import re

from menhir.domain.retention import destructive_retention_allowed_cypher
from menhir.infrastructure.cypher import non_derived_view_cypher

# Bounded per run so the decay sweep can't stall indefinitely; the stable
# ORDER BY means successive runs make forward progress.
_DECAY_CANDIDATE_LIMIT = 500


def automatic_lifecycle_protection_cypher(variable: str = "n") -> str:
    """Exclude derived Views and evidence retained by a live FACT View.

    FACT Views are ``:Entity`` nodes; their evidence-to-View ``MENTIONS`` relationship is the
    authoritative automatic-retention signal.  Explicit erasure repositories intentionally do not
    use this predicate: authorized erasure retires dependent Views before deleting their evidence.
    """
    non_derived = non_derived_view_cypher(variable)
    return (
        f"{non_derived} "
        "AND NOT EXISTS { "
        f"MATCH ({variable})-[:MENTIONS]->(retaining_view:Entity) "
        "WHERE coalesce(retaining_view.is_view, false) "
        "AND coalesce(retaining_view.view_current, retaining_view.qs_current, true) "
        "AND NOT coalesce(retaining_view.retired, false) }"
    )


def harmful_automatic_mutation_allowed_cypher(variable: str = "n") -> str:
    """Guard destructive automation without changing beneficial lifecycle behavior."""

    return (
        f"{automatic_lifecycle_protection_cypher(variable)} "
        f"AND {destructive_retention_allowed_cypher(variable)}"
    )


def _content_overlap_ratio(left: str | None, right: str | None) -> float:
    """Compute simple Jaccard overlap ratio over lowercase token sets."""

    left_tokens = set(re.findall(r"[A-Za-z0-9_]+", (left or "").lower()))
    right_tokens = set(re.findall(r"[A-Za-z0-9_]+", (right or "").lower()))
    if not left_tokens and not right_tokens:
        return 1.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    intersection = left_tokens & right_tokens
    return len(intersection) / len(union)
