"""Structured outcomes for saga reconciliation (CF-20a, the observation contract).

Every saga reconciler classifies each PREPARED row into exactly one of these outcomes.
``reconcile(dry_run=True)`` reports the classification and mutates NOTHING; live mode
reaches the same classification and then acts on it. One vocabulary, so a dry-run
summary from four different coordinators is directly comparable.

Deliberately absent: ``WOULD_COMMIT``. A dry-run proves the deterministic decision path,
not that the eventual mutation would commit successfully. Naming the happy path
"would commit" would claim a guarantee the dry-run cannot make.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# --- reachable in CF-20a -------------------------------------------------------------

#: Preconditions hold; live mode would attempt the forward mutation (merge, metric write).
WOULD_REPLAY = "WOULD_REPLAY"

#: Preconditions hold; live mode would attempt the unmerge restore.
WOULD_RESTORE = "WOULD_RESTORE"

#: The graph is ALREADY in this operation's after-state -- a previous attempt applied it and
#: crashed before the journal transition. No graph mutation is required; live mode would only
#: move the journal row to COMMITTED. This is NOT "the replay would commit"; nothing is replayed.
WOULD_MARK_ALREADY_APPLIED = "WOULD_MARK_ALREADY_APPLIED"

#: Live mode would quarantine the row (NEEDS_REVIEW): precondition drift, an unparseable
#: request, a missing frozen precondition, or an unreplayable snapshot.
WOULD_NEEDS_REVIEW = "WOULD_NEEDS_REVIEW"

#: The row belongs to a different saga type than the coordinator being asked. Today each
#: reconciler filters by ``operation_kind``; this makes that filtering visible instead of silent.
SKIP = "SKIP"

#: No coordinator claims this ``operation_kind``. A first-class outcome, never a silent skip:
#: an unrecognised PREPARED row is exactly the kind of thing that must not be invisible.
UNKNOWN_KIND = "UNKNOWN_KIND"

# --- reserved for CF-20b (operation ownership) ---------------------------------------
# Defined here so the vocabulary lives in one place and CF-20b is purely additive. No
# CF-20a code path can produce these: PREPARED rows carry no owner/heartbeat metadata yet.

#: The original writer still holds a live heartbeat on this operation. A hard veto on replay.
LIVE_OWNER = "LIVE_OWNER"

#: The row carries no owner metadata, so liveness cannot be proven either way. Fails closed
#: during mixed-version rollout rather than being assumed abandoned.
OWNER_UNKNOWN = "OWNER_UNKNOWN"


# --- live replay results (CF-20c) ----------------------------------------------------
# Distinct from the WOULD_* vocabulary on purpose: those forecast a decision, these report an
# action that has already happened to the graph. Collapsing the two would make a dry-run summary
# and a live summary indistinguishable at a glance, which is exactly the confusion an operator
# reading a recovery report cannot afford.

#: The row's mutation was applied (or recognised as already applied) and the journal reached a
#: terminal state. The only outcome that may report progress.
REPLAYED = "REPLAYED"

#: The graph was in neither expected state, so the row was quarantined. NOT a failure of recovery:
#: recovery did its job by refusing to paper over a graph some other writer changed.
DRIFTED = "DRIFTED"

#: Replay raised something unexpected. The row stays PREPARED and is retried on a later pass --
#: deliberately NOT quarantined, because a transient Neo4j outage must not become a permanent
#: operator ticket.
FAILED = "FAILED"

#: The row belongs to a different saga type than the coordinator asked. Mirrors SKIP in the
#: forecast vocabulary and increments no counter.
SKIPPED = "SKIPPED"

#: Every live outcome. A replay result outside this set is a defect, not a new case.
LIVE_OUTCOMES: frozenset[str] = frozenset({REPLAYED, DRIFTED, FAILED, SKIPPED})


#: Every defined outcome. Useful for asserting a summary covers the whole vocabulary.
ALL_OUTCOMES: frozenset[str] = frozenset(
    {
        WOULD_REPLAY,
        WOULD_RESTORE,
        WOULD_MARK_ALREADY_APPLIED,
        WOULD_NEEDS_REVIEW,
        SKIP,
        UNKNOWN_KIND,
        LIVE_OWNER,
        OWNER_UNKNOWN,
    }
)

#: Outcomes a CF-20a dry-run can actually emit. CF-20b adds the ownership pair.
CF20A_REACHABLE_OUTCOMES: frozenset[str] = frozenset(
    {
        WOULD_REPLAY,
        WOULD_RESTORE,
        WOULD_MARK_ALREADY_APPLIED,
        WOULD_NEEDS_REVIEW,
        SKIP,
        UNKNOWN_KIND,
    }
)


def summarize_outcomes(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count a dry-run's per-row outcomes by kind.

    Every CF-20a-reachable outcome appears with an explicit count, zero included. That matters
    for a preflight read: a reader must be able to tell "this coordinator saw no drifted rows"
    apart from "this coordinator does not report drift at all", and an absent key cannot express
    the difference. The two CF-20b ownership outcomes are omitted until they are reachable, so a
    20a summary never implies it checked ownership.

    Outcomes outside the vocabulary are counted under their own key rather than dropped -- a
    reconciler emitting something unrecognised is a defect that must be visible, not swallowed.
    """
    counts: dict[str, int] = {name: 0 for name in sorted(CF20A_REACHABLE_OUTCOMES)}
    for entry in outcomes:
        name = str(entry.get("outcome"))
        counts[name] = counts.get(name, 0) + 1
    return counts


__all__ = [
    "summarize_outcomes",
    "WOULD_REPLAY",
    "WOULD_RESTORE",
    "WOULD_MARK_ALREADY_APPLIED",
    "WOULD_NEEDS_REVIEW",
    "SKIP",
    "UNKNOWN_KIND",
    "LIVE_OWNER",
    "OWNER_UNKNOWN",
    "ALL_OUTCOMES",
    "CF20A_REACHABLE_OUTCOMES",
    "REPLAYED",
    "DRIFTED",
    "FAILED",
    "SKIPPED",
    "LIVE_OUTCOMES",
]
