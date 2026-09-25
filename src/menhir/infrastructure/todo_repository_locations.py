"""Location write support for the todo repository.

Split out of ``todo_repository.py`` (file-size refactor). ``TodoRepository``
composes this mixin so ``_known_projects`` and ``_write_locations`` stay
available on the facade unchanged. Methods run against the facade's
``self.neo4j``.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.todo_location import parse_code_ref
from menhir.infrastructure.paths import default_workspace_marker


class TodoLocationsMixin:
    """Bare project-name resolution and owned :TodoLocation node writes."""

    neo4j: Any  # Neo4jRepository

    def _known_projects(self) -> frozenset[str]:
        """Project names the structure graph knows, for bare `<project>/<path>` refs.

        Cached per repository instance: the set changes only when a project is
        scanned, and normalization must not pay a graph round trip per segment.
        """
        if self._known_projects_cache is None:
            rows = self.neo4j.execute(
                """
                MATCH (e:Entity)
                WHERE e.structure_project IS NOT NULL
                RETURN DISTINCT e.structure_project AS p
                """,
                {},
            )
            self._known_projects_cache = frozenset(
                str(r["p"]) for r in rows if r.get("p")
            )
        return self._known_projects_cache

    def _write_locations(
        self,
        todo_uuid: str,
        code_ref: str | None,
        structure_project: str | None,
    ) -> list[dict[str, Any]]:
        """Normalize ``code_ref`` into owned :TodoLocation nodes.

        :TodoLocation carries its own label and never :Entity or :Episodic --
        the same containment the :TurnEvidence node uses -- so a location can
        never surface in semantic recall. It holds no namespace: visibility is
        inherited through the owning :Todo, so there is no copy to drift.
        """
        if not code_ref:
            return []

        locations = parse_code_ref(
            code_ref,
            structure_project=structure_project,
            known_projects=self._known_projects(),
            workspace_marker=default_workspace_marker(),
        )
        if not locations:
            return []

        rows = [loc.as_properties() for loc in locations]
        self.neo4j.execute(
            """
            MATCH (t:Todo {uuid: $uuid})
            UNWIND $rows AS row
            CREATE (t)-[:HAS_LOCATION]->(l:TodoLocation)
            SET l += row
            """,
            {"uuid": todo_uuid, "rows": rows},
        )
        return rows
