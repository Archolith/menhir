"""The pointer that decides which snapshot a project's graph currently answers from.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P4).
Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

P4 is the first phase that writes to the graph, and it differs from every phase before it in one
way that shapes this module. The extraction writer deletes its root on any failure and a test
proves nothing survives, because a half-written directory can be removed in one call. **A
half-written graph cannot be.** There is no `rmtree` for nodes already visible to readers, and a
compensating delete is itself a write that can fail halfway.

So the strategy inverts. P3's rule was "a failure leaves nothing". P4's is **"a failure may leave
plenty, and none of it may be readable as complete"** -- and this pointer is the whole mechanism
for the second half. Writes build a new root that nothing reads; one atomic move publishes it.
Before that move a partial write is invisible and a sweep reclaims it. After it, `previous_root`
is the escape hatch, so compensation is another move rather than an inverse write.

**The check and the act are ONE statement, and that is not a style preference.**
`structure_write_fence.admit_structure_writer` documents why the hard way: validating a claim in a
statement of its own lets a transfer land between the validation and the write. The same applies
here -- a build takes minutes, ownership can change inside that window, and a flip authorised by a
generation read earlier is authorised by a fact that may no longer be true. `publish_root` reads,
decides and writes in a single Cypher statement; a caller that finds no row has lost, and must not
re-read and retry into the winner's state.

**Retention is exactly one previous root** (owner decision, 2026-09-17). Undo always works for the
most recent promotion, and the cost stays bounded rather than growing with how often a project
syncs. The accepted consequence: a bad promotion discovered after a later good one is not
reversible in place, and `restore_previous` refuses rather than silently restoring the wrong
generation. An escape hatch that opens onto the wrong room is worse than one that says no.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.snapshot.view_root import ROOT_COMPLETE, read_root

__all__ = [
    "CANONICAL_VIEW_CONSTRAINTS",
    "ERR_VIEW_ALREADY_CURRENT",
    "ERR_VIEW_ACTOR_REQUIRED",
    "ERR_VIEW_DEGRADED",
    "ERR_VIEW_NO_PREVIOUS",
    "ERR_VIEW_ROOT_UNPUBLISHABLE",
    "ERR_VIEW_SUPERSEDED",
    "ViewError",
    "ViewPointer",
    "mark_degraded",
    "publish_root",
    "read_view",
    "restore_previous",
]

ERR_VIEW_SUPERSEDED = "snapshot.view.superseded"
ERR_VIEW_DEGRADED = "snapshot.view.degraded"
ERR_VIEW_NO_PREVIOUS = "snapshot.view.no_previous"
ERR_VIEW_ROOT_UNPUBLISHABLE = "snapshot.view.root_unpublishable"
ERR_VIEW_ALREADY_CURRENT = "snapshot.view.already_current"
ERR_VIEW_ACTOR_REQUIRED = "snapshot.view.actor_required"

#: Real DDL, shipped with the module so a test can apply the SAME constraint the bootstrap does.
#: A test that creates its own copy proves nothing about the one production runs.
CANONICAL_VIEW_CONSTRAINTS = [
    (
        "CREATE CONSTRAINT canonical_view_identity IF NOT EXISTS "
        "FOR (v:CanonicalView) REQUIRE (v.project_id, v.view_key) IS UNIQUE"
    ),
]


class ViewError(RuntimeError):
    """A refusal carrying a stable code. Never carries a root id or a project path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ViewPointer:
    """Which root a view answers from, and whether it can be trusted."""

    project_id: str
    view_key: str
    generation: int
    current_root: str | None
    #: Exactly one, replaced at each publish. `None` before the second publish.
    previous_root: str | None
    degraded: bool
    degraded_reason: str = ""
    display_name: str = ""
    last_promoted_by: str = ""
    degraded_by: str = ""


def _pointer(row: Any, project_id: str, view_key: str) -> ViewPointer:
    return ViewPointer(
        project_id=project_id,
        view_key=view_key,
        generation=int(row.get("generation") or 0),
        current_root=row.get("current_root"),
        previous_root=row.get("previous_root"),
        degraded=bool(row.get("degraded") or False),
        degraded_reason=str(row.get("degraded_reason") or ""),
        display_name=str(row.get("display_name") or ""),
        last_promoted_by=str(row.get("last_promoted_by") or ""),
        degraded_by=str(row.get("degraded_by") or ""),
    )


