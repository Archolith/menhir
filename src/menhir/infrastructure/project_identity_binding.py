""":ProjectIdentity -- the durable binding from a project id to the one directory that owns it.

CF-257 phase 1. The filesystem primitive (``O_CREAT|O_EXCL`` in :mod:`menhir.domain.project_id_file`)
stops two ids being minted for one directory. It cannot stop the opposite -- **one id appearing in
two directories** -- which is what copying, restoring or rsyncing a tree produces.

**Why the composite key constraint cannot catch that.** ``(structure_project_id, structure_path)``
uniqueness is satisfied perfectly by a copied tree: the paths are identical, so both roots MERGE
onto the same nodes and the graph looks consistent while two directories quietly share one silo --
and ``root_path`` is last-writer-wins, so even the witness is destroyed. That is the original
collision, rebuilt underneath the fix for it.

So the binding is separate state with its own constraints, established by a compare-and-set on
every scan.

Two uniqueness rules, and they protect opposite directions:

``project_id IS UNIQUE``
    One node per identity. Makes ``MERGE`` on the id a compare-and-set.

``(bound_host, root_key) IS UNIQUE``
    **One active identity per directory, per host.** Without it, a transfer left the previous
    identity ALSO naming the root, so two ids claimed one directory and a later file loss resolved
    to whichever the lookup happened to return first. Host is part of the key because a path is not
    unique across machines -- that is the whole reason identity is a minted id rather than a path --
    so ``/srv/app`` on two hosts are two projects and must not contend.

**Retirement nulls ``root_key``.** Verified on Neo4j 5.26.21: a composite uniqueness constraint does
not apply to a node with NULL in any constrained property. That is normally the trap that lets bad
rows escape a constraint; here it is the mechanism, and it is load-bearing, so it is asserted by
test rather than assumed. It also makes the constraint safe to create over existing unstamped
bindings, which carry no ``root_key`` and are therefore not covered until something stamps them.

**The Python rival check is for the error message; the constraint is the enforcement.** Reading the
rivals and then writing cannot be atomic across two statements, so a concurrent claimer can appear
in between. The constraint rejects that at commit. The pre-write check exists because a raw
``ConstraintValidationFailed`` does not tell an operator which directory is contested by which id,
and because it still catches rivals that predate the backfill and so carry no ``root_key``.

**A conflict disables the identity for BOTH roots, including the incumbent.** Refusing only the
newcomer would leave the already-bound directory writing happily into a silo now known to be
ambiguous, and the operator would learn about it whenever something else broke. Marking the
identity conflicted stops both and surfaces the decision immediately.

That is deliberately NOT what happens when two ids contend for one DIRECTORY. There, only the
newcomer is wrong: the incumbent's silo is unambiguous, and poisoning it would break a working
project on the strength of a stray identity file. One id in two directories makes a silo
ambiguous; two ids on one directory makes a claim wrong. Different damage, different remedy.
"""

from __future__ import annotations

import socket
from typing import Any

from menhir.domain.project_identity import normalize_project_root_path
from menhir.infrastructure.project_identity_binding_model import (
    PROJECT_IDENTITY_CONSTRAINT,
    PROJECT_IDENTITY_CONSTRAINTS,
    PROJECT_IDENTITY_ROOT_CONSTRAINT,
    BindingState,
    IdentityBindingConflict,
    IdentityRootContested,
    PendingIdentityPublication,
    ensure_binding_constraint,
)
from menhir.infrastructure.project_identity_binding_transfer import _transfer


def _host() -> str:
    try:
        return socket.gethostname().casefold()
    except OSError:  # pragma: no cover
        return ""


__all__ = [
    "IdentityBindingConflict",
    "IdentityRootContested",
    "BindingState",
    "PendingIdentityPublication",
    "PROJECT_IDENTITY_CONSTRAINT",
    "PROJECT_IDENTITY_ROOT_CONSTRAINT",
    "PROJECT_IDENTITY_CONSTRAINTS",
    "ensure_binding_constraint",
    "bind_project_identity",
    "binding_for_root",
    "pending_identity_publication_for_root",
    "clear_identity_publication_pending",
    "read_binding",
    "clear_conflict",
    "root_key_for",
    "binding_host",
]


def root_key_for(root_path: str) -> str:
    """The normalized directory key a binding claims. Also the second half of the root constraint."""
    return normalize_project_root_path(root_path)


