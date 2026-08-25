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
    "StaleIdentityClaim",
    "FenceHandle",
    "IdentityClaim",
    "admit_structure_writer",
    "release_structure_writer",
    "raise_fence",
    "lower_fence",
    "fence_status",
    "writers_holding_identities",
    "STALE_WRITER_SECONDS",
    "FENCE_ID",

]

#: How long a registered writer may run before the drain treats its slot as abandoned. Generous
#: on purpose: a full structure write on the largest project here is thousands of MERGEs, and
#: reaping a live writer is worse than waiting for a dead one.
STALE_WRITER_SECONDS = 900

FENCE_ID = "singleton"
_FENCE_ID = FENCE_ID

#: Field separator inside a writer entry. Stripped from every component before joining, so a
#: project name containing it cannot shift the fields a transfer parses.
_SEP = "|"


class StructureWritesFrozen(RuntimeError):
    """Raised when a structure write is attempted while the fence is up."""


class StaleIdentityClaim(RuntimeError):
    """The identity a scan settled under is no longer the active binding for its directory."""


@dataclass(frozen=True)
class FenceHandle:
    """Proof that a writer was admitted, and the token it must release with."""

    writer_id: str


@dataclass(frozen=True)
class IdentityClaim:
    """What a settled scan must still be able to prove when it finally writes.

    Not just the id. An id alone is satisfied by an identity that has since been superseded,
    transferred to a different directory, or transferred away and back -- all three of which mean
    the scan in hand describes a directory this identity no longer owns.
    """

    project_id: str
    root_key: str
    generation: int
    host: str

    def as_entry_fields(self) -> tuple[str, ...]:
        return (self.root_key, self.project_id, str(self.generation))


def _writer_id() -> str:
    return f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _clean(value: Any) -> str:
    return str(value or "").replace(_SEP, "/")


def admit_structure_writer(
    neo4j: Any, *, label: str = "", claim: IdentityClaim | None = None
) -> FenceHandle:
    """Register a structure writer, or raise if the fence is up or the claim is stale.

    The fence check, the IDENTITY CHECK and both registrations are ONE statement.

    **Why the identity check is here.** It used to be a separate `if scan.project_id` at the choke
    point, which proves only that a field is populated. A scan settles its identity, then scans for
    minutes, then writes from a detached task; in that window a transfer can supersede the identity
    it settled under, and the write then lands in a silo that directory no longer owns -- carrying
    the per-project stale prune with it. Validating the claim in a statement of its own would not
    fix it either: the transfer could land between that statement and this one.

    **Why the first write is a probe.** `SET p.last_admission_probe` looks pointless and is the
    load-bearing line. Neo4j's default isolation is read-committed, and `MERGE` on a node that
    already exists takes no write lock, so reading the identity and then registering would let a
    transfer commit in between -- the claim would be validated against a value that was already
    superseded by the time the writer was admitted. The probe takes the write lock on the identity
    BEFORE the claim is read, so the read that authorises this writer is the read no one else can
    invalidate until it commits. Everything after it is under that lock.

    Ordering is deliberate: this locks the identity and then the fence; `_transfer` locks
    identities only. Nothing takes them in the opposite order, so the two cannot deadlock against
    each other.

    A refused admission leaves only the probe -- an inert timestamp. Both registrations happen in
    one `SET` after every gate, so there is no state in which a writer holds one slot and not the
    other.
    """
    writer_id = _writer_id()
    started = f"{time.time():.0f}"
    if claim is None:
        raise StaleIdentityClaim(
            "A structure writer must present an identity claim. Settle identity first "
            "(services.project_identity_service.settle_project_identity), which returns the "
            "claim this admission validates."
        )

    entry = _SEP.join(
        (writer_id, started, _clean(label), *(_clean(f) for f in claim.as_entry_fields()))
    )
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity {project_id: $project_id})
        SET p.last_admission_probe = timestamp()
        WITH p
        WHERE coalesce(p.state, 'bound') = 'bound'
          AND p.bound_host = $host
          AND p.root_key = $root_key
          AND coalesce(p.claim_generation, 0) = $generation
        MERGE (f:StructureWriteFence {id: $fence_id})
          ON CREATE SET f.frozen = false, f.writers = []
        WITH p, f
        WHERE coalesce(f.frozen, false) = false
        SET p.active_writers = coalesce(p.active_writers, []) + [$entry],
            f.writers = coalesce(f.writers, []) + [$entry]
        RETURN size(f.writers) AS active
        """,
        {
            "fence_id": _FENCE_ID,
            "entry": entry,
            "project_id": claim.project_id,
            "host": claim.host,
            "root_key": claim.root_key,
            "generation": int(claim.generation),
        },
    )
    if rows:
        return FenceHandle(writer_id=writer_id)

    # Both refusals return zero rows, so the reason is established afterwards -- on the failure
    # path only, where an extra read costs nothing and a wrong diagnosis costs an hour.
    status = fence_status(neo4j)
    if status.get("frozen"):
        raise StructureWritesFrozen(
            "Structure writes are frozen for a migration"
            + (f": {status.get('reason')}" if status.get("reason") else "")
            + ". This is temporary; retry once the migration reports complete."
        )
    raise StaleIdentityClaim(
        f"Refusing to write structure for {label or '<unknown>'}: the identity this scan settled "
        f"under ({claim.project_id}, generation {claim.generation}) is no longer the active "
        f"binding for {claim.root_key!r} on {claim.host!r}. It was superseded, transferred to "
        f"another directory, or re-issued while this scan was running. Re-scan; the scan in hand "
        f"describes a directory this identity no longer owns, and writing it would carry the "
        f"per-project stale prune into another project's silo."
    )


def writers_holding_identities(neo4j: Any, project_ids: list[str]) -> list[dict[str, Any]]:
    """Writers registered against any of *project_ids*. Diagnostics for a refused transfer."""
    if not project_ids:
        return []
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity) WHERE p.project_id IN $ids
        RETURN p.project_id AS id, coalesce(p.active_writers, []) AS writers
        """,
        {"ids": list(project_ids)},
    )
    now = time.time()
    out = []
    for row in rows:
        for entry in row.get("writers") or []:
            parts = str(entry).split(_SEP)
            started = float(parts[1]) if len(parts) > 1 and parts[1] else now
            out.append(
                {
                    "identity": row.get("id"),
                    "id": parts[0],
                    "age_s": int(now - started),
                    "label": parts[2] if len(parts) > 2 else "",
                }
            )
    return out


def release_structure_writer(neo4j: Any, handle: FenceHandle | None) -> None:
    """Drop a writer's registration from BOTH lists. Best-effort: never fail the write it guarded.

    Both, in one statement. A writer left on the identity's list blocks every future transfer of
    that directory, and one left on the fence's list blocks the migration drain -- so releasing
    half is a slow outage in whichever half was missed.
    """
    if handle is None:
        return
    try:
        neo4j.execute(
            """
            MATCH (f:StructureWriteFence {id: $fence_id})
            SET f.writers = [w IN coalesce(f.writers, []) WHERE NOT w STARTS WITH $prefix]
            WITH f
            MATCH (p:ProjectIdentity)
            WHERE any(w IN coalesce(p.active_writers, []) WHERE w STARTS WITH $prefix)
            SET p.active_writers =
                [w IN coalesce(p.active_writers, []) WHERE NOT w STARTS WITH $prefix]
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