_RETURN = (
    "RETURN v.generation AS generation, v.current_root AS current_root, "
    "v.previous_root AS previous_root, coalesce(v.degraded, false) AS degraded, "
    "coalesce(v.degraded_reason, '') AS degraded_reason"
    ", coalesce(v.display_name, '') AS display_name, "
    "coalesce(v.last_promoted_by, '') AS last_promoted_by, "
    "coalesce(v.degraded_by, '') AS degraded_by"
)


def read_view(neo4j: Any, *, project_id: str, view_key: str) -> ViewPointer | None:
    """Return the view, or None when the project has never been promoted.

    Reads are ALWAYS served, including for a degraded view. The plan is explicit: a degraded view
    is fail-closed in the sense that nothing may be promoted INTO it, and reads carry the status
    rather than being refused. Blocking reads would take a project's memory away over a fault in
    the write path.
    """
    rows = neo4j.execute(
        f"MATCH (v:CanonicalView {{project_id: $pid, view_key: $vk}}) {_RETURN}",
        {"pid": project_id, "vk": view_key},
    )
    rows = list(rows)
    return _pointer(rows[0], project_id, view_key) if rows else None


def publish_root(
    neo4j: Any,
    *,
    project_id: str,
    view_key: str,
    root_id: str,
    expected_generation: int,
    actor: str,
    display_name: str = "",
) -> ViewPointer:
    """Make `root_id` the view's current root, or refuse.

    ONE statement. The ROOT checks, the generation check, the degraded check and the pointer move
    happen together, so a transfer, a competing promotion or a sweep cannot land between the
    decision and the act -- the failure `admit_structure_writer` exists to prevent, in a different
    costume.

    **Four things are checked about the root, and the first version of this function checked none
    of them.** A CAS on the generation proves the view has not moved; it proves nothing about what
    is being published into it. Without these, a caller could publish a root still being written
    (readers see half a snapshot as complete), a root belonging to another project or view (a
    cross-project read), or a root a sweep has already retired and begun deleting.

    `SET r.publish_probe` takes the write lock on the root BEFORE its state is read, so the sweeper
    cannot retire it between the read and the flip. `view_root.retire_root` locks the root first
    too, and in the same order, so the two serialise rather than deadlock.

    A caller that loses gets `ERR_VIEW_SUPERSEDED` and must NOT re-read and retry: the generation
    it would read back belongs to the winner, and publishing against it would overwrite a
    promotion that legitimately happened.
    """
    if not actor.strip():
        raise ViewError(ERR_VIEW_ACTOR_REQUIRED, "an authenticated promotion actor is required")
    rows = list(
        neo4j.execute(
            "MATCH (r:ViewRoot {root_id: $root}) "
            "SET r.publish_probe = timestamp() "
            "WITH r "
            "WHERE r.project_id = $pid AND r.view_key = $vk "
            "  AND r.state = $complete "
            "  AND coalesce(r.lease_expires_at, 0) > timestamp() "
            "MERGE (v:CanonicalView {project_id: $pid, view_key: $vk}) "
            "ON CREATE SET v.generation = 0, v.degraded = false "
            "WITH v, r WHERE coalesce(v.generation, 0) = $expected "
            "AND coalesce(v.degraded, false) = false "
            "AND trim($actor) <> '' "
            "AND coalesce(v.current_root, '') <> r.root_id "
            "SET v.previous_root = v.current_root, "
            "    v.current_root = r.root_id, "
            "    v.generation = coalesce(v.generation, 0) + 1, "
            "    v.display_name = CASE WHEN trim($display_name) = '' "
            "      THEN coalesce(v.display_name, $pid) ELSE $display_name END, "
            "    v.last_promoted_by = $actor, v.last_promoted_at = timestamp() "
            f"{_RETURN}",
            {
                "pid": project_id,
                "vk": view_key,
                "root": root_id,
                "expected": expected_generation,
                "complete": ROOT_COMPLETE,
                "actor": actor.strip(),
                "display_name": display_name.strip(),
            },
        )
    )
    if rows:
        return _pointer(rows[0], project_id, view_key)

    # No row means a guard did not match. Re-read ONLY to report which reason -- never to retry.
    # The root is diagnosed first: "this view moved on" would be an actively misleading answer for
    # a caller whose real mistake was publishing something unfinished.
    root = read_root(neo4j, root_id=root_id)
    if root is None or root.project_id != project_id or root.view_key != view_key:
        raise ViewError(
            ERR_VIEW_ROOT_UNPUBLISHABLE,
            "that root does not exist, or does not belong to this project and view",
        )
    if root.state != ROOT_COMPLETE or not root.lease_live:
        raise ViewError(
            ERR_VIEW_ROOT_UNPUBLISHABLE,
            f"that root is {root.state.lower()} with a "
            f"{'live' if root.lease_live else 'expired'} lease; only a complete, still-leased "
            "root may be published",
        )
    current = read_view(neo4j, project_id=project_id, view_key=view_key)
    if current is not None and current.degraded:
        raise ViewError(
            ERR_VIEW_DEGRADED, "this view is degraded and cannot be promoted into"
        )
    if current is not None and current.current_root == root_id:
        # A retry whose first attempt actually succeeded. Publishing again would set
        # `previous_root` and `current_root` to the SAME root and silently destroy the one
        # generation of undo the gate depends on -- so it is refused here and read as success by
        # `promotion.promote_snapshot`, which is the only caller that can know it retried.
        raise ViewError(
            ERR_VIEW_ALREADY_CURRENT, "this root is already the view's current root"
        )
    raise ViewError(
        ERR_VIEW_SUPERSEDED, "this view moved on while the promotion was being built"
    )


