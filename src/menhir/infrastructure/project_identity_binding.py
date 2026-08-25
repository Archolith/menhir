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
from dataclasses import dataclass
from typing import Any


def _host() -> str:
    try:
        return socket.gethostname().casefold()
    except OSError:  # pragma: no cover
        return ""


__all__ = [
    "IdentityBindingConflict",
    "IdentityRootContested",
    "BindingState",
    "PROJECT_IDENTITY_CONSTRAINT",
    "PROJECT_IDENTITY_ROOT_CONSTRAINT",
    "PROJECT_IDENTITY_CONSTRAINTS",
    "ensure_binding_constraint",
    "bind_project_identity",
    "binding_for_root",
    "read_binding",
    "clear_conflict",
    "root_key_for",
    "binding_host",
]

PROJECT_IDENTITY_CONSTRAINT = (
    "CREATE CONSTRAINT project_identity_id_unique IF NOT EXISTS "
    "FOR (p:ProjectIdentity) REQUIRE p.project_id IS UNIQUE"
)

#: One ACTIVE binding per (host, normalized root). Retired bindings null `root_key` and so fall
#: outside it -- see the module docstring.
PROJECT_IDENTITY_ROOT_CONSTRAINT = (
    "CREATE CONSTRAINT project_identity_root_unique IF NOT EXISTS "
    "FOR (p:ProjectIdentity) REQUIRE (p.bound_host, p.root_key) IS UNIQUE"
)

PROJECT_IDENTITY_CONSTRAINTS = (
    PROJECT_IDENTITY_CONSTRAINT,
    PROJECT_IDENTITY_ROOT_CONSTRAINT,
)


class IdentityBindingConflict(RuntimeError):
    """One project id was presented from two different directories."""


class IdentityRootContested(RuntimeError):
    """One directory was claimed by two different project ids on the same host."""


@dataclass(frozen=True)
class BindingState:
    project_id: str
    canonical_root_path: str
    state: str  # "bound" | "conflicted" | "superseded"
    #: Bumped every time this identity CLAIMS a directory. A scan settles under one generation and
    #: must still hold it when it writes; see :class:`~menhir.infrastructure.structure_write_fence.
    #: IdentityClaim`. Without it, transferring a root away and back would let a scan settled
    #: before the round trip write as though nothing had happened -- the state and root checks both
    #: pass, and only the generation records that the directory changed hands in between.
    claim_generation: int = 0


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def root_key_for(root_path: str) -> str:
    """The normalized directory key a binding claims. Also the second half of the root constraint."""
    return _norm(root_path)


def binding_host() -> str:
    """This host's identity for binding purposes. Exposed so tests can pin it explicitly."""
    return _host()


def _is_constraint_violation(exc: Exception) -> bool:
    code = getattr(exc, "code", "") or ""
    return "ConstraintValidationFailed" in str(code) or "already exists with label" in str(exc)


def ensure_binding_constraint(neo4j: Any) -> None:
    """Create both uniqueness constraints if absent.

    Separate from :func:`bind_project_identity` so a migration can assert them up front and fail
    before writing anything, rather than discovering mid-run that the guarantee is missing. Also
    invoked from the phase-1 schema bootstrap, because a constraint that only a migration script
    creates is one a fresh deployment does not have.
    """
    for statement in PROJECT_IDENTITY_CONSTRAINTS:
        neo4j.execute(statement, {})


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
            claimed = _norm(str(row["root"]))
        if claimed and claimed == root_key:
            rivals.append(str(row.get("id")))
    return rivals


