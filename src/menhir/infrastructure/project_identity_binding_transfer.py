"""The single-statement identity transfer behind :mod:`menhir.infrastructure.project_identity_binding`.

``_transfer`` -- the retire-rivals-and-claim statement -- moved verbatim from the facade module by
the file-size refactor. The facade re-imports it, so callers are unaffected. The facade helpers it
needs are fetched at call time so the module never imports the facade at import time.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.project_identity_binding_model import (
    BindingState,
    IdentityBindingConflict,
    IdentityRootContested,
)


def _transfer(
    neo4j: Any,
    *,
    project_id: str,
    root_path: str,
    root_key: str,
    host: str,
    resolve_conflict: bool = False,
    publication_pending: bool = False,
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
    from menhir.infrastructure.project_identity_binding import (
        _active_rivals,
        _is_constraint_violation,
    )

    conflicted = neo4j.execute(
        """
        MATCH (p:ProjectIdentity {project_id: $project_id})
        WHERE coalesce(p.state, 'bound') = 'conflicted'
        RETURN p.project_id AS id
        """,
        {"project_id": project_id},
    )
    if conflicted and not resolve_conflict:
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
                 [x IN locked WHERE x.project_id IN $rival_ids] AS rivals,
                 any(x IN locked WHERE x.project_id = $project_id
                     AND coalesce(x.state, 'bound') = 'conflicted') AS target_conflicted
            WHERE held = 0 AND ($resolve_conflict OR NOT target_conflicted)
            FOREACH (o IN rivals |
                SET o.previous_root_key = o.root_key,
                    o.root_key = null,
                    o.state = 'superseded',
                    o.superseded_by = $project_id,
                    o.superseded_at = datetime(),
                    o.publication_pending = null,
                    o.publication_pending_host = null,
                    o.publication_pending_root_key = null,
                    o.publication_pending_generation = null,
                    o.publication_pending_at = null)
            WITH size(rivals) AS retired
            MERGE (p:ProjectIdentity {project_id: $project_id})
              ON CREATE SET p.bound_at = datetime()
            WITH retired, p, coalesce(p.claim_generation, 0) + 1 AS next_generation
            SET p.previous_root_path = p.canonical_root_path,
                p.canonical_root_path = $root_path,
                p.state = 'bound',
                p.bound_host = $host,
                p.root_key = $root_key,
                p.rebound_at = datetime(),
                p.claim_generation = next_generation,
                p.conflicting_root_path = CASE WHEN $resolve_conflict
                                              THEN null ELSE p.conflicting_root_path END,
                p.conflicting_bound_host = CASE WHEN $resolve_conflict
                                               THEN null ELSE p.conflicting_bound_host END,
                p.conflicted_at = CASE WHEN $resolve_conflict THEN null ELSE p.conflicted_at END,
                p.publication_pending = CASE WHEN $publication_pending THEN true ELSE null END,
                p.publication_pending_host = CASE WHEN $publication_pending THEN $host ELSE null END,
                p.publication_pending_root_key = CASE WHEN $publication_pending
                                                      THEN $root_key ELSE null END,
                p.publication_pending_generation = CASE WHEN $publication_pending
                                                        THEN next_generation ELSE null END,
                p.publication_pending_at = CASE WHEN $publication_pending
                                                THEN datetime() ELSE null END
            RETURN retired, p.claim_generation AS claim_generation
            """,
            {
                "project_id": project_id,
                "root_path": root_path,
                "host": host,
                "root_key": root_key,
                "rival_ids": rival_ids,
                "lock_ids": lock_ids,
                "resolve_conflict": resolve_conflict,
                "publication_pending": publication_pending,
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
        conflicted_now = neo4j.execute(
            """
            MATCH (p:ProjectIdentity {project_id: $project_id})
            WHERE coalesce(p.state, 'bound') = 'conflicted'
            RETURN p.project_id AS id
            """,
            {"project_id": project_id},
        )
        if conflicted_now and not resolve_conflict:
            raise IdentityBindingConflict(
                f"Project id {project_id} is marked CONFLICTED and cannot be transferred until "
                f"an operator resolves it by naming the root to keep."
            )
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
