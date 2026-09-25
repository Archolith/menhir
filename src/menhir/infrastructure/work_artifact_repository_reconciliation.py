"""Source reconciliation (v2) for :class:`WorkArtifactRepository`: snapshot and
identity audit reads, the reconciliation cursor, backfills, preflight and
schema activation. Methods moved verbatim from work_artifact_repository.py."""

from __future__ import annotations

from typing import Any, Sequence
from uuid import uuid4

from menhir.domain.artifact_reconciliation import (
    ARTIFACT_SOURCE_SCHEMA_VERSION,
    ArtifactSourceSnapshot,
    INTEGRITY_ALGORITHM,
    ResolutionStatus,
    VersionKind,
    WorkArtifactIdentitySnapshot,
    locator_key,
)
from menhir.domain.work_artifact import ArtifactMedium
from menhir.infrastructure.schema import (
    ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS,
    get_artifact_reconciliation_schema_queries,
)
from menhir.infrastructure.work_artifact_repository_support import (
    _safe_namespace_filter,
)


class WorkArtifactReconciliationMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

    # ------------------------------------------------------------------
    # Source reconciliation (v2)
    #
    # Every write here is conditional on the state the audit read: the source
    # UUID, the old locator, and the integrity that was current when the plan
    # was computed. A stale action is refused rather than applied over newer
    # state -- an approved ledger must not be able to overwrite a change that
    # happened after it was approved.
    # ------------------------------------------------------------------

    def list_artifact_source_snapshots(
        self, *, repository: str | None = None, namespace: str | None = None
    ) -> list[ArtifactSourceSnapshot]:
        """Every embodiment as the graph holds it, for the read-only audit.

        ``namespace`` is opt-in: ``None``/empty does not filter. A repository is NOT a tenancy
        boundary -- `a.namespace` is -- so two silos holding artifacts for the same repository
        were previously visible to each other through this read.
        """
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource)
            WHERE ($repository IS NULL OR s.locator_repository = $repository)
              AND ($namespace IS NULL OR a.namespace = $namespace)
            RETURN a.artifact_uuid       AS artifact_uuid,
                   a.artifact_type       AS artifact_type,
                   a.title               AS title,
                   a.status              AS status,
                   s.source_uuid         AS source_uuid,
                   s.medium              AS medium,
                   s.locator_repository  AS repository,
                   s.locator_path        AS path,
                   s.integrity           AS integrity,
                   s.version             AS version,
                   s.version_kind        AS version_kind,
                   s.corpus_lane         AS corpus_lane,
                   s.resolution_status   AS resolution_status,
                   s.schema_version      AS schema_version
            ORDER BY s.locator_path, a.artifact_uuid
            """,
            {"repository": repository, "namespace": _safe_namespace_filter(namespace)},
        )
        return [
            ArtifactSourceSnapshot(
                artifact_uuid=row.get("artifact_uuid"),
                medium=row.get("medium") or ArtifactMedium.MARKDOWN,
                source_uuid=row.get("source_uuid"),
                artifact_type=row.get("artifact_type"),
                repository=row.get("repository"),
                path=row.get("path"),
                integrity=row.get("integrity"),
                version=row.get("version"),
                version_kind=row.get("version_kind"),
                lane=row.get("corpus_lane"),
                resolution_status=row.get("resolution_status")
                or ResolutionStatus.RESOLVED,
                title=row.get("title"),
                status=row.get("status"),
                schema_version=row.get("schema_version"),
            )
            for row in rows
            if row.get("artifact_uuid")
        ]

    def list_unscoped_artifact_source_snapshots(
        self, *, paths: Sequence[str], artifact_uuids: Sequence[str],
        namespace: str | None = None,
    ) -> list[ArtifactSourceSnapshot]:
        """Relevant legacy sources whose repository locator was never recorded."""
        if not paths and not artifact_uuids:
            return []
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource)
            WHERE coalesce(trim(s.locator_repository), '') = ''
              AND (s.locator_path IN $paths OR a.artifact_uuid IN $artifact_uuids)
              AND ($namespace IS NULL OR a.namespace = $namespace)
            RETURN a.artifact_uuid       AS artifact_uuid,
                   a.artifact_type       AS artifact_type,
                   a.title               AS title,
                   a.status              AS status,
                   s.source_uuid         AS source_uuid,
                   s.medium              AS medium,
                   s.locator_repository  AS repository,
                   s.locator_path        AS path,
                   s.integrity           AS integrity,
                   s.version             AS version,
                   s.version_kind        AS version_kind,
                   s.corpus_lane         AS corpus_lane,
                   s.resolution_status   AS resolution_status,
                   s.schema_version      AS schema_version
            ORDER BY s.locator_path, a.artifact_uuid
            """,
            {
                "paths": sorted(set(paths)),
                "artifact_uuids": sorted(set(artifact_uuids)),
                "namespace": _safe_namespace_filter(namespace),
            },
        )
        return [
            ArtifactSourceSnapshot(
                artifact_uuid=row["artifact_uuid"],
                medium=row.get("medium") or ArtifactMedium.MARKDOWN,
                source_uuid=row.get("source_uuid"),
                artifact_type=row.get("artifact_type"),
                repository=row.get("repository"),
                path=row.get("path"),
                integrity=row.get("integrity"),
                version=row.get("version"),
                version_kind=row.get("version_kind"),
                lane=row.get("corpus_lane"),
                resolution_status=(
                    row.get("resolution_status") or ResolutionStatus.RESOLVED
                ),
                title=row.get("title"),
                status=row.get("status"),
                schema_version=row.get("schema_version"),
            )
            for row in rows
            if row.get("artifact_uuid")
        ]

    def get_artifact_reconciliation_cursor(self, *, repository: str) -> str | None:
        """Return the last commit cleanly reconciled for one repository."""
        rows = self.neo4j.execute(
            """
            MATCH (c:ArtifactReconciliationCursor {repository: $repository})
            RETURN c.commit AS commit
            """,
            {"repository": repository},
        )
        return rows[0].get("commit") if rows else None

    def list_work_artifact_identities(
        self, *, artifact_uuids: Sequence[str], namespace: str | None = None
    ) -> list[WorkArtifactIdentitySnapshot]:
        """Identity state for declared UUIDs, including artifacts with no source.

        The UUIDs come from files in the caller's own worktree, but a UUID is not proof of
        ownership -- a declared UUID that resolves to another silo's artifact would otherwise
        return that artifact's title and status.
        """
        if not artifact_uuids:
            return []
        rows = self.neo4j.execute(
            """
            UNWIND $artifact_uuids AS artifact_uuid
            MATCH (a:WorkArtifact {artifact_uuid: artifact_uuid})
            WHERE $namespace IS NULL OR a.namespace = $namespace
            OPTIONAL MATCH (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            RETURN a.artifact_uuid AS artifact_uuid,
                   a.artifact_type AS artifact_type,
                   a.title AS title,
                   a.status AS status,
                   count(s) AS source_count
            ORDER BY a.artifact_uuid
            """,
            {
                "artifact_uuids": sorted(set(artifact_uuids)),
                "namespace": _safe_namespace_filter(namespace),
            },
        )
        return [
            WorkArtifactIdentitySnapshot(
                artifact_uuid=row["artifact_uuid"],
                artifact_type=row.get("artifact_type"),
                title=row.get("title"),
                status=row.get("status"),
                source_count=int(row.get("source_count", 0) or 0),
            )
            for row in rows
            if row.get("artifact_uuid")
        ]

    def advance_artifact_reconciliation_cursor(
        self,
        *,
        repository: str,
        expected_commit: str | None,
        observed_commit: str,
        observed_at: str,
    ) -> dict[str, Any]:
        """Compare-and-set a repository cursor after a clean reconciliation.

        The conditional CREATE/SET is one Cypher statement. A missing cursor is
        created only when the caller also observed it missing; an existing
        cursor moves only when its commit still equals the audit premise.
        """
        params = {
            "repository": repository,
            "expected_commit": expected_commit,
            "observed_commit": observed_commit,
            "observed_at": observed_at,
        }
        if expected_commit is None:
            # MERGE plus a private token makes first creation atomic under the
            # repository uniqueness constraint. A concurrent loser sees the
            # winner's token/commit and reports advanced=false.
            params["cas_token"] = str(uuid4())
            rows = self.neo4j.execute(
                """
                MERGE (c:ArtifactReconciliationCursor {repository: $repository})
                ON CREATE SET c.commit = $observed_commit,
                              c.created_at = $observed_at,
                              c.updated_at = $observed_at,
                              c._cas_token = $cas_token
                WITH c, c._cas_token = $cas_token AS advanced
                FOREACH (_ IN CASE WHEN advanced THEN [1] ELSE [] END |
                    REMOVE c._cas_token
                )
                RETURN advanced, c.commit AS current_commit
                """,
                params,
            )
        else:
            rows = self.neo4j.execute(
                """
                MATCH (c:ArtifactReconciliationCursor {
                    repository: $repository,
                    commit: $expected_commit
                })
                SET c.commit = $observed_commit,
                    c.updated_at = $observed_at
                RETURN true AS advanced, c.commit AS current_commit
                """,
                params,
            )
        if not rows:
            return {"advanced": False, "current_commit": None}
        return {
            "advanced": bool(rows[0].get("advanced")),
            "current_commit": rows[0].get("current_commit"),
        }

    def backfill_source_uuids(self) -> int:
        """Give every existing embodiment a stable handle.

        Addressability for an Owned Record, not semantic identity: a source
        still carries no meaning alone and still dies with its artifact. It has
        to exist before a uniqueness constraint can be created, and before a
        write can be conditioned on "this exact source and no other".
        """
        rows = self.neo4j.execute(
            """
            MATCH (s:ArtifactSource)
            WHERE s.source_uuid IS NULL
            RETURN elementId(s) AS eid
            """,
            {},
        )
        stamped = 0
        for row in rows:
            self.neo4j.execute(
                """
                MATCH (s:ArtifactSource) WHERE elementId(s) = $eid
                SET s.source_uuid = coalesce(s.source_uuid, $uuid)
                """,
                {"eid": row["eid"], "uuid": str(uuid4())},
            )
            stamped += 1
        return stamped

    def backfill_current_locator_keys(self) -> int:
        """Materialize the normalized locator key on every existing source.

        Also retypes the v1 ``version`` leg: a forty-character value written by
        the old migration is a commit SHA, and labelling it as such is the only
        way a later reader can tell it apart from a blob OID. The value is not
        reinterpreted, only described.
        """
        rows = self.neo4j.execute(
            """
            MATCH (s:ArtifactSource)
            WHERE coalesce(s.schema_version, 1) < $schema_version
               OR s.resolution_status IS NULL
               OR (s.version IS NOT NULL AND s.version_kind IS NULL)
               OR (s.integrity IS NOT NULL AND s.integrity_algorithm IS NULL)
               OR (
                    coalesce(s.resolution_status, $resolved) = $resolved
                    AND s.current_locator_key IS NULL
               )
            RETURN elementId(s)         AS eid,
                   s.locator_repository AS repository,
                   s.medium             AS medium,
                   s.locator_path       AS path,
                   s.version            AS version,
                   s.version_kind       AS version_kind,
                   s.resolution_status  AS resolution_status
            """,
            {
                "schema_version": ARTIFACT_SOURCE_SCHEMA_VERSION,
                "resolved": ResolutionStatus.RESOLVED,
            },
        )
        updated = 0
        for row in rows:
            resolution_status = (
                row.get("resolution_status") or ResolutionStatus.RESOLVED
            )
            key = (
                None
                if resolution_status == ResolutionStatus.UNRESOLVED
                else locator_key(
                    row.get("repository"),
                    row.get("medium") or ArtifactMedium.MARKDOWN,
                    row.get("path"),
                )
            )
            version_kind = row.get("version_kind")
            if version_kind is None and row.get("version"):
                version_kind = VersionKind.LEGACY_COMMIT_SHA
            self.neo4j.execute(
                """
                MATCH (s:ArtifactSource) WHERE elementId(s) = $eid
                SET s.current_locator_key = $key,
                    s.version_kind = coalesce(s.version_kind, $version_kind),
                    s.resolution_status = coalesce(s.resolution_status, $resolved),
                    s.integrity_algorithm = CASE
                        WHEN s.integrity IS NULL THEN s.integrity_algorithm
                        ELSE coalesce(s.integrity_algorithm, $integrity_algorithm)
                    END,
                    s.schema_version = $schema_version
                """,
                {
                    "eid": row["eid"],
                    "key": key,
                    "version_kind": version_kind,
                    "resolved": ResolutionStatus.RESOLVED,
                    "integrity_algorithm": INTEGRITY_ALGORITHM,
                    "schema_version": ARTIFACT_SOURCE_SCHEMA_VERSION,
                },
            )
            updated += 1
        return updated

    def artifact_reconciliation_preflight(self) -> dict[str, int]:
        """Count the graph-wide source-v2 migration surface and blockers."""
        rows = self.neo4j.execute(
            """
            CALL () { MATCH (s:ArtifactSource) RETURN count(s) AS sources }
            CALL () {
                MATCH (s:ArtifactSource) WHERE s.source_uuid IS NULL
                RETURN count(s) AS missing_source_uuids
            }
            CALL () {
                MATCH (s:ArtifactSource)
                WHERE s.current_locator_key IS NULL
                  AND coalesce(s.resolution_status, 'resolved') <> 'unresolved'
                RETURN count(s) AS missing_locator_keys
            }
            CALL () {
                MATCH (a:WorkArtifact) WHERE a.artifact_uuid IS NOT NULL
                WITH a.artifact_uuid AS value, count(*) AS n WHERE n > 1
                RETURN count(*) AS duplicate_artifact_uuids
            }
            CALL () {
                MATCH (s:ArtifactSource) WHERE s.source_uuid IS NOT NULL
                WITH s.source_uuid AS value, count(*) AS n WHERE n > 1
                RETURN count(*) AS duplicate_source_uuids
            }
            CALL () {
                MATCH (s:ArtifactSource)
                WHERE coalesce(s.resolution_status, 'resolved') <> 'unresolved'
                WITH trim(coalesce(s.locator_repository, '')) + '|' +
                     coalesce(s.medium, 'markdown') + '|' +
                     trim(coalesce(s.locator_path, '')) AS value,
                     count(*) AS n
                WHERE n > 1
                RETURN count(*) AS duplicate_raw_locators
            }
            CALL () {
                MATCH (s:ArtifactSource) WHERE s.current_locator_key IS NOT NULL
                WITH s.current_locator_key AS value, count(*) AS n WHERE n > 1
                RETURN count(*) AS duplicate_locator_keys
            }
            CALL () {
                MATCH (c:ArtifactReconciliationCursor)
                WITH c.repository AS value, count(*) AS n WHERE n > 1
                RETURN count(*) AS duplicate_cursor_repositories
            }
            RETURN sources, missing_source_uuids, missing_locator_keys,
                   duplicate_artifact_uuids, duplicate_source_uuids,
                   duplicate_raw_locators, duplicate_locator_keys,
                   duplicate_cursor_repositories
            """,
            {},
        )
        keys = (
            "sources",
            "missing_source_uuids",
            "missing_locator_keys",
            "duplicate_artifact_uuids",
            "duplicate_source_uuids",
            "duplicate_raw_locators",
            "duplicate_locator_keys",
            "duplicate_cursor_repositories",
        )
        row = rows[0] if rows else {}
        return {key: int(row.get(key, 0) or 0) for key in keys}

    def activate_artifact_reconciliation_schema(self) -> dict[str, Any]:
        """Install reconciliation constraints and verify their indexes ONLINE."""
        queries = get_artifact_reconciliation_schema_queries()
        for query in queries:
            self.neo4j.execute(query, {})
        required = list(ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS)
        rows = self.neo4j.execute(
            """
            SHOW INDEXES YIELD name, state
            WHERE name IN $names AND state = 'ONLINE'
            RETURN collect(name) AS names
            """,
            {"names": required},
        )
        online = sorted(str(name) for name in (rows[0].get("names", []) if rows else []))
        missing = sorted(set(required) - set(online))
        return {
            "queries_executed": len(queries),
            "constraints_online": online,
            "constraints_missing": missing,
            "ready": not missing,
        }
