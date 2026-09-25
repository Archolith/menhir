"""Inbound semantic links and lifecycle transitions for the todo repository.

Split out of ``todo_repository.py`` (file-size refactor). ``TodoRepository``
composes this mixin: ``link_memory_to_todo`` and ``todo_inbound_links`` (the
Phase B slice-1 relation surface) plus the atomic ``resolve_todo`` /
``reopen_todo`` / ``_lifecycle_transition`` transactions. Methods run against
the facade's ``self.neo4j``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from menhir.domain.todo_location import DEFAULT_TODO_NAMESPACE
from menhir.infrastructure.todo_repository_constants import (
    _ALL_TODO_INBOUND_EDGES,
    _TODO_LIFECYCLE_RELATIONS,
    _TODO_LINK_RELATIONS,
    _TODO_SUPERSESSION_EDGE,
)


class TodoLinksMixin:
    """Memory-to-todo links and the atomic status transitions built on them."""

    def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        """Point a durable semantic entity at a todo.

        Direction is always inward: a memory references the todo. The todo stays
        an operational object -- knowledge lives in memories and semantic
        objects, never accumulating inside the todo itself.

        The single exception is SUPERSEDED_BY (see ``supersede_todo``), which is
        todo-to-todo because it states an identity fact rather than a knowledge
        claim. It is not reachable from here: this method only ever writes the
        relations in ``_TODO_LINK_RELATIONS``.

        Eligibility is deliberately narrow. Only PERSISTENT, non-structural
        entities may link. A file does not "address" a todo, and admitting
        structural nodes would recreate the CONCERNS noise problem in a typed
        costume.

        Returns a dict with ``linked`` and, when refused, a ``reason``. Refusing
        is not an error: callers get a reason rather than an exception, because
        a rejected link is a data-quality signal, not a crash.
        """
        edge_type = _TODO_LINK_RELATIONS.get(relation)
        if edge_type is None:
            return {"linked": False, "reason": "unsupported_relation", "relation": relation}

        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $todo_uuid}})
            MATCH (m:Entity {{uuid: $memory_uuid}})
            WHERE m.scope = 'PERSISTENT'
              AND m.structure_role IS NULL
              // Visibility mirrors the read rule: a memory may link to a todo in
              // its own silo or in the shared default bucket, never across silos.
              AND t.namespace IN [coalesce(m.namespace, $default_ns), $default_ns]
            MERGE (m)-[r:{edge_type}]->(t)
            RETURN count(r) AS linked
            """,
            {
                "todo_uuid": todo_uuid,
                "memory_uuid": memory_uuid,
                "default_ns": DEFAULT_TODO_NAMESPACE,
            },
        )
        if rows and int(rows[0].get("linked", 0)) > 0:
            return {"linked": True, "relation": relation, "edge_type": edge_type}
        return {"linked": False, "reason": "ineligible_or_not_found", "relation": relation}

    def todo_inbound_links(self, todo_uuid: str) -> list[dict[str, Any]]:
        """Semantic entities referencing this todo, newest first."""
        return self.neo4j.execute(
            """
            MATCH (m:Entity)-[r]->(t:Todo {uuid: $uuid})
            WHERE type(r) IN $edge_types
            RETURN type(r)  AS relation,
                   m.uuid   AS memory_uuid,
                   m.name   AS memory_name,
                   m.created_at AS created_at
            ORDER BY m.created_at DESC
            """,
            {"uuid": todo_uuid, "edge_types": _ALL_TODO_INBOUND_EDGES},
        )

    def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        """Close a todo and record the memory that resolved it, atomically.

        Deliberately one Cypher statement. ``Neo4jRepository.execute`` exposes no
        transaction scope, and Neo4j wraps a single statement in an implicit
        transaction, so the edge and the status move together or neither does.
        Splitting this into two calls would allow a RESOLVES_TODO edge pointing
        at a still-open todo, or a closed todo with no evidence of why.

        This is the only path that may create RESOLVES_TODO -- ``link_memory_to_todo``
        refuses the relation precisely so an edge can never imply a status change
        it did not make.

        Eligibility and namespace rules match slice 1. The todo must be open;
        resolving an already-closed todo is refused rather than silently
        re-closing it, so the recorded resolution stays the one that applied.
        """
        return self._lifecycle_transition(
            todo_uuid,
            memory_uuid,
            edge_type=_TODO_LIFECYCLE_RELATIONS["resolves"],
            from_status="open",
            to_status="closed",
            reminder_status="completed",
            set_closed_at=True,
        )

    def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        """Reopen a closed todo and record the memory that reopened it, atomically.

        Same single-statement guarantee as ``resolve_todo``. Clears ``closed_at``
        and returns any linked reminder to open, mirroring what closing did.

        Refuses a SUPERSEDED todo. Reopening one produced an open node still carrying
        an outgoing SUPERSEDED_BY -- the state ``supersede_todo`` exists to prevent --
        and made cycles reachable via ``supersede(A, B)`` / ``reopen(A)`` /
        ``supersede(B, A)``. Refusing is the conservative half of the fix: it destroys
        nothing, where deleting the edge on reopen would silently discard the lineage.
        A superseded todo that genuinely needs to come back is reopened by superseding
        or reopening its successor, not by resurrecting the predecessor underneath it.
        """
        return self._lifecycle_transition(
            todo_uuid,
            memory_uuid,
            edge_type=_TODO_LIFECYCLE_RELATIONS["reopens"],
            from_status="closed",
            to_status="open",
            reminder_status="open",
            set_closed_at=False,
            require_no_successor=True,
        )

    def _lifecycle_transition(
        self,
        todo_uuid: str,
        memory_uuid: str,
        *,
        edge_type: str,
        from_status: str,
        to_status: str,
        reminder_status: str,
        set_closed_at: bool,
        require_no_successor: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        closed_at = "$now" if set_closed_at else "null"
        # A fixed fragment built from a module constant, never from caller input --
        # the same injection rule the relation whitelists above exist to enforce.
        successor_guard = (
            f"\n              AND NOT (t)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)"
            if require_no_successor
            else ""
        )
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $todo_uuid}})
            WHERE t.status = $from_status{successor_guard}
            MATCH (m:Entity {{uuid: $memory_uuid}})
            WHERE m.scope = 'PERSISTENT'
              AND m.structure_role IS NULL
              AND t.namespace IN [coalesce(m.namespace, $default_ns), $default_ns]
            MERGE (m)-[:{edge_type}]->(t)
            SET t.status = $to_status, t.closed_at = {closed_at}
            WITH t
            OPTIONAL MATCH (t)-[:HAS_REMINDER]->(r:Entity {{type: 'TEMPORAL'}})
            SET r.status = $reminder_status, r.last_accessed = $now
            RETURN count(t) AS applied
            """,
            {
                "todo_uuid": todo_uuid,
                "memory_uuid": memory_uuid,
                "from_status": from_status,
                "to_status": to_status,
                "reminder_status": reminder_status,
                "default_ns": DEFAULT_TODO_NAMESPACE,
                "now": now,
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {"applied": True, "status": to_status, "edge_type": edge_type}
        return {
            "applied": False,
            "reason": "todo_not_in_expected_status_or_memory_ineligible",
            "expected_status": from_status,
        }
