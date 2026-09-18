"""The lifecycle of a view root: the thing a promotion builds and the pointer later publishes.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P4).
Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

`canonical_view` moves a pointer. This module owns what the pointer is allowed to point AT, and
it exists because the pointer alone does not make a partial write invisible -- it only makes a
partial write *unreferenced*. Something still has to guarantee that a root under construction can
never be published, and that a root nobody can publish is eventually reclaimed without ever
reclaiming one a reader still needs.

**Promotion never prunes, and that is the point.** The local scan path MERGEs in place and then
deletes whatever the scan no longer sees -- which is where #99 lived, and why the gate asks for "no
name-keyed prune". A promotion instead writes a COMPLETE new root and flips to it. A file deleted
from the project leaves the graph because the new root never contained it, not because anything
matched it by name. Deletion needs no delete, so the prune bug class has no door to come back
through.

## The safety argument, stated before the code

The dangerous interleaving is the sweeper racing the builder: the sweeper decides a root is
garbage and deletes its nodes while a promotion is flipping to it, and the view ends up pointing
at a root whose content is being erased. Lock ordering would be one answer, and a fragile one --
Neo4j's default isolation is read-committed and a `MATCH` takes no write lock, so "read the view,
then decide" is the same TOCTOU `admit_structure_writer` documents.

Two mechanisms close it, and only together:

1. **A lease, so the fact is about the builder rather than the clock.** Publishing a root requires
   a LIVE lease (`canonical_view.publish_root` enforces it in its own statement), and a lease
   expires against the server clock and never un-expires. So once a root's lease has expired, the
   set of pointers referencing it can only shrink -- a publish can no longer add one, and a restore
   to `previous` cannot introduce a root the view was not already holding.

2. **A probe write, so the authorising read is under a lock.** Monotonicity alone is NOT enough,
   and it is worth being exact about why: the sweeper can read the view *before* a concurrent
   publish commits and retire the root *after* it, and every observation it made was true when it
   made them. Neo4j's default isolation is read-committed and a `MATCH` takes no write lock, so
   "read the view, then decide" is the same TOCTOU `admit_structure_writer` documents.

   Both paths therefore take the write lock on the ROOT before reading what authorises them, and
   both take it in the same order (root, then view), so they serialise against each other and
   cannot deadlock. Whichever acquires the root first wins, and the loser sees the winner's
   outcome: a sweeper that lost sees the root referenced and skips; a publisher that lost sees the
   root abandoned and refuses.

Retirement is split from deletion for the same reason: :func:`retire_root` flips the state under
that guard, and only :func:`purge_root` deletes content, and only for a root already retired.
**Content deletion never begins on a root that is still publishable.**

"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_ROOT_LEASE_SECONDS",
    "ERR_ROOT_NOT_BUILDING",
    "ERR_ROOT_NOT_RETIRED",
    "ERR_ROOT_UNKNOWN",
    "PURGE_BATCH",
    "ROOT_ABANDONED",
    "ROOT_BUILDING",
    "ROOT_COMPLETE",
    "VIEW_ROOT_CONSTRAINTS",
    "VIEW_ROOT_PROPERTY",
    "RootError",
    "RootRecord",
    "begin_root",
    "complete_root",
    "find_publishable_root",
    "new_root_id",
    "purge_root",
    "read_root",
    "renew_root",
    "retire_root",
]

ROOT_BUILDING = "BUILDING"
ROOT_COMPLETE = "COMPLETE"
ROOT_ABANDONED = "ABANDONED"

ERR_ROOT_UNKNOWN = "snapshot.root.unknown"
ERR_ROOT_NOT_BUILDING = "snapshot.root.not_building"
ERR_ROOT_NOT_RETIRED = "snapshot.root.not_retired"

#: How long a build may run before the sweeper may treat the root as abandoned. Generous for the
#: same reason `STRUCTURE_WRITER_SECONDS` is: a full structure write on a large project is
#: thousands of MERGEs, and reaping a live builder is worse than waiting on a dead one. A builder
#: that expects to exceed it calls :func:`renew_root` rather than raising the default.
DEFAULT_ROOT_LEASE_SECONDS = 900

#: The property every structure node written under a root carries. Defined HERE rather than in the
#: writer, because the purge deletes by it: if the two halves disagreed on the name, a purge would
#: silently delete nothing and the graph would grow a root per promotion forever.
VIEW_ROOT_PROPERTY = "view_root"

#: Rows per purge statement. A purge is the one operation here with unbounded size, and a single
#: DETACH DELETE over a large project is a long lock held against every reader.
PURGE_BATCH = 1000

#: Real DDL, shipped with the module so a test applies the SAME constraint the bootstrap does.
VIEW_ROOT_CONSTRAINTS = [
    "CREATE CONSTRAINT view_root_id IF NOT EXISTS FOR (r:ViewRoot) REQUIRE r.root_id IS UNIQUE",
]


class RootError(RuntimeError):
    """A refusal carrying a stable code. Never carries a path, a filename, or file content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RootRecord:
    """A root's durable state. `lease_live` is the SERVER's answer, never computed here."""

    root_id: str
    project_id: str
    view_key: str
    snapshot_id: str
    state: str
    lease_live: bool