def restore_previous(
    neo4j: Any, *, project_id: str, view_key: str, expected_generation: int, actor: str
) -> ViewPointer:
    """Flip back to `previous_root`, or refuse.

    Compensation is a pointer move, not an inverse write, which is what keeps "the undo also
    failed" a tractable case rather than a traversal that can die halfway.

    Refuses when there is no previous root. With a retention of exactly one, that covers both the
    never-promoted-twice case and the already-restored case, and refusing is the whole point:
    restoring a generation the operator did not mean would be worse than telling them no.
    """
    if not actor.strip():
        raise ViewError(ERR_VIEW_ACTOR_REQUIRED, "an authenticated restore actor is required")
    rows = list(
        neo4j.execute(
            "MATCH (v:CanonicalView {project_id: $pid, view_key: $vk}) "
            "WHERE coalesce(v.generation, 0) = $expected AND v.previous_root IS NOT NULL "
            "AND trim($actor) <> '' "
            "SET v.current_root = v.previous_root, "
            "    v.previous_root = NULL, "
            "    v.generation = coalesce(v.generation, 0) + 1, "
            "    v.last_restored_by = $actor, v.last_restored_at = timestamp() "
            f"{_RETURN}",
            {
                "pid": project_id,
                "vk": view_key,
                "expected": expected_generation,
                "actor": actor.strip(),
            },
        )
    )
    if rows:
        return _pointer(rows[0], project_id, view_key)

    current = read_view(neo4j, project_id=project_id, view_key=view_key)
    if current is None or current.previous_root is None:
        raise ViewError(
            ERR_VIEW_NO_PREVIOUS,
            "no previous root is retained for this view; retention is one generation",
        )
    raise ViewError(ERR_VIEW_SUPERSEDED, "this view moved on before the restore")


def mark_degraded(
    neo4j: Any,
    *,
    project_id: str,
    view_key: str,
    reason: str,
    actor: str,
    expected_generation: int | None = None,
    expected_root: str | None = None,
) -> ViewPointer:
    """Record durably that this view cannot be trusted, and block promotion into it.

    Ordinary compensation deliberately leaves the optional guards unset: its failure means the
    state is one no code path intended and must be recorded. A background reconciler supplies both
    guards because another legitimate promotion can race its stale read; in that case degrading
    the newer view would manufacture a fault rather than record one.
    """
    if not actor.strip():
        raise ViewError(ERR_VIEW_ACTOR_REQUIRED, "an authenticated degradation actor is required")
    guarded = expected_generation is not None or expected_root is not None
    match = (
        "MATCH (v:CanonicalView {project_id: $pid, view_key: $vk}) "
        if guarded
        else "MERGE (v:CanonicalView {project_id: $pid, view_key: $vk}) "
        "ON CREATE SET v.generation = 0 "
    )
    rows = list(
        neo4j.execute(
            match + "WITH v WHERE trim($actor) <> '' "
            "AND ($expected IS NULL OR coalesce(v.generation, 0) = $expected) "
            "AND ($root IS NULL OR v.current_root = $root) "
            "SET v.degraded = true, v.degraded_reason = $reason, "
            "v.degraded_by = $actor, v.degraded_at = timestamp() "
            f"{_RETURN}",
            {
                "pid": project_id,
                "vk": view_key,
                "reason": reason,
                "actor": actor.strip(),
                "expected": expected_generation,
                "root": expected_root,
            },
        )
    )
    if not rows:
        if not actor.strip():
            raise ViewError(
                ERR_VIEW_ACTOR_REQUIRED, "an authenticated degradation actor is required"
            )
        raise ViewError(ERR_VIEW_SUPERSEDED, "this view moved on before degradation")
    return _pointer(rows[0], project_id, view_key)
