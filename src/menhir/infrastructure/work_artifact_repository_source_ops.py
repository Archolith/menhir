"""Embodiment and locator writes for :class:`WorkArtifactRepository`:
relocate/refresh/mark plus the observation-to-properties helper. Methods
moved verbatim from work_artifact_repository.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from menhir.domain.artifact_reconciliation import (
    ARTIFACT_SOURCE_SCHEMA_VERSION,
    ResolutionStatus,
    SourceObservation,
    locator_key,
)
from menhir.domain.work_artifact import ArtifactMedium


class WorkArtifactSourceOpsMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

    def relocate_source(
        self,
        artifact_uuid: str,
        medium: str,
        locator: dict[str, str],
        version: str | None = None,
    ) -> bool:
        """Update an embodiment's locator after a rename, move, or archive.

        This is an *update*, never a new embodiment: identity and embodiment are
        unchanged, only the locator moved (`model.embodiment_invariant`). Every
        relationship pointing at the artifact survives, which is the entire
        reason identity is not the path.
        """
        props = {f"locator_{k}": v for k, v in (locator or {}).items()}
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:EMBODIED_IN]->(s:ArtifactSource)
            WHERE s.medium = $medium
            SET s += $props, s.last_seen_at = $now,
                s.version = coalesce($version, s.version)
            RETURN count(s) AS updated
            """,
            {
                "uuid": artifact_uuid,
                "medium": medium,
                "props": props,
                "version": version,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def _source_write_props(self, observation: SourceObservation) -> dict[str, Any]:
        """Observation properties, with absent legs left alone rather than nulled.

        A scan that could not reach Git still knows the raw bytes. Writing None
        over a previously known blob OID would turn "not observed this time"
        into "does not exist", which is the same class of error as treating a
        missing file as a deleted artifact.

        ``resolution_reason`` is the one exception and is written back as null.
        Observing a source resolves it, and carrying "source_not_observed" on a
        source we just read leaves a record that contradicts itself.
        """
        props = {k: v for k, v in observation.as_properties().items() if v is not None}
        props["resolution_reason"] = None
        return props

    def refresh_artifact_source(
        self,
        *,
        source_uuid: str,
        observation: SourceObservation,
        expected_integrity: str | None = None,
    ) -> dict[str, Any]:
        """Update integrity and provenance for a source that has not moved."""
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource {source_uuid: $source_uuid})
            WITH a, s,
                 ($expected IS NULL OR coalesce(s.integrity, '') = $expected) AS fresh
            FOREACH (_ IN CASE WHEN fresh THEN [1] ELSE [] END | SET s += $props)
            RETURN fresh AS fresh, a.artifact_uuid AS artifact_uuid
            """,
            {
                "source_uuid": source_uuid,
                "expected": expected_integrity,
                "props": self._source_write_props(observation),
            },
        )
        if not rows:
            return {"applied": False, "reason": "source_not_found"}
        row = rows[0]
        if not row.get("fresh"):
            return {"applied": False, "reason": "stale_expected_integrity"}
        return {
            "applied": True,
            "artifact_uuid": row.get("artifact_uuid"),
            "source_uuid": source_uuid,
        }

    def relocate_artifact_source(
        self,
        *,
        source_uuid: str,
        old_locator: dict[str, str],
        new_locator: dict[str, str],
        observation: SourceObservation,
        expected_integrity: str | None = None,
    ) -> dict[str, Any]:
        """Move one source's locator in place, preserving identity and edges.

        The destination is checked inside the same statement that changes the
        key. Checking first and writing second would leave a window in which two
        relocations both saw an empty destination -- and the uniqueness
        constraint on the key is the backstop if one still slips through.
        """
        medium = str(
            new_locator.get("medium")
            or old_locator.get("medium")
            or ArtifactMedium.MARKDOWN
        )
        repository = new_locator.get("repository") or old_locator.get("repository")
        new_key = locator_key(repository, medium, new_locator.get("path"))
        old_key = locator_key(
            old_locator.get("repository"), medium, old_locator.get("path")
        )
        props = self._source_write_props(observation)
        props["locator_repository"] = repository
        props["locator_path"] = new_locator.get("path")
        props["current_locator_key"] = new_key

        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource {source_uuid: $source_uuid})
            OPTIONAL MATCH (other:ArtifactSource)
            WHERE (coalesce(other.current_locator_key,
                            coalesce(other.locator_repository, '') + '|'
                            + coalesce(other.medium, '') + '|'
                            + coalesce(other.locator_path, '')) = $new_key
                   OR (coalesce(trim(other.locator_repository), '') = ''
                       AND other.medium = $medium
                       AND other.locator_path = $new_path))
              AND coalesce(other.source_uuid, '') <> $source_uuid
            WITH a, s, count(other) AS blockers,
                 count(CASE WHEN other IS NOT NULL
                                 AND coalesce(trim(other.locator_repository), '') = ''
                            THEN 1 END) AS unscoped_blockers,
                 (coalesce(s.current_locator_key,
                           coalesce(s.locator_repository, '') + '|'
                           + coalesce(s.medium, '') + '|'
                           + coalesce(s.locator_path, '')) = $old_key
                  OR ($old_repository_unscoped
                      AND coalesce(trim(s.locator_repository), '') = ''
                      AND s.medium = $medium
                      AND s.locator_path = $old_path)) AS at_old,
                 ($expected IS NULL OR coalesce(s.integrity, '') = $expected) AS fresh
            FOREACH (_ IN CASE WHEN blockers = 0 AND at_old AND fresh THEN [1] ELSE [] END |
                SET s += $props)
            RETURN blockers AS blockers, unscoped_blockers AS unscoped_blockers,
                   at_old AS at_old, fresh AS fresh,
                   a.artifact_uuid AS artifact_uuid
            """,
            {
                "source_uuid": source_uuid,
                "new_key": new_key,
                "medium": medium,
                "new_path": new_locator.get("path"),
                "old_key": old_key,
                "old_repository_unscoped": not str(
                    old_locator.get("repository") or ""
                ).strip(),
                "old_path": old_locator.get("path"),
                "expected": expected_integrity,
                "props": props,
            },
        )
        if not rows:
            return {"applied": False, "reason": "source_not_found"}
        row = rows[0]
        if int(row.get("unscoped_blockers") or 0) > 0:
            return {"applied": False, "reason": "unscoped_source_claims_destination"}
        if int(row.get("blockers") or 0) > 0:
            return {"applied": False, "reason": "destination_already_claimed"}
        if not row.get("at_old"):
            return {"applied": False, "reason": "stale_old_locator"}
        if not row.get("fresh"):
            return {"applied": False, "reason": "stale_expected_integrity"}
        return {
            "applied": True,
            "artifact_uuid": row.get("artifact_uuid"),
            "source_uuid": source_uuid,
            "path": new_locator.get("path"),
        }

    def _source_uuid_at_locator(
        self, repository: str, medium: str, path: str
    ) -> tuple[str | None, str | None]:
        """Resolve one locator to one source UUID, or say why it could not.

        Returns ``(source_uuid, reason)``. Ambiguity is a refusal: an old path
        that identifies two sources is exactly the case where picking one does
        the most damage.
        """
        key = locator_key(repository, medium, path)
        rows = self.neo4j.execute(
            """
            MATCH (:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource)
            WHERE coalesce(s.current_locator_key,
                           coalesce(s.locator_repository, '') + '|' + coalesce(s.medium, '')
                           + '|' + coalesce(s.locator_path, '')) = $key
            RETURN s.source_uuid AS source_uuid
            """,
            {"key": key},
        )
        if not rows:
            return None, "locator_not_found"
        if len(rows) > 1:
            return None, "locator_is_ambiguous"
        source_uuid = rows[0].get("source_uuid")
        if not source_uuid:
            return None, "source_uuid_not_backfilled"
        return str(source_uuid), None

    def relocate_artifact_source_by_locator(
        self,
        *,
        repository: str,
        medium: str,
        old_path: str,
        new_path: str,
        observation: SourceObservation,
    ) -> dict[str, Any]:
        """Relocate by old locator, for callers with no source UUID in hand.

        The immediate hook detector knows two paths and nothing else, which is
        enough for an unambiguous move and deliberately not enough for anything
        else.
        """
        source_uuid, reason = self._source_uuid_at_locator(repository, medium, old_path)
        if source_uuid is None:
            return {"applied": False, "reason": reason}
        return self.relocate_artifact_source(
            source_uuid=source_uuid,
            old_locator={"repository": repository, "path": old_path, "medium": medium},
            new_locator={"repository": repository, "path": new_path, "medium": medium},
            observation=observation,
        )

    def refresh_artifact_source_by_locator(
        self, *, repository: str, medium: str, path: str, observation: SourceObservation
    ) -> dict[str, Any]:
        """Refresh integrity for an unmoved path. One source or nothing."""
        source_uuid, reason = self._source_uuid_at_locator(repository, medium, path)
        if source_uuid is None:
            return {"applied": False, "reason": reason}
        return self.refresh_artifact_source(
            source_uuid=source_uuid, observation=observation
        )

    def mark_artifact_source_unresolved(
        self, *, source_uuid: str, reason: str, observed_commit: str | None = None
    ) -> dict[str, Any]:
        """Record that a source could not be found. Never a delete.

        The locator, the artifact, and every relationship stay exactly as they
        were, so the state is reversible: if the file reappears, the next audit
        refreshes it back to resolved rather than minting a second identity for
        the same document.
        """
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource {source_uuid: $source_uuid})
            SET s.resolution_status = $unresolved,
                s.resolution_reason = $reason,
                s.observed_commit = coalesce($observed_commit, s.observed_commit),
                s.last_reconciled_at = $now,
                s.schema_version = $schema_version
            RETURN a.artifact_uuid AS artifact_uuid
            """,
            {
                "source_uuid": source_uuid,
                "unresolved": ResolutionStatus.UNRESOLVED,
                "reason": reason,
                "observed_commit": observed_commit,
                "now": datetime.now(timezone.utc).isoformat(),
                "schema_version": ARTIFACT_SOURCE_SCHEMA_VERSION,
            },
        )
        if not rows:
            return {"applied": False, "reason": "source_not_found"}
        return {"applied": True, "artifact_uuid": rows[0].get("artifact_uuid")}