def new_root_id() -> str:
    """A fresh, opaque root id.

    Never derived from the project name, the snapshot digest or a counter. A root id that encodes
    something is a root id someone will one day parse, and the retention rules here depend on ids
    being distinct rather than meaningful.
    """
    return f"vr-{uuid.uuid4().hex}"


_RETURN = (
    "RETURN r.root_id AS root_id, r.project_id AS project_id, r.view_key AS view_key, "
    "coalesce(r.snapshot_id, '') AS snapshot_id, r.state AS state, "
    "coalesce(r.lease_expires_at, 0) > timestamp() AS lease_live"
)


def _record(row: Any) -> RootRecord:
    return RootRecord(
        root_id=str(row.get("root_id")),
        project_id=str(row.get("project_id")),
        view_key=str(row.get("view_key")),
        snapshot_id=str(row.get("snapshot_id") or ""),
        state=str(row.get("state")),
        lease_live=bool(row.get("lease_live")),
    )


def begin_root(
    neo4j: Any,
    *,
    project_id: str,
    view_key: str,
    snapshot_id: str,
    root_id: str | None = None,
    lease_seconds: int = DEFAULT_ROOT_LEASE_SECONDS,
) -> RootRecord:
    """Create a new root in BUILDING, holding a lease.

    `CREATE`, never `MERGE`. Reusing a root id would mean a new build inheriting the nodes of an
    old one -- the extraction lease's generation-reuse bug in a different costume -- and the
    uniqueness constraint turns that mistake into an error instead of a silent merge.

    The lease is computed from `timestamp()`, the SERVER's clock. A builder's own clock is not a
    distributed fact, and four menhir processes on this deployment would each have their own.
    """
    rid = root_id or new_root_id()
    rows = list(
        neo4j.execute(
            "CREATE (r:ViewRoot {root_id: $root, project_id: $pid, view_key: $vk, "
            "  snapshot_id: $sid, state: $building, created_at: timestamp(), "
            "  lease_expires_at: timestamp() + $lease_ms}) "
            f"{_RETURN}",
            {
                "root": rid,
                "pid": project_id,
                "vk": view_key,
                "sid": snapshot_id,
                "building": ROOT_BUILDING,
                "lease_ms": int(lease_seconds) * 1000,
            },
        )
    )
    return _record(rows[0])


def renew_root(neo4j: Any, *, root_id: str, lease_seconds: int = DEFAULT_ROOT_LEASE_SECONDS) -> RootRecord:
    """Extend a BUILDING root's lease, or refuse.

    Called between write batches, not once at the start. A lease checked only before a long
    operation is a lease that expires in the middle of it, and the whole safety argument above
    rests on "an expired lease can never be published" -- which is a promise to the SWEEPER, and
    means a builder that lets its lease lapse must find out rather than carry on.

    Refuses once the root is no longer BUILDING: a retired root must not be resurrected by a
    builder that woke up late.
    """
    rows = list(
        neo4j.execute(
            "MATCH (r:ViewRoot {root_id: $root}) "
            "WHERE r.state = $building "
            "SET r.lease_expires_at = timestamp() + $lease_ms "
            f"{_RETURN}",
            {
                "root": root_id,
                "building": ROOT_BUILDING,
                "lease_ms": int(lease_seconds) * 1000,
            },
        )
    )
    if rows:
        return _record(rows[0])
    raise _not_building(neo4j, root_id, "renewed")


def complete_root(neo4j: Any, *, root_id: str) -> RootRecord:
    """Mark a root finished and publishable, or refuse.

    Requires a LIVE lease, not merely the BUILDING state. If the lease lapsed, the sweeper was
    entitled to retire this root and may already be deleting its nodes, so "I finished" is no
    longer a claim this builder can make. Refusing here is what keeps the sweeper's decision final.
    """
    rows = list(
        neo4j.execute(
            "MATCH (r:ViewRoot {root_id: $root}) "
            "WHERE r.state = $building AND coalesce(r.lease_expires_at, 0) > timestamp() "
            "SET r.state = $complete, r.completed_at = timestamp() "
            f"{_RETURN}",
            {"root": root_id, "building": ROOT_BUILDING, "complete": ROOT_COMPLETE},
        )
    )
    if rows:
        return _record(rows[0])
    raise _not_building(neo4j, root_id, "completed")


def _not_building(neo4j: Any, root_id: str, verb: str) -> RootError:
    """Establish the reason on the failure path only, as `admit_structure_writer` does."""
    current = read_root(neo4j, root_id=root_id)
    if current is None:
        return RootError(ERR_ROOT_UNKNOWN, f"no such view root; it cannot be {verb}")
    return RootError(
        ERR_ROOT_NOT_BUILDING,
        f"this root is {current.state.lower()} and its lease is "
        f"{'live' if current.lease_live else 'expired'}; it cannot be {verb}",
    )


