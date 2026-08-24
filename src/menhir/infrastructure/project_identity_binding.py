""":ProjectIdentity -- the durable binding from a project id to the one directory that owns it.

CF-257 phase 1. The filesystem primitive (``O_CREAT|O_EXCL`` in :mod:`menhir.domain.project_id_file`)
stops two ids being minted for one directory. It cannot stop the opposite -- **one id appearing in
two directories** -- which is what copying, restoring or rsyncing a tree produces.

**Why the composite key constraint cannot catch that.** ``(structure_project_id, structure_path)``
uniqueness is satisfied perfectly by a copied tree: the paths are identical, so both roots MERGE
onto the same nodes and the graph looks consistent while two directories quietly share one silo --
and ``root_path`` is last-writer-wins, so even the witness is destroyed. That is the original
collision, rebuilt underneath the fix for it.

So the binding is separate state with its own uniqueness constraint, established by a
compare-and-set on every scan.

**A conflict disables the identity for BOTH roots, including the incumbent.** Refusing only the
newcomer would leave the already-bound directory writing happily into a silo now known to be
ambiguous, and the operator would learn about it whenever something else broke. Marking the
identity conflicted stops both and surfaces the decision immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "IdentityBindingConflict",
    "BindingState",
    "PROJECT_IDENTITY_CONSTRAINT",
    "ensure_binding_constraint",
    "bind_project_identity",
    "read_binding",
    "clear_conflict",
]

PROJECT_IDENTITY_CONSTRAINT = (
    "CREATE CONSTRAINT project_identity_id_unique IF NOT EXISTS "
    "FOR (p:ProjectIdentity) REQUIRE p.project_id IS UNIQUE"
)


class IdentityBindingConflict(RuntimeError):
    """One project id was presented from two different directories."""


@dataclass(frozen=True)
class BindingState:
    project_id: str
    canonical_root_path: str
    state: str  # "bound" | "conflicted"


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def ensure_binding_constraint(neo4j: Any) -> None:
    """Create the uniqueness constraint if absent.

    Separate from :func:`bind_project_identity` so a migration can assert it up front and fail
    before writing anything, rather than discovering mid-run that the guarantee is missing.
    """
    neo4j.execute(PROJECT_IDENTITY_CONSTRAINT, {})


def bind_project_identity(neo4j: Any, *, project_id: str, root_path: str) -> BindingState:
    """Bind *project_id* to *root_path*, or raise if it is already bound elsewhere.

    One statement does the compare and the set: `MERGE` on the id, then a conditional `SET` that
    only lands when the binding is new. Reading the row and deciding in Python would leave a
    window in which a second process binds the same id to a different root and both believe they
    won.
    """
    rows = neo4j.execute(
        """
        MERGE (p:ProjectIdentity {project_id: $project_id})
          ON CREATE SET p.canonical_root_path = $root_path,
                        p.state = 'bound',
                        p.bound_at = datetime()
        RETURN p.canonical_root_path AS bound_root, coalesce(p.state, 'bound') AS state
        """,
        {"project_id": project_id, "root_path": root_path},
    )
    if not rows:  # pragma: no cover - MERGE always returns a row
        raise IdentityBindingConflict(f"could not bind {project_id}")

    bound_root = str(rows[0].get("bound_root") or "")
    state = str(rows[0].get("state") or "bound")

    if state == "conflicted":
        raise IdentityBindingConflict(
            f"Project id {project_id} is marked CONFLICTED: it was presented from more than one "
            f"directory. No root may write under it until an operator resolves the conflict "
            f"(adopt one root and mint a fresh id for the other). Recorded root: {bound_root}."
        )

    if _norm(bound_root) != _norm(root_path):
        # Disable it for the incumbent too -- see the module docstring.
        neo4j.execute(
            """
            MATCH (p:ProjectIdentity {project_id: $project_id})
            SET p.state = 'conflicted',
                p.conflicting_root_path = $root_path,
                p.conflicted_at = datetime()
            """,
            {"project_id": project_id, "root_path": root_path},
        )
        raise IdentityBindingConflict(
            f"Project id {project_id} is bound to {bound_root} but was presented from "
            f"{root_path}. Both are now refused: an id in two directories re-creates exactly the "
            "collision this identity scheme removes, and letting the incumbent continue would "
            "hide it. Give one of them a fresh identity."
        )

    return BindingState(project_id=project_id, canonical_root_path=bound_root, state=state)


def read_binding(neo4j: Any, project_id: str) -> BindingState | None:
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity {project_id: $project_id})
        RETURN p.canonical_root_path AS root, coalesce(p.state, 'bound') AS state
        """,
        {"project_id": project_id},
    )
    if not rows:
        return None
    return BindingState(
        project_id=project_id,
        canonical_root_path=str(rows[0].get("root") or ""),
        state=str(rows[0].get("state") or "bound"),
    )


def clear_conflict(neo4j: Any, *, project_id: str, keep_root_path: str) -> BindingState:
    """Operator resolution: re-bind a conflicted identity to one root.

    Deliberately requires naming the root to keep. There is no "just clear it" -- the whole point
    of the conflicted state is that the system cannot tell which directory is the real one.
    """
    neo4j.execute(
        """
        MATCH (p:ProjectIdentity {project_id: $project_id})
        SET p.state = 'bound',
            p.canonical_root_path = $root_path,
            p.conflicting_root_path = null,
            p.conflicted_at = null,
            p.bound_at = datetime()
        """,
        {"project_id": project_id, "root_path": keep_root_path},
    )
    return BindingState(project_id=project_id, canonical_root_path=keep_root_path, state="bound")