def bind_project_identity(
    neo4j: Any, *, project_id: str, root_path: str, rebind: bool = False
) -> BindingState:
    """Bind *project_id* to *root_path* on this host, or raise.

    ``rebind`` is a TRANSFER: the caller has operator authority and is saying this directory
    continues (or newly becomes) that project. It supersedes whatever active binding currently
    claims the directory. Without ``rebind`` a contested directory is refused.

    Both `adopt` and `new` are transfers. `new` was previously not one, so minting a fresh id for
    a directory left the old identity still claiming it -- two active bindings for one root, and
    the erosion this constraint exists to stop.
    """
    host = _host()
    root_key = root_key_for(root_path)

    if rebind:
        return _transfer(
            neo4j, project_id=project_id, root_path=root_path, root_key=root_key, host=host
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
            RETURN p.canonical_root_path AS bound_root, coalesce(p.state, 'bound') AS state,
                   p.bound_host AS bound_host, p.root_key AS root_key,
                   coalesce(p.claim_generation, 0) AS claim_generation
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

    if _norm(bound_root) != root_key:
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

    rivals = _active_rivals(neo4j, project_id=project_id, root_key=root_key, host=host)
    if rivals:
        raise IdentityRootContested(
            f"{root_path} is already bound on {host!r} to project id {rivals[0]}, but "
            f"{project_id} was presented for it. Only one identity may own a directory. The "
            f"incumbent is left intact: transfer deliberately with an operator-tier "
            f"identity_action, or remove the stale identity file from this checkout."
        )

    if bound_host != host or stamped_key != root_key:
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


def _transfer(
    neo4j: Any, *, project_id: str, root_path: str, root_key: str, host: str
) -> BindingState:
    """Retire every other active claim on this (host, root) and claim it, in ONE statement.

    One statement is one transaction. Retiring in one and claiming in another leaves a window in
    which the directory has no owner (a concurrent scan mints a third identity into it) or two
    (the claim fails and the retirement stands). Verified on Neo4j 5.26.21: two concurrent
    executions of this statement do not both commit -- the loser fails on lock contention or the
    root constraint, and exactly one active binding remains.

    **A transfer is refused while any registered structure writer could be writing this root.**
    An earlier version recorded that window as an accepted assumption -- "last-writer-wins among
    authorised callers" -- which was true of the BINDINGS and false of the data. Transfer X
    succeeds, transfer Y supersedes X, and X's already-settled scan then writes minutes later
    under a superseded identity, carrying the per-project stale prune into a silo that directory
    no longer owns. Last-writer-wins for a pointer does not make a delayed write through the old
    pointer safe.

    So the writer registry is consulted IN THIS STATEMENT, contending on the same
    `:StructureWriteFence` singleton that admission locks, and an entry that cannot be proven
    irrelevant blocks. Two operators may still transfer in sequence; what they cannot do is
    transfer out from under a writer that is already admitted, and a writer whose claim was
    superseded before it was admitted is refused there.

    The remaining ordering is genuinely benign: two transfers with no writer in flight serialise
    and the later wins, and any scan settled under the earlier one is refused at admission by its
    claim generation rather than by this check.
    """
    conflicted = neo4j.execute(
        """
        MATCH (p:ProjectIdentity {project_id: $project_id})
        WHERE coalesce(p.state, 'bound') = 'conflicted'
        RETURN p.project_id AS id
        """,
        {"project_id": project_id},
    )
    if conflicted:
        raise IdentityBindingConflict(
            f"Project id {project_id} is marked CONFLICTED and cannot be transferred until an "
            f"operator resolves it by naming the root to keep."
        )

    # Which rivals to retire is decided in Python, for the normalisation reason in
    # :func:`_active_rivals` -- but the retirement and the claim are ONE statement, so a rival
    # that appears after this read does not slip through: it holds the same (host, root_key), the
    # constraint rejects the claim, and the whole statement -- retirement included -- rolls back.
    rival_ids = _active_rivals(neo4j, project_id=project_id, root_key=root_key, host=host)
    from menhir.infrastructure.structure_write_fence import writers_holding_identities

    # Every identity whose writers this transfer could invalidate: the incumbents losing the
    # directory, AND the target -- which may be mid-write against the root it is leaving.
    lock_ids = sorted({*rival_ids, project_id})
    try:
        rows = neo4j.execute(
            """
            MATCH (n:ProjectIdentity) WHERE n.project_id IN $lock_ids
            SET n.last_transfer_probe = timestamp()
            WITH collect(n) AS locked
            WITH locked,
                 reduce(c = 0, x IN locked | c + size(coalesce(x.active_writers, []))) AS held,
                 [x IN locked WHERE x.project_id IN $rival_ids] AS rivals
            WHERE held = 0
            FOREACH (o IN rivals |
                SET o.previous_root_key = o.root_key,
                    o.root_key = null,
                    o.state = 'superseded',
                    o.superseded_by = $project_id,
                    o.superseded_at = datetime())
            WITH size(rivals) AS retired
            MERGE (p:ProjectIdentity {project_id: $project_id})
              ON CREATE SET p.bound_at = datetime()
            SET p.previous_root_path = p.canonical_root_path,
                p.canonical_root_path = $root_path,
                p.state = 'bound',
                p.bound_host = $host,
                p.root_key = $root_key,
                p.rebound_at = datetime(),
                p.claim_generation = coalesce(p.claim_generation, 0) + 1
            RETURN retired, p.claim_generation AS claim_generation
            """,
            {
                "project_id": project_id,
                "root_path": root_path,
                "host": host,
                "root_key": root_key,
                "rival_ids": rival_ids,
                "lock_ids": lock_ids,
            },
        )
    except Exception as exc:
        if not _is_constraint_violation(exc):
            raise
        raise IdentityRootContested(
            f"{root_path} was claimed concurrently on {host!r} while transferring it to "
            f"{project_id}. Nothing was changed. Re-issue the transfer."
        ) from exc

    if not rows:
        # `WHERE held = 0` filtered the row out, so nothing after it ran: no retirement, no claim.
        # Only `last_transfer_probe` was written, and that is an inert timestamp -- the price of
        # taking the lock before reading the value the decision depends on.
        blockers = writers_holding_identities(neo4j, lock_ids)
        detail = (
            ", ".join(
                f"{b['id']} on {b['identity']} ({b['label'] or 'unlabelled'}, {b['age_s']}s)"
                for b in blockers
            )
            or "a writer that was released between the refusal and this diagnostic"
        )
        raise IdentityRootContested(
            f"Refusing to transfer {root_path} on {host!r} to {project_id}: a structure writer is "
            f"registered against an identity this transfer would invalidate, and could be "
            f"mid-write. Nothing was changed. Blocking writers: {detail}. Wait for it to finish "
            f"and re-issue. An entry that persists is an abandoned slot from a killed process -- "
            f"clear it deliberately after confirming the process is gone; an old timestamp is not "
            f"proof that a process stopped writing."
        )

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
            claimed = _norm(str(row["root"]))
        if claimed and claimed == target:
            return str(row["id"])
    return None


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
    neo4j.execute(
        """
        MATCH (p:ProjectIdentity {project_id: $project_id})
        SET p.state = 'bound',
            p.conflicting_root_path = null,
            p.conflicted_at = null,
            p.bound_at = datetime()
        """,
        {"project_id": project_id},
    )
    return _transfer(
        neo4j,
        project_id=project_id,
        root_path=keep_root_path,
        root_key=root_key_for(keep_root_path),
        host=host,
    )