def read_root(neo4j: Any, *, root_id: str) -> RootRecord | None:
    rows = list(
        neo4j.execute(f"MATCH (r:ViewRoot {{root_id: $root}}) {_RETURN}", {"root": root_id})
    )
    return _record(rows[0]) if rows else None


def find_publishable_root(
    neo4j: Any, *, project_id: str, view_key: str, snapshot_id: str
) -> RootRecord | None:
    """A COMPLETE, still-leased root already built for this snapshot, if one exists.

    This is how promotion becomes idempotent by snapshot: a retry after an ambiguous failure is the
    NORMAL case, and rebuilding an identical root every time would make a flaky network a source of
    graph garbage.

    It is deliberately a lookup rather than a guarantee. Two promotions of the same snapshot can
    still both build, because the alternative -- a durable claim on the snapshot -- is a second
    ownership mechanism, and the design refuses to invent one. Two builds waste work; only one of
    them can ever publish, which is the property that actually matters.
    """
    rows = list(
        neo4j.execute(
            "MATCH (r:ViewRoot {project_id: $pid, view_key: $vk, snapshot_id: $sid}) "
            "WHERE r.state = $complete AND coalesce(r.lease_expires_at, 0) > timestamp() "
            f"{_RETURN} ORDER BY r.completed_at DESC LIMIT 1",
            {"pid": project_id, "vk": view_key, "sid": snapshot_id, "complete": ROOT_COMPLETE},
        )
    )
    return _record(rows[0]) if rows else None


def retire_root(neo4j: Any, *, root_id: str) -> bool:
    """Flip a root to ABANDONED if -- and only if -- it can never be published again.

    The guard is the safety argument made executable:

    * the lease has expired, so `canonical_view.publish_root` will refuse this root forever;
    * and no pointer references it, which -- given the first -- can never become true again.

    **`SET r.retire_probe` is not bookkeeping; it is the load-bearing line.** It takes the write
    lock on the root BEFORE the view is read, so the view state this decision rests on is one a
    concurrent publish cannot invalidate before this statement commits. Without it the sweeper
    could read a view that does not yet reference the root, a publish could commit, and this would
    then retire the root the view had just made current -- and every read it made would have been
    true at the time. `publish_root` locks the root first for the same reason and in the same
    order, so the two serialise and neither can deadlock the other.

    Returns False when the root is already retired, still leased, or still referenced -- all
    ordinary outcomes for a sweep, none of them errors.
    """
    rows = list(
        neo4j.execute(
            "MATCH (r:ViewRoot {root_id: $root}) "
            "WHERE r.state <> $abandoned AND coalesce(r.lease_expires_at, 0) <= timestamp() "
            "SET r.retire_probe = timestamp() "
            "WITH r "
            "OPTIONAL MATCH (v:CanonicalView {project_id: r.project_id, view_key: r.view_key}) "
            "WITH r, v "
            "WHERE v IS NULL "
            "   OR (coalesce(v.current_root, '') <> r.root_id "
            "       AND coalesce(v.previous_root, '') <> r.root_id) "
            "SET r.state = $abandoned, r.retired_at = timestamp() "
            "RETURN r.root_id AS root_id",
            {"root": root_id, "abandoned": ROOT_ABANDONED},
        )
    )
    return bool(rows)


def purge_root(neo4j: Any, *, root_id: str, batch: int = PURGE_BATCH) -> int:
    """Delete the structure nodes written under a retired root. Returns how many were removed.

    Refuses anything not already ABANDONED. Deleting content is the only irreversible act in this
    module, so it is gated on a state that :func:`retire_root` has already proven unpublishable --
    rather than re-deriving that proof here, where a second copy could drift from the first.

    Batched and resumable: a purge that dies halfway leaves a root that is still ABANDONED, still
    unpublishable and still purgeable, so the next sweep finishes it. Partially-deleted is a safe
    resting state precisely because nothing may read this root again.
    """
    record = read_root(neo4j, root_id=root_id)
    if record is None:
        raise RootError(ERR_ROOT_UNKNOWN, "no such view root; nothing to purge")
    if record.state != ROOT_ABANDONED:
        raise RootError(
            ERR_ROOT_NOT_RETIRED,
            "refusing to delete content for a root that has not been retired; "
            "retire it first so the decision is durable before anything is destroyed",
        )

    removed = 0
    while True:
        rows = list(
            neo4j.execute(
                f"MATCH (n) WHERE n.{VIEW_ROOT_PROPERTY} = $root "
                "WITH n LIMIT $batch DETACH DELETE n RETURN count(*) AS deleted",
                {"root": root_id, "batch": int(batch)},
            )
        )
        deleted = int(rows[0].get("deleted") or 0) if rows else 0
        removed += deleted
        if deleted == 0:
            return removed
