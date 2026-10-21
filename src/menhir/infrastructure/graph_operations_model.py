"""Shared vocabulary for the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): the closed operation kind/state sets,
the per-participant fencing kind sets, the lock-release states, the participant-UUID derivation
helpers, and the saga exception types. The ``graph_operations`` facade re-exports every public
name defined here, so existing imports keep working unchanged.
"""

from __future__ import annotations

import json
from typing import Any

# Closed enums (Part E1). Kept as plain frozensets so callers pass validated strings.
#
# ENTITY_MERGE (merge/delete lifecycle remediation, Phase 4) reuses this journal rather than adding
# a parallel audit store. Its target_key is the normalized PAIR key, so the existing unresolved-key
# fence (ux_graph_ops_unresolved_key) automatically quarantines a drifted pair: while a merge is
# PREPARED or NEEDS_REVIEW, a competing merge of the same pair cannot be prepared.
#
# METRIC_WRITE is preserved verbatim (invariant 13): renaming it would strand PREPARED rows written
# by an earlier build and silently stop their replay.
OPERATION_KINDS = frozenset(
    {
        "METRIC_WRITE", "METRIC_MIGRATE", "METRIC_REVERSE",
        "ENTITY_MERGE", "ENTITY_UNMERGE", "LEGACY_ENTITY_UNMERGE",
        "ENTITY_DELETE", "SESSION_TTL_DELETE",
        "EXPLICIT_ERASURE",
    }
)
# FAILED is terminal and means "no graph mutation occurred" (plan section 1's state list). It is
# NOT a quarantine: an operation that abstained at the mutation gate (e.g. a node legitimately became
# COMPRESSED between PREPARE and MUTATE) left the graph untouched, so there is nothing for an operator
# to adjudicate. Because the unresolved-key index fences only PREPARED and NEEDS_REVIEW, marking such
# an op FAILED RELEASES the pair -- whereas NEEDS_REVIEW would fence it forever over a benign
# abstention, and leaving it PREPARED would make reconciliation retry a pair that will never become
# eligible again.
OPERATION_STATES = frozenset(
    {"PREPARED", "COMMITTED", "NEEDS_REVIEW", "REVERSED", "FAILED"}
)

# Per-participant fencing (invariant 14). The pair-key fence blocks a competing operation on the
# SAME pair, but two unresolved operations that share ONE node via different pairs (a merge of A+B
# and an unrelated merge of B+C, or a delete of B while A+B is unresolved) are not mutually fenced by
# target_key alone. graph_operation_locks holds one row per participant UUID of an UNRESOLVED op, so
# at most one unresolved operation may hold any given participant. Metric kinds fence on target_key
# (metric identity, not entity pairs) and take no participant locks.
#
# Entity-pair kinds lock {survivor, absorbed}; delete kinds lock each target uuid.
_PARTICIPANT_PAIR_KINDS = frozenset(
    {"ENTITY_MERGE", "ENTITY_UNMERGE", "LEGACY_ENTITY_UNMERGE"}
)
# EXPLICIT_ERASURE fences its participants for the same reason a delete does: while an
# erasure is unresolved, a merge on one of its subjects could copy the content being erased
# into a survivor's recovery snapshot. A NAMESPACE erasure carries no "targets" (membership
# lives in the erasure_subjects inventory, not in request_json, so a large namespace stays
# bounded), so it takes no participant lock and fences on its target_key instead.
_PARTICIPANT_DELETE_KINDS = frozenset(
    {"ENTITY_DELETE", "SESSION_TTL_DELETE", "EXPLICIT_ERASURE"}
)
_PARTICIPANT_KINDS = _PARTICIPANT_PAIR_KINDS | _PARTICIPANT_DELETE_KINDS

# A lock is released only when its op reaches a terminal state. NEEDS_REVIEW keeps the fence
# (quarantine still fences); PREPARED keeps it (the op is in flight).
_LOCK_RELEASE_STATES = frozenset({"COMMITTED", "REVERSED", "FAILED"})


def _participant_uuids(operation_kind: str, request: dict[str, Any]) -> list[str]:
    """Participant UUIDs an operation must fence, derived from its request payload.

    Entity-pair kinds fence {survivor, absorbed}; delete kinds fence each target. Everything else
    (Metric writes/migrations/reversals) takes no participant lock. Used identically at PREPARE and
    at backfill so the two paths can never disagree about what a row locks.
    """
    if operation_kind in _PARTICIPANT_PAIR_KINDS:
        return [
            str(u)
            for u in (request.get("survivor_uuid"), request.get("absorbed_uuid"))
            if u
        ]
    if operation_kind in _PARTICIPANT_DELETE_KINDS:
        seen: dict[str, None] = {}
        for t in request.get("targets") or []:
            if t:
                seen.setdefault(str(t), None)
        return list(seen)
    return []


def _participants_from_request_json(operation_kind: str, request_json: str | None) -> list[str]:
    """Best-effort participant extraction from a stored request_json string.

    A parse failure degrades to no participant locks (the pair-key fence still holds), matching the
    documented backward-compat posture: the participant fence is advisory until a row is locked.
    """
    if operation_kind not in _PARTICIPANT_KINDS or not request_json:
        return []
    try:
        request = json.loads(request_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(request, dict):
        return []
    return _participant_uuids(operation_kind, request)


#: Lease name that pauses all saga PREPARE while recovery owns the backlog (CF-20c).
#:
#: The lease lives in `scheduler_leases`, in the SAME SQLite database as this journal. That shared
#: database is what makes the gate correct rather than advisory: a writer's BEGIN IMMEDIATE and the
#: recovery lease's BEGIN IMMEDIATE contend for the same write lock, so they serialise and neither
#: can slip between the other's check and write.
RECONCILIATION_LEASE_NAME = "saga-reconciliation"


class GraphOperationError(RuntimeError):
    """Raised when a saga invariant would be violated (immutability, fencing, state)."""


class SagaWritesPausedError(GraphOperationError):
    """A new saga cannot PREPARE because recovery currently owns the backlog.

    Distinct from the fencing errors so a caller can tell "this specific target is busy" (retry
    later, or adjudicate) from "this process is not accepting new sagas at all" (wait for recovery
    to finish). Subclasses GraphOperationError so existing handlers keep working unchanged.
    """