def binding_host() -> str:
    """This host's identity for binding purposes. Exposed so tests can pin it explicitly."""
    return _host()


def _is_constraint_violation(exc: Exception) -> bool:
    code = getattr(exc, "code", "") or ""
    return "ConstraintValidationFailed" in str(code) or "already exists with label" in str(exc)


def _active_rivals(
    neo4j: Any, *, project_id: str, root_key: str, host: str
) -> list[str]:
    """Other active identities claiming this (host, root), by root_key OR recorded path.

    Both, because they cover different eras: `root_key` is the constrained column, and
    `canonical_root_path` is all a binding written before the backfill has. Matching only the
    former would report "no rival" for exactly the rows the constraint also cannot see.

    Normalisation happens in Python, not Cypher. Comparing separator-insensitively in Cypher needs
    an escaped backslash literal, which is easy to get subtly wrong and impossible to notice: a
    mis-escaped pattern simply matches nothing, and "no rival" reads as "clear to proceed" -- a
    silent wrong answer of exactly the kind CF-258 records elsewhere.
    """
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity)
        WHERE coalesce(p.state, 'bound') = 'bound'
          AND p.bound_host = $host
          AND p.project_id <> $project_id
        RETURN p.project_id AS id, p.canonical_root_path AS root, p.root_key AS root_key
        """,
        {"host": host, "project_id": project_id},
    )
    rivals = []
    for row in rows:
        claimed = str(row.get("root_key") or "")
        if not claimed and row.get("root"):
            claimed = root_key_for(str(row["root"]))
        if claimed and claimed == root_key:
            rivals.append(str(row.get("id")))
    return rivals


def bind_project_identity(
    neo4j: Any,
    *,
    project_id: str,
    root_path: str,
    rebind: bool = False,
    resolve_conflict: bool = False,
    publication_pending: bool = False,
) -> BindingState:
    """Bind *project_id* to *root_path* on this host, or raise.

    ``rebind`` is a TRANSFER: the caller has operator authority and is saying this directory
    continues (or newly becomes) that project. It supersedes whatever active binding currently
    claims the directory. Without ``rebind`` a contested directory is refused.

    Both `adopt` and `new` are transfers. `new` was previously not one, so minting a fresh id for
    a directory left the old identity still claiming it -- two active bindings for one root, and
    the erosion this constraint exists to stop. ``resolve_conflict`` is narrower than ``rebind``:
    only operator adoption and :func:`clear_conflict` set it, authorizing the transfer statement
    itself to clear conflict evidence while claiming the named root.
    """
    host = _host()
    root_key = root_key_for(root_path)

    if rebind:
        return _transfer(
            neo4j,
            project_id=project_id,
            root_path=root_path,
            root_key=root_key,
            host=host,
            resolve_conflict=resolve_conflict,
            publication_pending=publication_pending,
        )

    # A brand-new id stamps `root_key` on create, so the root constraint is the FIRST thing that
    # sees a contested directory -- before any Python check runs. That ordering is what makes the
    # refusal safe under concurrency, and it means the raw violation has to be translated here:
    # a `ConstraintValidationFailed` naming two internal node ids tells an operator nothing about
    # which directory is contested.
    try:
        rows = neo4j.execute(
            """
            MERGE (p:ProjectIdentity {project_id: $project_id})
              ON CREATE SET p.canonical_root_path = $root_path,
                            p.state = 'bound',
                            p.bound_at = datetime(),
                            p.bound_host = $host,
                            p.root_key = $root_key,
                            p.claim_generation = 1
            FOREACH (_ IN CASE WHEN $publication_pending THEN [1] ELSE [] END |
                SET p.publication_pending = true,
                    p.publication_pending_host = $host,
                    p.publication_pending_root_key = $root_key,
                    p.publication_pending_generation = coalesce(p.claim_generation, 0),
                    p.publication_pending_at = datetime())
            RETURN p.canonical_root_path AS bound_root, coalesce(p.state, 'bound') AS state,
                   p.bound_host AS bound_host, p.root_key AS root_key,
                   coalesce(p.claim_generation, 0) AS claim_generation
            """,
            {
                "project_id": project_id,
                "root_path": root_path,
                "host": host,
                "root_key": root_key,
                "publication_pending": publication_pending,
            },
        )
    except Exception as exc:
        if not _is_constraint_violation(exc):
            raise
        incumbent = binding_for_root(neo4j, root_path)
        raise IdentityRootContested(
            f"{root_path} is already bound on {host!r} to project id {incumbent or '<unknown>'}, "
            f"but {project_id} was presented for it. Only one identity may own a directory. "
            f"Nothing was changed. Transfer deliberately with an operator-tier identity_action, "
            f"or remove the stale identity file from this checkout."
        ) from exc
    if not rows:  # pragma: no cover - MERGE always returns a row
        raise IdentityBindingConflict(f"could not bind {project_id}")

    bound_root = str(rows[0].get("bound_root") or "")
    state = str(rows[0].get("state") or "bound")
    bound_host = rows[0].get("bound_host")
    stamped_key = rows[0].get("root_key")

    if state == "conflicted":
        raise IdentityBindingConflict(
            f"Project id {project_id} is marked CONFLICTED: it was presented from more than one "
            f"directory. No root may write under it until an operator resolves the conflict "
            f"(adopt one root and mint a fresh id for the other). Recorded root: {bound_root}."
        )

    if state == "superseded":
        raise IdentityBindingConflict(
            f"Project id {project_id} was SUPERSEDED: this directory was transferred to another "
            f"identity. Re-scan without an identity file to pick up the current one, or transfer "
            f"it back explicitly with an operator-tier identity_action."
        )

    host_conflict = bool(bound_host) and bound_host != host
    root_conflict = root_key_for(bound_root) != root_key
    if host_conflict or root_conflict:
        # Disable it for the incumbent too -- see the module docstring.
        neo4j.execute(
            """
            MATCH (p:ProjectIdentity {project_id: $project_id})
            SET p.state = 'conflicted',
                p.conflicting_root_path = $root_path,
                p.conflicting_bound_host = $host,
                p.conflicted_at = datetime()
            """,
            {"project_id": project_id, "root_path": root_path, "host": host},
        )
        difference = (
            f"host {bound_host!r}" if host_conflict else f"root {bound_root}"
        )
        raise IdentityBindingConflict(
            f"Project id {project_id} is bound to {difference} but was presented from host "
            f"{host!r}, root {root_path}. Both are now refused: an id in two checkouts re-creates "
            "exactly the collision this identity scheme removes, and letting the incumbent "
            "continue would hide it. Give one of them a fresh identity."
        )

    rivals = _active_rivals(neo4j, project_id=project_id, root_key=root_key, host=host)
    if rivals:
        raise IdentityRootContested(
            f"{root_path} is already bound on {host!r} to project id {rivals[0]}, but "
            f"{project_id} was presented for it. Only one identity may own a directory. The "
            f"incumbent is left intact: transfer deliberately with an operator-tier "
            f"identity_action, or remove the stale identity file from this checkout."
        )

    if not bound_host or stamped_key != root_key:
        # A binding written before the root constraint existed, or one whose recorded path was
        # normalised differently. Stamping it is what brings it UNDER the constraint; until then
        # it is invisible to the very rule that protects it.
        try:
            neo4j.execute(
                """
                MATCH (p:ProjectIdentity {project_id: $project_id})
                WHERE coalesce(p.state, 'bound') = 'bound'
                SET p.bound_host = $host,
                    p.root_key = $root_key,
                    p.canonical_root_path = $root_path
                """,
                {
                    "project_id": project_id,
                    "root_path": root_path,
                    "host": host,
                    "root_key": root_key,
                },
            )
        except Exception as exc:
            if not _is_constraint_violation(exc):
                raise
            raise IdentityRootContested(
                f"{root_path} was claimed by another identity on {host!r} while {project_id} was "
                f"being bound to it. Refused rather than allowing two active bindings for one "
                f"directory. Re-run the scan; if it persists, an operator must choose."
            ) from exc

    # The generation is deliberately NOT touched on this path. An ordinary re-scan re-binds the
    # same identity to the same directory; bumping there would invalidate a concurrent writer of
    # the SAME identity that had done nothing wrong. Stamping a legacy row leaves it absent, which
    # reads as 0 -- stable, so a scan that settled at 0 can still write at 0.
    return BindingState(
        project_id=project_id,
        canonical_root_path=root_path,
        state="bound",
        claim_generation=int(rows[0].get("claim_generation") or 0),
    )


def binding_for_root(neo4j: Any, root_path: str) -> str | None:
    """The project id already bound to *root_path* ON THIS HOST, or None.

    The host is part of the match on purpose. A path alone is not a unique identity -- two
    machines can carry the same folder layout, which is the reason identity is a minted id rather
    than a path at all. But the same path on the SAME host, already bound, is not ambiguous: it is
    this checkout, and asking an operator to confirm it every time would mean 60 decisions after
    the backfill and an unattended watcher that refreshes nothing until they are made.
    """
    host = _host()
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity)
        WHERE coalesce(p.state, 'bound') = 'bound'
          AND p.bound_host = $host
        RETURN p.project_id AS id, p.canonical_root_path AS root, p.root_key AS root_key
        """,
        {"host": host},
    )
    # Path normalisation happens in Python, not Cypher -- see :func:`_active_rivals`.
    target = root_key_for(root_path)
    for row in rows:
        claimed = str(row.get("root_key") or "")
        if not claimed and row.get("root"):
            claimed = root_key_for(str(row["root"]))
        if claimed and claimed == target:
            return str(row["id"])
    return None


