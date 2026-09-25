"""Context-line rendering helpers.

Source/world time evidence lines and event-selection fail-closed verdict checks for the
context builder. Moved verbatim from ``context_builder.py``, which re-exports them.
"""

from __future__ import annotations

from menhir.domain.recall import EventAuthorityVerdict, ScoredMemory


def _source_time_lines(memory: ScoredMemory) -> list[str]:
    """Render source/world time without guessing from Menhir's belief-time clock."""
    facts = memory.temporal_facts
    if not facts:
        return ["  Source time: unknown."]

    lines = ["  Source-time evidence:"]
    for temporal_fact in facts:
        valid_at = temporal_fact.valid_at
        invalid_at = temporal_fact.invalid_at
        if valid_at and invalid_at:
            happened = f"{valid_at} through {invalid_at}"
        elif valid_at:
            happened = valid_at
        else:
            happened = "unknown"
        fact = temporal_fact.fact or "(supporting fact text unavailable)"
        belief_role = temporal_fact.temporal_role.replace("_", " ")
        lines.append(f"  - {happened} | {fact} | belief: {belief_role}")
    return lines


#: Gates that represent a failed event selection (no resolved object). Only these gates fail closed;
#: any other advisory gate is not inferred as unresolved.
_SELECTION_FAIL_CLOSED_GATES = frozenset(
    {"anchor", "ambiguity", "time", "scope", "no_candidate"}
)


def _event_selection_failed(verdict: EventAuthorityVerdict) -> bool:
    """True when an event advisory's selection itself failed to resolve an object.

    Fail-closed applies only to an ``advisory`` whose gate is a selection-failure gate
    (``anchor``, ``ambiguity``, ``time``, ``scope``, ``no_candidate``). Route/foundation/evidence
    advisories carry a resolved selection and stay advisory; a future advisory gate that is not a
    selection failure must not be inferred as unresolved from ``object_key`` alone.
    """
    return (
        verdict.status == "advisory"
        and verdict.gate in _SELECTION_FAIL_CLOSED_GATES
    )
