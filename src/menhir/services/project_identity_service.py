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
    binding_host,
    clear_identity_publication_pending,
    pending_identity_publication_for_root,
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
              AND p.structure_project_id IS NOT NULL
            OPTIONAL MATCH (n:Entity {structure_project_id: p.structure_project_id})
            RETURN p.structure_project_id AS id,
                   p.structure_project AS name,
                   p.root_path AS root,
                   toString(p.last_accessed) AS last_scan,
                   count(n) AS entities
            """,
            {},
        )
    except Exception:  # pragma: no cover - a candidate is an aid, never a gate
        logger.warning("identity candidate lookup failed for %s", root_path, exc_info=True)
        return None
    target_root_key = root_key_for(root_path)
    row = next(
        (
            candidate
            for candidate in rows
            if candidate.get("id")
            and candidate.get("root") is not None
            and root_key_for(str(candidate["root"])) == target_root_key
        ),
        None,
    )
    if row is None:
        return None
    return IdentityCandidate(
        project_id=str(row["id"]),
        display_name=str(row.get("name") or Path(root_path).name),
        entity_count=int(row.get("entities") or 0),
        last_scan=str(row.get("last_scan") or ""),
        recorded_root_path=str(row.get("root") or ""),
    )


def _publication_candidate(
    pending: Any, *, root_path: str, display_name: str
) -> IdentityCandidate:
    """Render interrupted publication as decision evidence, never checkout authority."""
    return IdentityCandidate(
        project_id=pending.project_id,
        display_name=display_name,
        entity_count=0,
        last_scan="",
        recorded_root_path=str(pending.canonical_root_path or root_path),
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

    pending = pending_identity_publication_for_root(graph_adapter.neo4j, str(root_path))
    if pending is not None:
        # The marker proves that publication was interrupted, but host/path/generation does not
        # identify the checkout currently occupying that directory. Only a file already naming
        # the pending id permits unattended marker clearing. Missing or different files require
        # an explicit operator decision and cause no filesystem side effect.
        if existing is None or existing.project_id != pending.project_id:
            return None, IdentityResolution(
                status=ResolutionStatus.NEEDS_DECISION,
                reason=(
                    "identity_file_missing_publication_pending"
                    if existing is None
                    else "identity_file_mismatch_publication_pending"
                ),
                directory=str(root_path),
                candidates=[
                    _publication_candidate(
                        pending, root_path=str(root_path), display_name=display_name
                    )
                ],
            )
        with identity_publication_lock(root_path):
            return _settle_project_identity_locked(
                graph_adapter,
                root_path=root_path,
                display_name=display_name,
                identity_action=identity_action,
                adopt_project_id=adopt_project_id,
                existing=read_identity(root_path),
            )

    if existing is None:
        # A decision is a read-only outcome. Resolve it before taking the publication lock,
        # because that lock's stable cross-process lock file is `.agent/.gitignore` and creating
        # it would mutate the checkout before the caller has chosen adopt or new.
        preflight = resolve_identity(
            root_path=str(root_path),
            existing_file_id=None,
            candidate=_candidate_for(graph_adapter, root_path),
            action=None,
            adopt_project_id=None,
        )
        return None, preflight

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

    if identity_action is None:
        pending = pending_identity_publication_for_root(graph_adapter.neo4j, str(root_path))
        if pending is not None:
            if existing is None or existing.project_id != pending.project_id:
                return None, IdentityResolution(
                    status=ResolutionStatus.NEEDS_DECISION,
                    reason=(
                        "identity_file_missing_publication_pending"
                        if existing is None
                        else "identity_file_mismatch_publication_pending"
                    ),
                    directory=str(root_path),
                    candidates=[
                        _publication_candidate(
                            pending, root_path=str(root_path), display_name=display_name
                        )
                    ],
                )
            _clear_publication_marker(
                graph_adapter.neo4j,
                project_id=pending.project_id,
                root_path=str(root_path),
                claim_generation=pending.claim_generation,
            )
            resolution = resolve_identity(
                root_path=str(root_path),
                existing_file_id=pending.project_id,
                candidate=None,
            )
            return (
                IdentityClaim(
                    project_id=pending.project_id,
                    root_key=root_key_for(str(root_path)),
                    generation=pending.claim_generation,
                    host=binding_host(),
                ),
                resolution,
            )

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
    needs_publication = existing is None or existing.project_id != project_id
    binding = bind_project_identity(
        graph_adapter.neo4j,
        project_id=project_id,
        root_path=str(root_path),
        rebind=transferring,
        resolve_conflict=identity_action == IdentityAction.ADOPT.value,
        # Record an interrupted graph-first publication so an operator can identify and explicitly
        # adopt the intended id. The marker is evidence for that decision, not checkout authority.
        publication_pending=needs_publication,
    )

    # Publication can still fail after the binding committed, and there is no ordering that
    # removes that -- two stores cannot be written atomically. The graph transaction therefore
    # records a root/host/generation-scoped marker before publication. A later ordinary scan may
    # clear it only when the file already names that id; otherwise it returns a typed decision.
    if needs_publication:
        _publish_identity_file(
            root_path, project_id=project_id, display_name=display_name, existing=existing
        )
        _clear_publication_marker(
            graph_adapter.neo4j,
            project_id=project_id,
            root_path=str(root_path),
            claim_generation=binding.claim_generation,
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
    try:
        ensure_ignore_rule(root_path)
        if existing is not None:
            Path(existing.path).unlink()
        mint_identity(root_path, project_id=project_id, display_name=display_name)
    except Exception as exc:
        raise ProjectIdentityPublicationFailed(
            f"{project_id} is now bound to {root_path} in the graph, but the identity file could "
            f"not be written ({exc}). The graph retained a publication-recovery marker; retry "
            f"with an explicit operator adopt of this id. An unattended re-scan exposes the id "
            f"as a candidate but cannot treat host/path as checkout authority."
        ) from exc


def _clear_publication_marker(
    neo4j: Any, *, project_id: str, root_path: str, claim_generation: int
) -> None:
    try:
        clear_identity_publication_pending(
            neo4j,
            project_id=project_id,
            root_path=root_path,
            claim_generation=claim_generation,
        )
    except Exception as exc:
        raise ProjectIdentityPublicationFailed(
            f"The identity file for {project_id} at {root_path} was published, but its durable "
            f"recovery marker could not be cleared ({exc}). Re-scan this directory: only a "
            "matching current identity file permits unattended marker clearing."
        ) from exc
