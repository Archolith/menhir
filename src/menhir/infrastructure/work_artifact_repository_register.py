"""Reconciliation registration for :class:`WorkArtifactRepository`:
idempotent artifact registration and first-source attachment. Methods
moved verbatim from work_artifact_repository.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.artifact_reconciliation import SourceObservation, locator_key
from menhir.domain.work_artifact import ARTIFACT_MEDIA, ArtifactSourceSpec
from menhir.infrastructure.work_artifact_repository_support import (
    _Neo4jConstraintError,
)


class WorkArtifactRegistrationMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

    def register_work_artifact(
        self,
        *,
        artifact_type: str,
        title: str,
        repository: str,
        path: str,
        medium: str,
        observation: SourceObservation,
        namespace: str | None = None,
        status: str | None = None,
        status_raw: str | None = None,
        status_unresolved_reason: str | None = None,
        artifact_uuid: str | None = None,
        structure_project: str | None = None,
    ) -> dict[str, Any]:
        """Create one artifact for one newly discovered source.

        Idempotent on both keys that could already identify it: a declared UUID
        already in the graph, and a locator already claimed. Re-running a
        reconcile must not mint a second identity for a document that has one.

        ``status_unresolved_reason`` is carried through to ``create_artifact``
        rather than dropped here. This is the only path reconciliation uses to
        register a document, so a reason lost at this hop is a reason that never
        reaches the graph, and every read surface is gated on it being stored.
        """
        if artifact_uuid:
            existing = self.neo4j.execute(
                "MATCH (a:WorkArtifact {artifact_uuid: $uuid}) RETURN a.artifact_uuid AS uuid",
                {"uuid": artifact_uuid},
            )
            if existing:
                return {
                    "applied": False,
                    "reason": "declared_uuid_already_registered",
                    "artifact_uuid": artifact_uuid,
                }

        key = locator_key(repository, medium, path)
        claimed = self.neo4j.execute(
            """
            MATCH (s:ArtifactSource)
            WHERE coalesce(s.current_locator_key,
                           coalesce(s.locator_repository, '') + '|'
                           + coalesce(s.medium, '') + '|'
                           + coalesce(s.locator_path, '')) = $key
               OR (coalesce(trim(s.locator_repository), '') = ''
                   AND s.medium = $medium AND s.locator_path = $path)
            RETURN count(s) AS n,
                   count(CASE WHEN coalesce(trim(s.locator_repository), '') = ''
                              THEN 1 END) AS unscoped
            """,
            {"key": key, "medium": medium, "path": path},
        )
        if claimed and int(claimed[0].get("n", 0) or 0) > 0:
            if int(claimed[0].get("unscoped", 0) or 0) > 0:
                return {
                    "applied": False,
                    "reason": "unscoped_source_claims_destination",
                }
            return {"applied": False, "reason": "destination_already_claimed"}

        created = self.create_artifact(
            artifact_type=artifact_type,
            title=title,
            source=ArtifactSourceSpec(
                medium=medium,
                locator={"repository": repository, "path": path},
                version=observation.version,
                integrity=observation.integrity,
            ),
            namespace=namespace,
            status=status,
            status_raw=status_raw,
            status_unresolved_reason=status_unresolved_reason,
            structure_project=structure_project,
            artifact_uuid=artifact_uuid,
        )
        props = self._source_write_props(observation)
        props["current_locator_key"] = key
        props["source_uuid"] = str(uuid4())
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:EMBODIED_IN]->(s:ArtifactSource)
            WHERE s.locator_path = $path
            SET s += $props
            """,
            {"uuid": created["artifact_uuid"], "path": path, "props": props},
        )
        created["applied"] = True
        created["source_uuid"] = props["source_uuid"]
        return created

    def attach_artifact_source(
        self,
        *,
        artifact_uuid: str,
        expected_artifact_type: str,
        repository: str,
        path: str,
        medium: str,
        observation: SourceObservation,
    ) -> dict[str, Any]:
        """Attach the first embodiment to an existing semantic artifact.

        A temporary property write locks the artifact before source state is
        read, so concurrent attachment attempts serialize. The property is
        removed in the same transaction and never becomes committed state.
        """
        if medium not in ARTIFACT_MEDIA:
            raise ValueError(f"unknown medium: {medium!r}")

        source_uuid = str(uuid4())
        observed_at = observation.observed_at or datetime.now(timezone.utc).isoformat()
        key = locator_key(repository, medium, path)
        props = self._source_write_props(observation)
        props.update(
            {
                "source_uuid": source_uuid,
                "medium": medium,
                "locator_repository": repository,
                "locator_path": path,
                "current_locator_key": key,
                "first_seen_at": observed_at,
            }
        )
        try:
            rows = self.neo4j.execute(
                """
                MATCH (a:WorkArtifact {artifact_uuid: $artifact_uuid})
                SET a._reconcile_attach_lock = $lock_token
                WITH a
                OPTIONAL MATCH (a)-[:EMBODIED_IN]->(existing:ArtifactSource)
                WITH a, count(existing) AS source_count
                OPTIONAL MATCH (occupied:ArtifactSource)
                WHERE coalesce(occupied.current_locator_key,
                               coalesce(occupied.locator_repository, '') + '|'
                               + coalesce(occupied.medium, '') + '|'
                               + coalesce(occupied.locator_path, '')) = $key
                   OR (coalesce(trim(occupied.locator_repository), '') = ''
                       AND occupied.medium = $medium
                       AND occupied.locator_path = $path)
                WITH a, source_count, count(occupied) AS blockers,
                     count(CASE WHEN occupied IS NOT NULL
                                     AND coalesce(trim(occupied.locator_repository), '') = ''
                                THEN 1 END) AS unscoped_blockers,
                     a.artifact_type = $expected_artifact_type AS type_matches
                FOREACH (_ IN CASE WHEN source_count = 0 AND blockers = 0 AND type_matches
                                   THEN [1] ELSE [] END |
                    CREATE (a)-[:EMBODIED_IN]->(s:ArtifactSource)
                    SET s += $props
                )
                REMOVE a._reconcile_attach_lock
                WITH a, source_count, blockers, unscoped_blockers, type_matches
                OPTIONAL MATCH (a)-[:EMBODIED_IN]->(
                    created:ArtifactSource {source_uuid: $source_uuid}
                )
                RETURN a.artifact_type AS artifact_type,
                       source_count,
                       blockers,
                       unscoped_blockers,
                       type_matches,
                       count(created) = 1 AS attached
                """,
                {
                    "artifact_uuid": artifact_uuid,
                    "expected_artifact_type": expected_artifact_type,
                    "key": key,
                    "medium": medium,
                    "path": path,
                    "source_uuid": source_uuid,
                    "lock_token": str(uuid4()),
                    "props": props,
                },
            )
        except _Neo4jConstraintError:
            # The locator uniqueness constraint is the final race fence when a
            # different artifact claims the destination concurrently.
            return {"applied": False, "reason": "destination_already_claimed"}

        if not rows:
            return {"applied": False, "reason": "artifact_not_found"}
        row = rows[0]
        if not row.get("type_matches"):
            return {
                "applied": False,
                "reason": "artifact_type_changed",
                "artifact_type": row.get("artifact_type"),
            }
        if int(row.get("source_count", 0) or 0) > 0:
            return {"applied": False, "reason": "artifact_already_has_source"}
        if int(row.get("unscoped_blockers", 0) or 0) > 0:
            return {"applied": False, "reason": "unscoped_source_claims_destination"}
        if int(row.get("blockers", 0) or 0) > 0:
            return {"applied": False, "reason": "destination_already_claimed"}
        if not row.get("attached"):
            return {"applied": False, "reason": "source_attach_not_confirmed"}
        return {
            "applied": True,
            "artifact_uuid": artifact_uuid,
            "source_uuid": source_uuid,
        }
