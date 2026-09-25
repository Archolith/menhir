"""Supersession (todo -> todo) for the todo repository.

Split out of ``todo_repository.py`` (file-size refactor). ``TodoRepository``
composes this mixin: the atomic ``supersede_todo`` write, its refusal
diagnostics, and the ``todo_supersession`` lineage read. Methods run against
the facade's ``self.neo4j``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from menhir.domain.todo_location import DEFAULT_TODO_NAMESPACE
from menhir.infrastructure.todo_repository_constants import _TODO_SUPERSESSION_EDGE


class TodoSupersessionMixin:
    """Refile lineage: the one todo-to-todo edge, written and read."""

    def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        """Close ``old`` and record that ``new`` replaced it, atomically.

        Menhir has no update path: editing a todo means closing it and adding a
        replacement, which until now dropped the link between the two. This is that
        link, written as the same kind of single statement ``resolve_todo`` is -- a
        SUPERSEDED_BY edge pointing at a still-open todo, or a closed todo with no
        record of what replaced it, are both states the graph must never hold.

        Deliberately NOT built on ``_lifecycle_transition``: that helper hardcodes the
        memory side of the edge (``:Entity`` with ``scope = 'PERSISTENT'`` and no
        ``structure_role``), and this edge has a ``:Todo`` on both ends. The two
        eligibility rules have nothing in common, so they stay separate rather than
        growing a parameter that means "which of two unrelated predicates to apply".

        Refusals, all returned as a reason rather than raised:

        - ``old`` must be open.
        - ``new`` must exist, be open, and not be ``old`` itself.
        - ``old`` must not already have a successor, and ``new`` must not have one
          either. The guard on ``new`` is what blocks cycles, and it is NOT redundant
          with the status guards. An earlier version of this docstring argued that
          closing ``old`` made cycles impossible; that was wrong, because ``reopen_todo``
          returns a superseded todo to open and does not remove its edge. The concrete
          sequence was: ``supersede(A, B)`` -> ``reopen(A)`` -> ``supersede(B, A)``,
          leaving A->B and B->A. Reopen now refuses a superseded todo, and this guard
          closes the same hole independently: every ancestor in a chain carries an
          outgoing SUPERSEDED_BY, so a ``new`` with no successor cannot be an ancestor
          of ``old``. Two guards for one invariant is deliberate -- this one is local to
          the write and holds even if the reopen rule is later relaxed.
        - Namespace, mirroring the slice-1 rule exactly: ``new`` must be in ``old``'s
          silo or in the shared default bucket. The asymmetry is inherited on purpose --
          a default-bucket todo cannot be superseded by a siloed one, the same way a
          memory cannot link across silos.

          **This is the inverse of `supersede_artifact`.** That path uses
          ``namespace_compatibility_cypher(owner=new, subordinate=old)``, i.e.
          ``old.namespace IN [new.namespace, default]`` -- owner and subordinate are
          swapped for the same verb, so the two tools refuse opposite cases. Kept rather
          than aligned because todos inherit the slice-1 link rule, where the memory doing
          the pointing is the owner, and changing it would silently alter which existing
          links are legal. Recorded here because an agent that learned one rule will get
          the opposite answer from the other, and that is a documentation problem, not a
          bug in either.
        """
        if old_uuid == new_uuid:
            return {"applied": False, "reason": "cannot_supersede_itself"}

        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            f"""
            MATCH (t_old:Todo {{uuid: $old_uuid}})
            WHERE t_old.status = 'open'
              AND NOT (t_old)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)
            MATCH (t_new:Todo {{uuid: $new_uuid}})
            WHERE t_new.status = 'open'
              AND NOT (t_new)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)
              AND coalesce(t_new.namespace, $default_ns)
                  IN [coalesce(t_old.namespace, $default_ns), $default_ns]
            MERGE (t_old)-[:{_TODO_SUPERSESSION_EDGE}]->(t_new)
            SET t_old.status = 'closed', t_old.closed_at = $now
            WITH t_old
            OPTIONAL MATCH (t_old)-[:HAS_REMINDER]->(r:Entity {{type: 'TEMPORAL'}})
            SET r.status = 'completed', r.last_accessed = $now
            RETURN count(t_old) AS applied
            """,
            {
                "old_uuid": old_uuid,
                "new_uuid": new_uuid,
                "default_ns": DEFAULT_TODO_NAMESPACE,
                "now": now,
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {
                "applied": True,
                "status": "closed",
                "edge_type": _TODO_SUPERSESSION_EDGE,
                "superseded_by": new_uuid,
            }
        return {"applied": False, "reason": self._supersede_refusal_reason(old_uuid)}

    def _supersede_refusal_reason(self, old_uuid: str) -> str:
        """Name which precondition failed, for a refusal the caller can act on.

        A second read, deliberately: the write is one statement for atomicity, so it
        cannot also report WHY it matched nothing. Diagnosing after the fact costs a
        round trip only on the refusal path. It is advisory -- state may have moved
        between the two reads -- so it never gates anything, it only explains.

        Worth the round trip because `old_already_superseded` is now reachable and a
        caller told only "refused" cannot tell it apart from a typo'd uuid.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $uuid}})
            RETURN t.status AS status,
                   exists((t)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)) AS has_successor
            LIMIT 1
            """,
            {"uuid": old_uuid},
        )
        if not rows:
            return "old_todo_not_found"
        if rows[0].get("has_successor"):
            return "old_todo_already_superseded"
        if rows[0].get("status") != "open":
            return "old_todo_not_open"
        # Old is fine, so the failure is on the new side: absent, closed, already a
        # predecessor itself, or in a namespace the old todo cannot reach.
        return "new_todo_ineligible"

    def todo_supersession(
        self, todo_uuid: str, namespaces: list[str] | None = None
    ) -> dict[str, Any]:
        """Both directions of this todo's refile lineage.

        The reader half of ``supersede_todo``. CF-143 removed two edge types that were
        written on every create and read by nothing; this exists so SUPERSEDED_BY never
        becomes the third. It is surfaced through ``get_todo``, which renders it.

        Both sides are collected, and neither is a grouping key. An earlier version
        returned ``succ.uuid`` bare with ``LIMIT 1``: a todo with two successors -- which
        concurrent supersessions can still produce, since the write's successor guard is
        a read in the same statement rather than a constraint -- then yielded two rows and
        the unordered LIMIT dropped one at random. Collecting reports the anomaly instead
        of hiding it, so ``superseded_by`` is a LIST. Callers treat >1 as a repair signal,
        not as a normal outcome.

        ``namespaces`` scopes both sides to the caller's silo. Without it a shared
        default-bucket todo hands every reader the uuids of siloed todos on either end
        of its lineage.

        That filter is applied in PYTHON, not in the query, and deliberately. The rule
        todos use -- "the caller's silo OR the shared default bucket" -- is not the rule
        ``tenant_scope_cypher`` implements, which is "the caller's silo", with
        ``namespace_spellings`` covering only the ``''``/``'default'`` spelling
        equivalence. Using the shared builder here would hide default-bucket lineage
        from a siloed caller who can nonetheless read the default-bucket todo itself.
        Filtering in Python against the SAME ``namespaces`` list ``get_todo`` matched on
        keeps the two rules identical by construction, and adds no fourth hand-written
        tenancy predicate to this file (CF-127's ratchet).
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $uuid}})
            OPTIONAL MATCH (t)-[:{_TODO_SUPERSESSION_EDGE}]->(succ:Todo)
            WITH t, collect(DISTINCT {{uuid: succ.uuid, ns: succ.namespace}}) AS successors
            OPTIONAL MATCH (pred:Todo)-[:{_TODO_SUPERSESSION_EDGE}]->(t)
            RETURN successors AS superseded_by,
                   collect(DISTINCT {{uuid: pred.uuid, ns: pred.namespace}}) AS supersedes
            """,
            {"uuid": todo_uuid},
        )
        if not rows:
            return {"superseded_by": [], "supersedes": []}

        def _visible(entries: Any) -> list[str]:
            out = []
            for entry in entries or []:
                uuid = (entry or {}).get("uuid")
                if not uuid:
                    continue  # OPTIONAL MATCH miss collects a null-valued map
                if namespaces is not None and (entry.get("ns") or DEFAULT_TODO_NAMESPACE) not in namespaces:
                    continue
                out.append(uuid)
            return out

        return {
            "superseded_by": _visible(rows[0].get("superseded_by")),
            "supersedes": _visible(rows[0].get("supersedes")),
        }
