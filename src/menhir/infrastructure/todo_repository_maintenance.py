"""Close and delete operations for the todo repository.

Split out of ``todo_repository.py`` (file-size refactor). ``TodoRepository``
composes this mixin: the plain status close and the hard delete with their
reminder/location cleanup. Methods run against the facade's ``self.neo4j``.
"""

from __future__ import annotations

from datetime import datetime, timezone


class TodoMaintenanceMixin:
    """Direct status close and hard delete, with owned-node cleanup."""

    def close_todo(self, uuid: str) -> bool:
        """Mark a todo as closed. Returns True if it was open and got closed.

        Also completes any linked TEMPORAL reminder node via [:HAS_REMINDER].
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {uuid: $uuid})
            WHERE n.status = 'open'
            SET n.status = 'closed', n.closed_at = $now
            WITH n
            OPTIONAL MATCH (n)-[:HAS_REMINDER]->(r:Entity {type: 'TEMPORAL'})
            WHERE r.status = 'open'
            SET r.status = 'completed', r.last_accessed = $now
            RETURN count(n) AS updated
            """,
            {"uuid": uuid, "now": now},
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def delete_todo(self, uuid: str) -> bool:
        """Hard-delete a todo node regardless of status.

        Also detach-deletes any linked TEMPORAL reminder node via [:HAS_REMINDER]
        and every owned :TodoLocation. Locations are value objects owned by the
        todo, so they must not outlive it.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {uuid: $uuid})
            OPTIONAL MATCH (n)-[:HAS_REMINDER]->(r:Entity {type: 'TEMPORAL'})
            OPTIONAL MATCH (n)-[:HAS_LOCATION]->(l:TodoLocation)
            WITH n, r, l, count(n) AS found
            DETACH DELETE n, r, l
            RETURN found
            """,
            {"uuid": uuid},
        )
        return bool(rows and int(rows[0].get("found", 0)) > 0)