def pending_identity_publication_for_root(
    neo4j: Any, root_path: str
) -> PendingIdentityPublication | None:
    """Return the current root's durable publication repair authorization, if any.

    Every marker field must still agree with the active binding. That makes the marker a narrow
    capability to replace this root's file with this id at this generation, rather than a generic
    permission to overwrite a stale or copied identity file.
    """
    host = _host()
    root_key = root_key_for(root_path)
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity)
        WHERE coalesce(p.state, 'bound') = 'bound'
          AND p.bound_host = $host
          AND p.root_key = $root_key
          AND p.publication_pending = true
          AND p.publication_pending_host = $host
          AND p.publication_pending_root_key = $root_key
          AND p.publication_pending_generation = coalesce(p.claim_generation, 0)
        RETURN p.project_id AS id, p.canonical_root_path AS root,
               coalesce(p.claim_generation, 0) AS claim_generation,
               p.publication_pending AS publication_pending
        """,
        {"host": host, "root_key": root_key},
    )
    for row in rows:
        # The explicit check also keeps the strict offline fake honest: it deliberately ignores
        # predicates it cannot model and omits this field rather than manufacturing authority.
        if row.get("publication_pending") is True and row.get("id"):
            return PendingIdentityPublication(
                project_id=str(row["id"]),
                canonical_root_path=str(row.get("root") or root_path),
                claim_generation=int(row.get("claim_generation") or 0),
            )
    return None


def clear_identity_publication_pending(
    neo4j: Any,
    *,
    project_id: str,
    root_path: str,
    claim_generation: int,
) -> None:
    """Clear only the marker proven current for this id, root, host, and generation."""
    host = _host()
    root_key = root_key_for(root_path)
    rows = neo4j.execute(
        """
        MATCH (p:ProjectIdentity)
        WHERE coalesce(p.state, 'bound') = 'bound'
          AND p.bound_host = $host
          AND p.root_key = $root_key
          AND p.project_id = $expected_project_id
          AND coalesce(p.claim_generation, 0) = $claim_generation
          AND p.publication_pending = true
          AND p.publication_pending_host = $host
          AND p.publication_pending_root_key = $root_key
          AND p.publication_pending_generation = $claim_generation
        REMOVE p.publication_pending, p.publication_pending_host,
               p.publication_pending_root_key, p.publication_pending_generation,
               p.publication_pending_at
        RETURN p.project_id AS id
        """,
        {
            "host": host,
            "root_key": root_key,
            "expected_project_id": project_id,
            "claim_generation": claim_generation,
        },
    )
    if not any(str(row.get("id") or "") == project_id for row in rows):
        raise IdentityBindingConflict(
            f"Could not clear publication recovery for {project_id} at {root_path}: the active "
            "binding or generation changed before publication completed. The marker was left "
            "intact; re-scan to reconcile the current authoritative binding."
        )


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

    Routed through :func:`_transfer` so resolution obeys the same one-identity-per-directory rule
    as every other claim: resolving onto a directory another identity now owns is itself a
    transfer, and doing it with a bare SET would reintroduce the second active binding.
    """
    host = _host()
    return _transfer(
        neo4j,
        project_id=project_id,
        root_path=keep_root_path,
        root_key=root_key_for(keep_root_path),
        host=host,
        resolve_conflict=True,
    )
