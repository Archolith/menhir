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
import uuid
from pathlib import Path
from typing import Any

from menhir.domain.project_id_file import (
    MalformedIdentityFile,
    ensure_ignore_rule,
    identity_publication_lock,
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
from menhir.infrastructure.project_identity_binding import (
    bind_project_identity,
    binding_for_root,
    binding_host,
    root_key_for,
)
from menhir.infrastructure.structure_write_fence import IdentityClaim

logger = logging.getLogger(__name__)

__all__ = ["settle_project_identity", "ProjectIdentityPublicationFailed", "IdentityClaim"]


class ProjectIdentityPublicationFailed(RuntimeError):
    """The binding committed but the identity file could not be written."""


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
) -> tuple[IdentityClaim | None, IdentityResolution]:
    """Return ``(claim, resolution)``; the claim is None when a decision is needed.

    A CLAIM, not just an id. The id alone was what every writer checked, and an id is still
    populated after the identity behind it has been superseded -- so a scan settled minutes ago
    could write into a directory that had since changed hands. The claim carries the generation
    and the directory the binding was for, and the write boundary re-validates all three under a
    lock.

    Order matters. The file is read FIRST, so the common path -- an established checkout -- costs
    one stat and never touches the graph. A malformed file refuses here rather than falling
    through to the candidate branch, because "unreadable" must not be treated as "absent": that
    would re-mint over a file that may hold the only record of an id.
    """
    if identity_action is not None:
        with identity_publication_lock(root_path):
            return _settle_project_identity_locked(
                graph_adapter,
                root_path=root_path,
                display_name=display_name,
                identity_action=identity_action,
                adopt_project_id=adopt_project_id,
                existing=read_identity(root_path),
            )

    try:
        existing = read_identity(root_path)
    except MalformedIdentityFile:
        raise

    if existing is None:
        with identity_publication_lock(root_path):
            return _settle_project_identity_locked(
                graph_adapter,
                root_path=root_path,
                display_name=display_name,
                identity_action=identity_action,
                adopt_project_id=adopt_project_id,
                existing=read_identity(root_path),
            )

    return _settle_project_identity_locked(
        graph_adapter,
        root_path=root_path,
        display_name=display_name,
        identity_action=identity_action,
        adopt_project_id=adopt_project_id,
        existing=existing,
    )


def _settle_project_identity_locked(
    graph_adapter: Any,
    *,
    root_path: str,
    display_name: str,
    identity_action: str | None,
    adopt_project_id: str | None,
    existing: Any,
) -> tuple[IdentityClaim | None, IdentityResolution]:
    """Settle after the caller has re-read any mutable identity-file state."""

    # A binding that already names THIS directory on THIS host is not an open question -- it is
    # this checkout, recorded server-side. Treating it as a decision meant every one of the 60
    # projects backfilled in phase 2b would have needed an operator answer before it could be
    # re-scanned, and the unattended watcher would have refreshed nothing in the meantime. The
    # host is part of the match because a path alone is not unique across machines, which is the
    # whole reason identity is a minted id rather than a path.
    bound_id = None
    if existing is None and identity_action is None:
        try:
            bound_id = binding_for_root(graph_adapter.neo4j, str(root_path))
        except Exception:  # pragma: no cover - an aid, never a gate
            logger.warning("binding lookup failed for %s", root_path, exc_info=True)
        # Publication is deliberately NOT done here. It happens once, at the tail, after the
        # binding is confirmed -- doing it here as well meant this branch published the file and
        # the tail then tried to publish it again, and `mint_identity` is O_EXCL, so the second
        # attempt raised FileExistsError on the ordinary self-healing path.

    resolution = resolve_identity(
        root_path=str(root_path),
        existing_file_id=(existing.project_id if existing else None) or bound_id,
        candidate=None if existing else _candidate_for(graph_adapter, root_path),
        action=IdentityAction(identity_action) if identity_action else None,
        adopt_project_id=adopt_project_id,
    )

    if resolution.status is ResolutionStatus.NEEDS_DECISION:
        return None, resolution

    project_id = resolution.project_id
    # BOTH actions transfer. `adopt` re-points an existing identity at this directory; `new`
    # abandons the one this directory holds. Treating only adopt as a transfer left `new` minting
    # a fresh id while the old binding still claimed the root -- two active bindings for one
    # directory, which is the erosion the root constraint exists to stop.
    transferring = identity_action in (
        IdentityAction.ADOPT.value,
        IdentityAction.NEW.value,
    )

    if project_id is None or identity_action == IdentityAction.NEW.value:
        project_id = str(uuid.uuid4())

    # THE GRAPH FIRST, THE FILE SECOND. Publishing first meant a refused binding left the checkout
    # holding an id the graph had rejected -- and on the `new` path the previous file was already
    # unlinked, so a failed transfer destroyed the only local record of the id whose silo the
    # project owns. Bind first and a refusal costs nothing: the file on disk is still the truth it
    # was before the call.
    binding = bind_project_identity(
        graph_adapter.neo4j,
        project_id=project_id,
        root_path=str(root_path),
        rebind=transferring,
        resolve_conflict=identity_action == IdentityAction.ADOPT.value,
    )

    # Publication can still fail after the binding committed, and there is no ordering that
    # removes that -- two stores cannot be written atomically. What makes it recoverable is that
    # the graph, not the file, is authoritative for (host, root): the next scan finds no file,
    # `binding_for_root` returns the bound id, and the file is re-published. A scan that instead
    # finds a STALE file naming the superseded id is refused rather than silently re-pointed, so
    # the failure surfaces instead of resurrecting the abandoned identity.
    if existing is None or existing.project_id != project_id:
        _publish_identity_file(
            root_path, project_id=project_id, display_name=display_name, existing=existing
        )
    return (
        IdentityClaim(
            project_id=project_id,
            root_key=root_key_for(str(root_path)),
            generation=binding.claim_generation,
            host=binding_host(),
        ),
        resolution,
    )


def _publish_identity_file(
    root_path: str, *, project_id: str, display_name: str, existing: Any
) -> None:
    ensure_ignore_rule(root_path)
    if existing is not None:
        Path(existing.path).unlink()
    try:
        mint_identity(root_path, project_id=project_id, display_name=display_name)
    except Exception as exc:
        raise ProjectIdentityPublicationFailed(
            f"{project_id} is now bound to {root_path} in the graph, but the identity file could "
            f"not be written ({exc}). Nothing is lost: re-scan this directory and the binding "
            f"will re-publish it. Until then this checkout resolves its identity from the graph "
            f"on every scan."
        ) from exc
