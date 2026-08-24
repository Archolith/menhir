"""Resolve, mint and bind a project's identity for one scan.

CF-257 phase 1, wiring. Ties together the three pure pieces -- the file
(:mod:`menhir.domain.project_id_file`), the decision
(:mod:`menhir.domain.project_identity_resolution`) and the durable binding
(:mod:`menhir.infrastructure.project_identity_binding`) -- and does the I/O they deliberately
avoid, so each stays testable without a filesystem or a graph.

Returns either a resolved id or the ``needs_decision`` payload the caller should hand back
untouched. Nothing here writes structure; it only settles WHICH identity the scan may write under.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from menhir.domain.project_id_file import (
    MalformedIdentityFile,
    ensure_ignore_rule,
    mint_identity,
    read_identity,
)
from menhir.domain.project_identity_resolution import (
    IdentityAction,
    IdentityCandidate,
    IdentityResolution,
    ResolutionStatus,
    resolve_identity,
)
from menhir.infrastructure.project_identity_binding import bind_project_identity

logger = logging.getLogger(__name__)

__all__ = ["settle_project_identity"]


def _candidate_for(graph_adapter: Any, root_path: str) -> IdentityCandidate | None:
    """The identity this directory might be continuing, with the numbers a decision needs.

    Matched on the recorded ``root_path``, so it covers deletion IN PLACE -- a `git clean`, a
    cleared `.agent/`. A moved repo or a replacement machine has no match here and lands on the
    no-candidate branch, which is a decision rather than a silent mint precisely because those
    cases are indistinguishable from a genuinely new checkout.
    """
    try:
        rows = graph_adapter.neo4j.execute(
            """
            MATCH (p:Entity {structure_role: 'project'})
            WHERE p.root_path IS NOT NULL
              AND toLower(replace(p.root_path, '\\\\', '/')) = toLower(replace($root, '\\\\', '/'))
              AND p.structure_project_id IS NOT NULL
            OPTIONAL MATCH (n:Entity {structure_project_id: p.structure_project_id})
            RETURN p.structure_project_id AS id,
                   p.structure_project AS name,
                   p.root_path AS root,
                   toString(p.last_accessed) AS last_scan,
                   count(n) AS entities
            """,
            {"root": str(root_path).rstrip("/\\")},
        )
    except Exception:  # pragma: no cover - a candidate is an aid, never a gate
        logger.warning("identity candidate lookup failed for %s", root_path, exc_info=True)
        return None
    if not rows or not rows[0].get("id"):
        return None
    row = rows[0]
    return IdentityCandidate(
        project_id=str(row["id"]),
        display_name=str(row.get("name") or Path(root_path).name),
        entity_count=int(row.get("entities") or 0),
        last_scan=str(row.get("last_scan") or ""),
        recorded_root_path=str(row.get("root") or ""),
    )


def settle_project_identity(
    graph_adapter: Any,
    *,
    root_path: str,
    display_name: str,
    identity_action: str | None = None,
    adopt_project_id: str | None = None,
) -> tuple[str | None, IdentityResolution]:
    """Return ``(project_id, resolution)``; ``project_id`` is None when a decision is needed.

    Order matters. The file is read FIRST, so the common path -- an established checkout -- costs
    one stat and never touches the graph. A malformed file refuses here rather than falling
    through to the candidate branch, because "unreadable" must not be treated as "absent": that
    would re-mint over a file that may hold the only record of an id.
    """
    try:
        existing = read_identity(root_path)
    except MalformedIdentityFile:
        raise

    resolution = resolve_identity(
        root_path=str(root_path),
        existing_file_id=existing.project_id if existing else None,
        candidate=None if existing else _candidate_for(graph_adapter, root_path),
        action=IdentityAction(identity_action) if identity_action else None,
        adopt_project_id=adopt_project_id,
    )

    if resolution.status is ResolutionStatus.NEEDS_DECISION:
        return None, resolution

    project_id = resolution.project_id
    if project_id is None:
        # action=new: mint. The ignore rule is a precondition of minting, so it is established
        # first -- an untracked identity file eventually gets committed, and a committed id is
        # inherited by every clone and fork.
        ensure_ignore_rule(root_path)
        project_id = mint_identity(root_path, display_name=display_name).project_id
    elif existing is None:
        # action=adopt: write the adopted id into this checkout so the next scan resolves without
        # a decision.
        ensure_ignore_rule(root_path)
        project_id = mint_identity(
            root_path, project_id=project_id, display_name=display_name
        ).project_id

    # Compare-and-set LAST, so a conflicted identity is refused before any structure is written.
    bind_project_identity(graph_adapter.neo4j, project_id=project_id, root_path=str(root_path))
    return project_id, resolution
