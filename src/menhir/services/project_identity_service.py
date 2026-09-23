"""Resolve, mint and bind a project's identity for one scan.

CF-257, wiring. Ties together the decision (:mod:`menhir.domain.project_identity_resolution`) and
the durable binding (:mod:`menhir.infrastructure.project_identity_binding`), and does the I/O they
deliberately avoid, so each stays testable without a graph.

**Identity lives only in the graph.** Menhir writes nothing into a checkout: no identity file, no
ignore rule, no lock file. A directory is verified from durable facts -- this host's active
binding for it, plus proof that the checkout is the one the binding recorded (its ``origin``; for
a legacy binding that recorded none, the legacy ``.agent/project-id`` it left behind, which is
only ever READ). Anything unverified is a decision.

Returns either a claim or the ``needs_decision`` payload the caller should hand back untouched.
Nothing here writes structure; it only settles WHICH identity the scan may write under.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from menhir.domain.project_id_file import ProjectIdFileError, read_identity
from menhir.domain.project_identity_resolution import (
    IdentityAction,
    IdentityCandidate,
    IdentityResolution,
    resolve_identity,
)
from menhir.infrastructure.git_binding import read_origin
from menhir.infrastructure.project_identity_binding import (
    RootBinding,
    bind_project_identity,
    binding_host,
    root_binding,
    root_key_for,
)
from menhir.infrastructure.structure_write_fence import IdentityClaim

logger = logging.getLogger(__name__)

__all__ = ["settle_project_identity", "IdentityClaim"]

#: Why a directory with an active binding still needs a decision.
REASON_REPOSITORY_CHANGED = "repository_changed"
REASON_LEGACY_UNVERIFIED = "legacy_binding_unverified"


def _project_rows(graph_adapter: Any) -> list[dict[str, Any]]:
    try:
        return list(
            graph_adapter.neo4j.execute(
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
        )
    except Exception:  # pragma: no cover - a candidate is an aid, never a gate
        logger.warning("identity candidate lookup failed", exc_info=True)
        return []


def _as_candidate(row: dict[str, Any], root_path: str) -> IdentityCandidate:
    return IdentityCandidate(
        project_id=str(row["id"]),
        display_name=str(row.get("name") or Path(root_path).name),
        entity_count=int(row.get("entities") or 0),
        last_scan=str(row.get("last_scan") or ""),
        recorded_root_path=str(row.get("root") or ""),
    )


def _candidates(
    graph_adapter: Any, root_path: str, *, also: tuple[str | None, ...] = ()
) -> list[IdentityCandidate]:
    """Identities this directory might continue, with the numbers a decision needs.

    The project recorded at this directory (deletion in place, a different clone), plus any id
    named in *also* -- the current binding, or a legacy identity file (a moved checkout).
    """
    rows = _project_rows(graph_adapter)
    target_root_key = root_key_for(root_path)
    found: list[IdentityCandidate] = []
    seen: set[str] = set()
    for row in rows:
        if (
            row.get("id")
            and row.get("root") is not None
            and root_key_for(str(row["root"])) == target_root_key
        ):
            found.append(_as_candidate(row, root_path))
            seen.add(str(row["id"]))
            break
    for extra in also:
        if not extra or extra in seen:
            continue
        row = next((r for r in rows if str(r.get("id") or "") == extra), None)
        found.append(
            _as_candidate(row, root_path)
            if row is not None
            else IdentityCandidate(
                project_id=extra,
                display_name=Path(root_path).name,
                entity_count=0,
                last_scan="",
                recorded_root_path="",
            )
        )
        seen.add(extra)
    return found


def _legacy_identity_id(root_path: str) -> str | None:
    """The id a legacy ``.agent/project-id`` names, or None. Read only; malformed is ignored."""
    try:
        legacy = read_identity(root_path)
    except (ProjectIdFileError, OSError, ValueError):
        return None
    return legacy.project_id if legacy is not None else None


def _unverified_reason(bound: RootBinding, *, root_path: str, repository: str) -> str | None:
    """Why *bound* cannot be trusted for this checkout, or None when it is the same checkout."""
    if bound.repository is not None:
        return None if bound.repository == repository else REASON_REPOSITORY_CHANGED
    # Recorded before repositories were: the legacy file was this binding's only proof of checkout.
    if _legacy_identity_id(root_path) == bound.project_id:
        return None
    return REASON_LEGACY_UNVERIFIED


def settle_project_identity(
    graph_adapter: Any,
    *,
    root_path: str,
    display_name: str,
    identity_action: str | None = None,
    adopt_project_id: str | None = None,
) -> tuple[IdentityClaim | None, IdentityResolution]:
    """Return ``(claim, resolution)``; the claim is None when a decision is needed.

    A CLAIM, not just an id: it carries the generation and the directory the binding was for, and
    the write boundary re-validates all three under a lock, so a scan settled before a transfer
    cannot write after it.
    """
    del display_name  # identity is never derived from the name
    neo4j = graph_adapter.neo4j
    repository = read_origin(Path(root_path))
    action = IdentityAction(identity_action) if identity_action else None

    verified: str | None = None
    reason = ""
    candidates: list[IdentityCandidate] = []
    if action is None:
        bound = root_binding(neo4j, str(root_path))
        if bound is None:
            candidates = _candidates(
                graph_adapter, str(root_path), also=(_legacy_identity_id(str(root_path)),)
            )
        else:
            unverified = _unverified_reason(bound, root_path=str(root_path), repository=repository)
            if unverified is None:
                verified = bound.project_id
            else:
                reason = unverified
                candidates = _candidates(graph_adapter, str(root_path), also=(bound.project_id,))
    else:
        candidates = _candidates(graph_adapter, str(root_path))

    resolution = resolve_identity(
        root_path=str(root_path),
        verified_project_id=verified,
        candidates=candidates,
        action=action,
        adopt_project_id=adopt_project_id,
        reason=reason,
    )
    if not resolution.resolved:
        return None, resolution

    project_id = resolution.project_id
    if project_id is None or action is IdentityAction.NEW:
        project_id = str(uuid.uuid4())

    # BOTH actions transfer. `adopt` re-points an existing identity at this directory; `new`
    # abandons the one this directory holds. Treating only adopt as a transfer left `new` minting
    # a fresh id while the old binding still claimed the root -- two active bindings for one
    # directory, which is the erosion the root constraint exists to stop.
    binding = bind_project_identity(
        neo4j,
        project_id=project_id,
        root_path=str(root_path),
        rebind=action is not None,
        resolve_conflict=action is IdentityAction.ADOPT,
        repository=repository,
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
