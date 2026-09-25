"""Frontmatter declarations and their resolution for
:class:`WorkArtifactRepository`. Methods moved verbatim from
work_artifact_repository.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.work_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    DEFAULT_ARTIFACT_NAMESPACE,
    DeclarationKind,
    DeclarationStatus,
    namespace_compatibility_cypher,
    normalize_declarations,
)


class WorkArtifactDeclarationsMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

    # ------------------------------------------------------------------
    # Declarations
    # ------------------------------------------------------------------

    def declare_from_frontmatter(
        self, artifact_uuid: str, declarations: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Record structured frontmatter declarations and try to honor them.

        The declaration is the durable record; the edge is a *consequence* of a
        successful resolution. Frontmatter names targets by human-readable name
        while identity is a uuid, so resolution can fail -- a renamed target, a
        typo, a plan not yet ingested. Every declaration is stored verbatim
        first and resolved second, so a failure leaves evidence instead of
        silently deleting a relationship the author asked for.

        Declarations only ever *add*. A target dropped from frontmatter does not
        retract an existing edge: removal is an explicit act, and a document
        losing a line should not quietly rewrite the graph. (This settles the
        design's open question 2 in the only direction that is safe to
        automate.)
        """
        references, ignored = normalize_declarations(declarations)
        if not references:
            return {"declared": 0, "ignored": ignored, "results": []}

        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "declaration_uuid": str(uuid4()),
                "key": ref.key,
                "kind": ref.kind,
                "raw_target": ref.raw_target,
                "ordinal": ref.ordinal,
                "declared_at": now,
                "schema_version": ARTIFACT_SCHEMA_VERSION,
            }
            for ref in references
        ]

        # MERGE on (owner, key, raw_target): re-ingesting unchanged frontmatter
        # must not accumulate duplicate declarations, and an existing record
        # keeps its uuid so anything referring to it stays valid.
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            UNWIND $rows AS row
            MERGE (a)-[:DECLARES]->(d:ArtifactDeclaration {
                key: row.key, raw_target: row.raw_target
            })
            ON CREATE SET d.declaration_uuid = row.declaration_uuid,
                          d.kind             = row.kind,
                          d.declared_at      = row.declared_at,
                          d.resolution_status = $unresolved,
                          d.unresolved_reason = 'not_yet_resolved',
                          d.schema_version   = row.schema_version
            SET d.ordinal = row.ordinal, d.last_seen_at = row.declared_at
            """,
            {
                "uuid": artifact_uuid,
                "rows": rows,
                "unresolved": DeclarationStatus.UNRESOLVED,
            },
        )

        results = self.resolve_declarations(artifact_uuid)
        return {"declared": len(rows), "ignored": ignored, "results": results}

    def resolve_declarations(self, artifact_uuid: str) -> list[dict[str, Any]]:
        """Attempt resolution for every unresolved declaration on an artifact.

        Re-runnable by design: a declaration naming a plan that had not been
        ingested yet resolves on a later pass without the author touching the
        document. Already-resolved declarations are left alone so a successful
        resolution is never re-litigated against a graph that has since changed.
        """
        pending = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:DECLARES]->(d:ArtifactDeclaration)
            WHERE d.resolution_status <> $resolved
            RETURN d.declaration_uuid AS declaration_uuid, d.key AS key,
                   d.kind AS kind, d.raw_target AS raw_target
            ORDER BY d.ordinal ASC
            """,
            {"uuid": artifact_uuid, "resolved": DeclarationStatus.RESOLVED},
        )

        results: list[dict[str, Any]] = []
        for row in pending:
            outcome = self._resolve_one(artifact_uuid, row)
            self._stamp_declaration(row["declaration_uuid"], outcome)
            results.append(
                {**outcome, "raw_target": row["raw_target"], "key": row["key"]}
            )
        return results

    def _resolve_one(self, artifact_uuid: str, row: dict[str, Any]) -> dict[str, Any]:
        kind = row["kind"]
        raw_target = row["raw_target"]

        if kind == DeclarationKind.SUBJECT:
            return self._resolve_subject(artifact_uuid, raw_target)
        if kind == DeclarationKind.TODO:
            return self._resolve_todo(artifact_uuid, raw_target)

        target_uuid, reason = self._match_artifact(artifact_uuid, raw_target)
        if target_uuid is None:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": reason}

        if kind == DeclarationKind.SUPERSESSION:
            # Routed through supersede_artifact, never link_artifacts: the
            # document declaring "supersedes X" is the authority for X being
            # superseded, and the edge and status must move together.
            applied = self.supersede_artifact(artifact_uuid, target_uuid)
            if applied.get("applied"):
                return {
                    "status": DeclarationStatus.RESOLVED,
                    "target_uuid": target_uuid,
                }
            return {
                "status": DeclarationStatus.REJECTED,
                "reason": applied.get("reason"),
                "target_uuid": target_uuid,
            }

        linked = self.link_artifacts(artifact_uuid, target_uuid, row["key"])
        if linked.get("linked"):
            return {"status": DeclarationStatus.RESOLVED, "target_uuid": target_uuid}
        return {
            "status": DeclarationStatus.REJECTED,
            "reason": linked.get("reason"),
            "target_uuid": target_uuid,
        }

    def _match_artifact(
        self, artifact_uuid: str, raw_target: str
    ) -> tuple[str | None, str | None]:
        """Resolve a declared name to exactly one artifact uuid.

        Authors write either a title or the filename stem, so both are matched,
        case-insensitively. Ambiguity is a refusal rather than a first-match
        pick: choosing arbitrarily between two candidates would write a
        confident edge from an uncertain input, and the author is the only one
        who can say which was meant.
        """
        needle = raw_target.strip().lower()
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            MATCH (t:WorkArtifact)
            WHERE t.artifact_uuid <> $uuid
              AND """
            + namespace_compatibility_cypher()
            + """
            OPTIONAL MATCH (t)-[:EMBODIED_IN]->(s:ArtifactSource)
            WITH t, collect(toLower(coalesce(s.locator_path, ''))) AS paths
            WHERE toLower(t.title) = $needle
               OR any(p IN paths WHERE p = $needle
                      OR p ENDS WITH ('/' + $needle + '.md')
                      OR p = ($needle + '.md'))
            RETURN DISTINCT t.artifact_uuid AS artifact_uuid
            LIMIT 5
            """,
            {
                "uuid": artifact_uuid,
                "needle": needle,
                "default_ns": DEFAULT_ARTIFACT_NAMESPACE,
            },
        )
        if not rows:
            return None, "target_not_found"
        if len(rows) > 1:
            return None, "ambiguous_target"
        return rows[0]["artifact_uuid"], None

    def _resolve_subject(self, artifact_uuid: str, raw_target: str) -> dict[str, Any]:
        """Resolve an ``about:`` declaration to an existing semantic entity.

        Deliberately does not mint a missing entity, despite the design allowing
        it. An :Entity created here would carry no name embedding and would be
        silently invisible to recall -- a worse outcome than an honest
        unresolved declaration, because it looks like it worked. Entity creation
        belongs to the ingest path that stamps embeddings.
        """
        needle = raw_target.strip().lower()
        rows = self.neo4j.execute(
            """
            MATCH (e:Entity)
            WHERE toLower(e.name) = $needle
              AND e.scope = 'PERSISTENT'
              AND e.structure_role IS NULL
            RETURN e.uuid AS entity_uuid
            LIMIT 5
            """,
            {"needle": needle},
        )
        if not rows:
            return {
                "status": DeclarationStatus.UNRESOLVED,
                "reason": "subject_not_found",
            }
        if len(rows) > 1:
            return {
                "status": DeclarationStatus.UNRESOLVED,
                "reason": "ambiguous_subject",
            }

        entity_uuid = rows[0]["entity_uuid"]
        linked = self.link_subject(artifact_uuid, entity_uuid)
        if linked.get("linked"):
            return {"status": DeclarationStatus.RESOLVED, "target_uuid": entity_uuid}
        return {
            "status": DeclarationStatus.REJECTED,
            "reason": linked.get("reason"),
            "target_uuid": entity_uuid,
        }

    def _resolve_todo(self, artifact_uuid: str, raw_target: str) -> dict[str, Any]:
        """Resolve a ``todos:`` declaration.

        Todos have no titles, only content, so the declared value must be a uuid
        or an unambiguous uuid prefix -- the short form that already appears in
        every todo listing. Matching on content would be inference.
        """
        needle = raw_target.strip()
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            MATCH (t:Todo)
            WHERE (t.uuid = $needle OR t.uuid STARTS WITH $needle)
              AND """
            + namespace_compatibility_cypher()
            + """
            RETURN t.uuid AS todo_uuid
            LIMIT 5
            """,
            {
                "uuid": artifact_uuid,
                "needle": needle,
                "default_ns": DEFAULT_ARTIFACT_NAMESPACE,
            },
        )
        if not rows:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": "todo_not_found"}
        if len(rows) > 1:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": "ambiguous_todo"}

        todo_uuid = rows[0]["todo_uuid"]
        linked = self.link_todo(artifact_uuid, todo_uuid)
        if linked.get("linked"):
            return {"status": DeclarationStatus.RESOLVED, "target_uuid": todo_uuid}
        return {
            "status": DeclarationStatus.REJECTED,
            "reason": linked.get("reason"),
            "target_uuid": todo_uuid,
        }

    def _stamp_declaration(
        self, declaration_uuid: str, outcome: dict[str, Any]
    ) -> None:
        self.neo4j.execute(
            """
            MATCH (d:ArtifactDeclaration {declaration_uuid: $uuid})
            SET d.resolution_status = $status,
                d.unresolved_reason = $reason,
                d.resolved_uuid     = $target_uuid,
                d.resolved_at       = $now
            """,
            {
                "uuid": declaration_uuid,
                "status": outcome["status"],
                "reason": outcome.get("reason"),
                "target_uuid": outcome.get("target_uuid"),
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )

    def declarations(self, artifact_uuid: str) -> list[dict[str, Any]]:
        """Every declaration on an artifact, resolved or not, in author order."""
        return self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:DECLARES]->(d:ArtifactDeclaration)
            RETURN d.declaration_uuid  AS declaration_uuid,
                   d.key               AS key,
                   d.kind              AS kind,
                   d.raw_target        AS raw_target,
                   d.ordinal           AS ordinal,
                   d.resolution_status AS resolution_status,
                   d.unresolved_reason AS unresolved_reason,
                   d.resolved_uuid     AS resolved_uuid
            ORDER BY d.ordinal ASC
            """,
            {"uuid": artifact_uuid},
        )
