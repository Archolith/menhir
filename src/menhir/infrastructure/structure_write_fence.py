"""A cross-process freeze on structure writes, for migrations that must see a still graph.

CF-257 phase 2. A migration that counts rows, reconciles duplicates and then switches a key needs
those steps to run against a graph nothing else is writing. A count taken at T is worthless if a
write lands at T+1.

**Why this is durable state and not a process flag.** Four menhir processes run concurrently on
this deployment -- two `serve` and two `serve-watch` -- and the unattended structure watcher lives
in the latter. A flag in one process's memory is invisible to the other three, so the fence has to
live where all of them already agree: the graph.

**Why admission and counting share one statement.** The obvious shape -- set a flag, then wait for
an active count to hit zero -- has a TOCTOU hole: a writer reads the flag as clear, pauses before
registering itself, and enters after the fence has already counted zero. A flag and a counter
checked separately can never close that. Here a writer is admitted and registered by ONE Cypher
statement, so "frozen" and "no active writers" are a consistent pair rather than two observations
that happen to be adjacent.

**Why entries expire.** A writer killed mid-write cannot release its slot, and a fence that waits
forever on a dead process is an outage rather than a safeguard. Entries carry a start time and the
drain reports -- loudly -- any it treats as abandoned. That is a deliberate trade: a stale entry
means the fence may proceed while a genuinely-wedged writer is still alive, which is why the
threshold is generous and the report is not suppressible.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "StructureWritesFrozen",
    "FenceHandle",
    "admit_structure_writer",
    "release_structure_writer",
    "raise_fence",
    "lower_fence",
    "fence_status",
    "STALE_WRITER_SECONDS",
]

#: How long a registered writer may run before the drain treats its slot as abandoned. Generous
#: on purpose: a full structure write on the largest project here is thousands of MERGEs, and
#: reaping a live writer is worse than waiting for a dead one.
STALE_WRITER_SECONDS = 900

_FENCE_ID = "singleton"


class StructureWritesFrozen(RuntimeError):
    """Raised when a structure write is attempted while the fence is up."""


@dataclass(frozen=True)
class FenceHandle:
    """Proof that a writer was admitted, and the token it must release with."""

    writer_id: str


def _writer_id() -> str:
    return f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


def admit_structure_writer(neo4j: Any, *, label: str = "") -> FenceHandle:
    """Register a structure writer, or raise if the fence is up.

    The check and the registration are ONE statement. `WHERE` after the `MERGE` filters the row
    out when frozen, so the `SET` never runs and no row comes back -- an admitted writer is always
    a registered writer, and a refused one is never counted.
    """
    writer_id = _writer_id()
    rows = neo4j.execute(
        """
        MERGE (f:StructureWriteFence {id: $fence_id})
          ON CREATE SET f.frozen = false, f.writers = []
        WITH f
        WHERE coalesce(f.frozen, false) = false
        SET f.writers = coalesce(f.writers, []) + [$entry]
        RETURN size(f.writers) AS active
        """,
        {
            "fence_id": _FENCE_ID,
            "entry": f"{writer_id}|{time.time():.0f}|{label}",
        },
    )
    if not rows:
        status = fence_status(neo4j)
        raise StructureWritesFrozen(
            "Structure writes are frozen for a migration"
            + (f": {status.get('reason')}" if status.get("reason") else "")
            + ". This is temporary; retry once the migration reports complete."
        )
    return FenceHandle(writer_id=writer_id)


def release_structure_writer(neo4j: Any, handle: FenceHandle | None) -> None:
    """Drop a writer's registration. Best-effort: never fail the write it was guarding."""
    if handle is None:
        return
    try:
        neo4j.execute(
            """
            MATCH (f:StructureWriteFence {id: $fence_id})
            SET f.writers = [w IN coalesce(f.writers, []) WHERE NOT w STARTS WITH $prefix]
            """,
            {"fence_id": _FENCE_ID, "prefix": f"{handle.writer_id}|"},
        )
    except Exception:  # pragma: no cover - releasing must not mask the caller's outcome
        logger.warning("Failed to release structure-write slot %s", handle.writer_id, exc_info=True)


def raise_fence(neo4j: Any, *, reason: str) -> None:
    """Refuse new structure writers. Does NOT wait -- call :func:`drain` after."""
    neo4j.execute(
        """
        MERGE (f:StructureWriteFence {id: $fence_id})
          ON CREATE SET f.writers = []
        SET f.frozen = true, f.reason = $reason, f.frozen_at = datetime()
        """,
        {"fence_id": _FENCE_ID, "reason": reason},
    )


def lower_fence(neo4j: Any) -> None:
    """Admit writers again.

    Deliberately a separate, explicit call rather than something a context manager does on the way
    out: if a migration phase failed halfway, lifting the fence resumes writers against a
    half-migrated graph. Whoever lowers it is asserting they know which phase completed.
    """
    neo4j.execute(
        """
        MATCH (f:StructureWriteFence {id: $fence_id})
        SET f.frozen = false, f.reason = null, f.frozen_at = null
        """,
        {"fence_id": _FENCE_ID},
    )


def fence_status(neo4j: Any) -> dict[str, Any]:
    """Current fence state and the writers it is still waiting on."""
    rows = neo4j.execute(
        """
        MATCH (f:StructureWriteFence {id: $fence_id})
        RETURN coalesce(f.frozen, false) AS frozen, f.reason AS reason,
               coalesce(f.writers, []) AS writers
        """,
        {"fence_id": _FENCE_ID},
    )
    if not rows:
        return {"frozen": False, "reason": None, "active": [], "stale": []}
    now = time.time()
    active, stale = [], []
    for entry in rows[0].get("writers") or []:
        parts = str(entry).split("|")
        started = float(parts[1]) if len(parts) > 1 and parts[1] else now
        (stale if now - started > STALE_WRITER_SECONDS else active).append(
            {"id": parts[0], "age_s": int(now - started), "label": parts[2] if len(parts) > 2 else ""}
        )
    return {
        "frozen": bool(rows[0].get("frozen")),
        "reason": rows[0].get("reason"),
        "active": active,
        "stale": stale,
    }
