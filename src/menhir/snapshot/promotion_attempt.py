"""Durable intent for snapshot publication and interrupted compensation recovery.

The view pointer is atomic, but the process driving it is not. A PromotionAttempt records the
root, expected generation and actor before the flip. A scheduler can then distinguish an
unfinished publication from an ordinary successful view and conservatively restore or degrade it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PROMOTION_ATTEMPT_CONSTRAINTS",
    "AttemptRecord",
    "begin_attempt",
    "finish_attempt",
    "mark_attempt_compensating",
    "mark_attempt_published",
    "read_attempt",
    "read_latest_attempt",
    "reconcile_promotion_attempts",
]

PROMOTION_ATTEMPT_CONSTRAINTS = [
    "CREATE CONSTRAINT snapshot_promotion_attempt_id IF NOT EXISTS "
    "FOR (a:SnapshotPromotionAttempt) REQUIRE a.attempt_id IS UNIQUE",
    "CREATE INDEX snapshot_promotion_attempt_state IF NOT EXISTS "
    "FOR (a:SnapshotPromotionAttempt) ON (a.state, a.updated_at)",
]

_OPEN_STATES = ("PREPARED", "PUBLISHED", "COMPENSATING")


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    project_id: str
    view_key: str
    snapshot_id: str
    root_id: str
    actor: str
    state: str
    expected_generation: int
    published_generation: int | None


def _record(row: dict[str, Any]) -> AttemptRecord:
    published = row.get("published_generation")
    return AttemptRecord(
        attempt_id=str(row["attempt_id"]),
        project_id=str(row["project_id"]),
        view_key=str(row["view_key"]),
        snapshot_id=str(row["snapshot_id"]),
        root_id=str(row["root_id"]),
        actor=str(row["actor"]),
        state=str(row["state"]),
        expected_generation=int(row.get("expected_generation") or 0),
        published_generation=int(published) if published is not None else None,
    )


_RETURN = (
    "RETURN a.attempt_id AS attempt_id, a.project_id AS project_id, "
    "a.view_key AS view_key, a.snapshot_id AS snapshot_id, a.root_id AS root_id, "
    "a.actor AS actor, a.state AS state, a.expected_generation AS expected_generation, "
    "a.published_generation AS published_generation"
)


def begin_attempt(
    neo4j: Any,
    *,
    project_id: str,
    view_key: str,
    snapshot_id: str,
    root_id: str,
    actor: str,
    expected_generation: int,
) -> AttemptRecord:
    if not actor.strip():
        raise ValueError("promotion actor is required")
    attempt_id = f"pa-{uuid.uuid4().hex}"
    rows = list(
        neo4j.execute(
            "CREATE (a:SnapshotPromotionAttempt {attempt_id: $attempt, project_id: $pid, "
            "view_key: $vk, snapshot_id: $sid, root_id: $root, actor: $actor, "
            "state: 'PREPARED', expected_generation: $expected, created_at: timestamp(), "
            "updated_at: timestamp()}) " + _RETURN,
            {
                "attempt": attempt_id,
                "pid": project_id,
                "vk": view_key,
                "sid": snapshot_id,
                "root": root_id,
                "actor": actor.strip(),
                "expected": int(expected_generation),
            },
        )
    )
    return _record(rows[0])


def read_attempt(neo4j: Any, *, attempt_id: str) -> AttemptRecord | None:
    rows = list(
        neo4j.execute(
            "MATCH (a:SnapshotPromotionAttempt {attempt_id: $attempt}) " + _RETURN,
            {"attempt": attempt_id},
        )
    )
    return _record(rows[0]) if rows else None


def read_latest_attempt(
    neo4j: Any, *, project_id: str, view_key: str, snapshot_id: str
) -> AttemptRecord | None:
    """Return the newest durable attempt for one upload snapshot identity."""
    rows = list(
        neo4j.execute(
            "MATCH (a:SnapshotPromotionAttempt {project_id: $pid, view_key: $vk, "
            "snapshot_id: $sid}) "
            + _RETURN
            + " ORDER BY a.created_at DESC, a.attempt_id DESC LIMIT 1",
            {"pid": project_id, "vk": view_key, "sid": snapshot_id},
        )
    )
    return _record(rows[0]) if rows else None


def _transition(
    neo4j: Any,
    *,
    attempt_id: str,
    from_states: tuple[str, ...],
    state: str,
    published_generation: int | None = None,
    reason: str = "",
) -> AttemptRecord:
    rows = list(
        neo4j.execute(
            "MATCH (a:SnapshotPromotionAttempt {attempt_id: $attempt}) "
            "WHERE a.state IN $from_states "
            "SET a.state = $state, a.updated_at = timestamp(), "
            "a.published_generation = coalesce($published_generation, a.published_generation), "
            "a.failure_reason = CASE WHEN $reason = '' THEN a.failure_reason ELSE $reason END "
            + _RETURN,
            {
                "attempt": attempt_id,
                "from_states": list(from_states),
                "state": state,
                "published_generation": published_generation,
                "reason": reason[:200],
            },
        )
    )
    if not rows:
        raise RuntimeError("promotion attempt is missing or already terminal")
    return _record(rows[0])


def mark_attempt_published(
    neo4j: Any, *, attempt_id: str, generation: int
) -> AttemptRecord:
    return _transition(
        neo4j,
        attempt_id=attempt_id,
        from_states=("PREPARED",),
        state="PUBLISHED",
        published_generation=int(generation),
    )


def mark_attempt_compensating(
    neo4j: Any, *, attempt_id: str, reason: str
) -> AttemptRecord:
    return _transition(
        neo4j,
        attempt_id=attempt_id,
        from_states=("PUBLISHED",),
        state="COMPENSATING",
        reason=reason,
    )


def finish_attempt(neo4j: Any, *, attempt_id: str, state: str) -> AttemptRecord:
    if state not in {"SUCCEEDED", "ROLLED_BACK", "ABANDONED", "DEGRADED"}:
        raise ValueError("invalid terminal promotion-attempt state")
    return _transition(
        neo4j,
        attempt_id=attempt_id,
        from_states=_OPEN_STATES,
        state=state,
    )


def reconcile_promotion_attempts(
    neo4j: Any,
    *,
    stale_after_ms: int = 15 * 60 * 1000,
    limit: int = 50,
    actor: str = "system:snapshot-promotion-reconciler",
) -> dict[str, int]:
    """Resolve stale nonterminal attempts without guessing from process identity.

    An open attempt whose root is current is conservatively restored. If no previous root exists,
    the unchanged view is degraded so reads carry the warning and new promotions stop. If the view
    moved concurrently, a generation/root guard abandons only the stale attempt.
    An open attempt whose root is not current never published (or was already superseded) and is
    safely marked abandoned.
    """
    from menhir.snapshot.canonical_view import (
        ERR_VIEW_SUPERSEDED,
        ViewError,
        mark_degraded,
        read_view,
        restore_previous,
    )

    rows = list(
        neo4j.execute(
            "MATCH (a:SnapshotPromotionAttempt) "
            "WHERE a.state IN $states "
            "AND coalesce(a.updated_at, 0) <= timestamp() - $stale_ms "
            "RETURN a.attempt_id AS attempt_id "
            "ORDER BY a.updated_at LIMIT $limit",
            {"states": list(_OPEN_STATES), "stale_ms": int(stale_after_ms), "limit": int(limit)},
        )
    )
    examined = rolled_back = abandoned = degraded = 0
    for row in rows:
        attempt = read_attempt(neo4j, attempt_id=str(row["attempt_id"]))
        if attempt is None or attempt.state not in _OPEN_STATES:
            continue
        examined += 1
        view = read_view(
            neo4j, project_id=attempt.project_id, view_key=attempt.view_key
        )
        if view is None or view.current_root != attempt.root_id:
            finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="ABANDONED")
            abandoned += 1
            continue

        generation = attempt.published_generation or attempt.expected_generation + 1
        try:
            restore_previous(
                neo4j,
                project_id=attempt.project_id,
                view_key=attempt.view_key,
                expected_generation=generation,
                actor=actor,
            )
        except Exception:
            try:
                mark_degraded(
                    neo4j,
                    project_id=attempt.project_id,
                    view_key=attempt.view_key,
                    reason="interrupted snapshot promotion could not be restored",
                    actor=actor,
                    expected_generation=generation,
                    expected_root=attempt.root_id,
                )
            except ViewError as degrade_exc:
                if degrade_exc.code != ERR_VIEW_SUPERSEDED:
                    raise
                # A valid promotion advanced the pointer after our read. The stale attempt no
                # longer owns the view and must not poison the winner with a degraded flag.
                finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="ABANDONED")
                abandoned += 1
            else:
                finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="DEGRADED")
                degraded += 1
        else:
            finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="ROLLED_BACK")
            rolled_back += 1

    return {
        "examined": examined,
        "rolled_back": rolled_back,
        "abandoned": abandoned,
        "degraded": degraded,
    }
