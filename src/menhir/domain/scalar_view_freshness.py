"""Freshness gate for scalar authority verdicts (#168).

A current or expired verdict leads the answer with the fold's value. Newer evidence the fold does
not use can make that value stale:

* an occurrence in the same slot: once a slot has a stated absolute, occurrences never move it
  (``scalar_state_fold``), so "just finished my 5th project" leaves a View of 4 in place;
* a newer assertion about the same subject that landed in another slot of the same value kind
  ("switched to the F-150" under a different attribute than the car the View tracks).

When the recall's observation hits hold such evidence, the verdict is shown but does not lead.
Times are the assertions' ``valid_at``, the only time the observation query returns. A missing or
unparseable time never demotes.
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
    cosine: float


def newer_unfolded_evidence(
    slot: Slot,
    verdict_valid_at: str | None,
    observations: Iterable[ObservationTime],
    *,
    min_similarity: float,
) -> tuple[str, ...]:
    """Assertion ids that are newer than the verdict and that its fold does not account for."""
    verdict_time = parse_iso8601(verdict_valid_at)
    if verdict_time is None:
        return ()
    subject, _, _, value_kind, _ = slot
    stale_by: list[str] = []
    for obs in observations:
        if obs.slot[0] != subject:
            continue
        obs_time = parse_iso8601(obs.valid_at)
        if obs_time is None or obs_time <= verdict_time:
            continue
        if obs.slot == slot:
            # Absolutes and deltas in the slot are folded into the verdict; occurrences are not.
            if obs.operation != "occurrence":
                continue
        elif obs.slot[3] != value_kind or obs.cosine < min_similarity:
            continue
        stale_by.append(obs.assertion_id)
    return tuple(stale_by)
