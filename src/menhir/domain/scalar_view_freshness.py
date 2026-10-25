"""Freshness gate for scalar authority verdicts (#168).

A current or expired verdict leads the answer with the fold's value. Once a slot has a stated
absolute, occurrences in that slot never move it (``scalar_state_fold``), so "just finished my 5th
project" leaves a View of 4 in place. When the recall's observation hits hold a newer occurrence in
the verdict's own slot, the verdict is shown but does not lead.

Only the exact slot counts. A newer assertion in another slot of the same subject and value kind
is not evidence: the subject is usually the user, so "I have 22 books" would demote a coin count
(E2E-17). Times are the assertions' ``valid_at``; a missing or unparseable time never demotes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from menhir.domain.temporal import parse_iso8601

Slot = tuple[str, str, str, str, str]  # subject_uuid, attribute, scope, value_kind, unit


@dataclass(frozen=True)
class ObservationTime:
    """One observation hit as the freshness gate sees it."""

    assertion_id: str
    slot: Slot
    operation: str
    valid_at: str | None


def newer_unfolded_evidence(
    slot: Slot,
    verdict_valid_at: str | None,
    observations: Iterable[ObservationTime],
) -> tuple[str, ...]:
    """Ids of same-slot occurrences newer than the verdict (the fold ignores occurrences)."""
    verdict_time = parse_iso8601(verdict_valid_at)
    if verdict_time is None:
        return ()
    stale_by: list[str] = []
    for obs in observations:
        if obs.slot != slot or obs.operation != "occurrence":
            continue
        obs_time = parse_iso8601(obs.valid_at)
        if obs_time is None or obs_time <= verdict_time:
            continue
        stale_by.append(obs.assertion_id)
    return tuple(stale_by)
