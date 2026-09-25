"""Artifact reads for :class:`WorkArtifactRepository`: fetch, list and
hard-delete. Methods moved verbatim from work_artifact_repository.py."""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import normalize_namespace
from menhir.domain.work_artifact import DEFAULT_ARTIFACT_NAMESPACE


class WorkArtifactReadsMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

    def delete_artifact(self, artifact_uuid: str) -> bool:
        """Hard-delete an artifact and every subordinate it owns."""
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            OPTIONAL MATCH (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            OPTIONAL MATCH (a)-[:HAS_LOCATION]->(l:ArtifactLocation)
            OPTIONAL MATCH (a)-[:HAS_OPEN_QUESTION]->(q:OpenQuestion)
            OPTIONAL MATCH (a)-[:DECLARES]->(d:ArtifactDeclaration)
            WITH a, s, l, q, d, count(a) AS found
            DETACH DELETE a, s, l, q, d
            RETURN found
            """,
            {"uuid": artifact_uuid},
        )
        return bool(rows and int(rows[0].get("found", 0)) > 0)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch one artifact with its embodiments and locations."""
        namespaces = (
            [normalize_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE]
            if namespace
            else None
        )
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            WHERE $namespaces IS NULL OR a.namespace IN $namespaces
            OPTIONAL MATCH (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            WITH a, collect(s {.*}) AS embodiments
            OPTIONAL MATCH (a)-[:HAS_LOCATION]->(l:ArtifactLocation)
            WITH a, embodiments, l ORDER BY l.ordinal ASC
            RETURN
                a.artifact_uuid     AS artifact_uuid,
                a.artifact_type     AS artifact_type,
                a.title             AS title,
                a.status            AS status,
                a.namespace         AS namespace,
                a.created_at        AS created_at,
                a.updated_at        AS updated_at,
                a.status_changed_at AS status_changed_at,
                a.status_raw        AS status_raw,
                a.status_unresolved_reason AS status_unresolved_reason,
                a.shape_status      AS shape_status,
                a.shape_violations  AS shape_violations,
                a.shape_checked_at  AS shape_checked_at,
                embodiments         AS embodiments,
                collect(l {.project, .path, .kind, .line_start, .line_end,
                           .symbol, .ordinal, .resolution_status,
                           .unresolved_reason}) AS locations
            """,
            {"uuid": artifact_uuid, "namespaces": namespaces},
        )
        return rows[0] if rows else None

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List artifacts, newest first, with optional type/status/silo filters.

        ``namespace`` is opt-in and follows the :Todo rule: omitted spans every
        silo, supplied narrows to that silo plus the shared default bucket, so a
        pinned client sees shared artifacts rather than nothing.
        """
        namespaces = (
            [normalize_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE]
            if namespace
            else None
        )
        return self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)
            WHERE ($artifact_type IS NULL OR a.artifact_type = $artifact_type)
              AND ($status IS NULL OR a.status = $status)
              AND ($namespaces IS NULL OR a.namespace IN $namespaces)
            RETURN
                a.artifact_uuid AS artifact_uuid,
                a.artifact_type AS artifact_type,
                a.title         AS title,
                a.status        AS status,
                a.namespace     AS namespace,
                a.updated_at    AS updated_at,
                a.status_raw    AS status_raw,
                a.status_unresolved_reason AS status_unresolved_reason
            ORDER BY a.updated_at DESC
            LIMIT $limit
            """,
            {
                "artifact_type": artifact_type,
                "status": status,
                "namespaces": namespaces,
                "limit": max(1, min(limit, 200)),
            },
        )
