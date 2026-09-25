"""WorkArtifact delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphWorkArtifactsMixin:
    """WorkArtifact delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # WorkArtifact delegates → WorkArtifactRepository
    # -------------------------------------------------------------------------

    def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        return self._work_artifacts.get_artifact(artifact_uuid, namespace=namespace)

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._work_artifacts.list_artifacts(
            artifact_type=artifact_type, status=status, namespace=namespace, limit=limit
        )

    def list_artifact_questions(
        self,
        *,
        artifact_uuid: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._work_artifacts.open_questions(
            artifact_uuid=artifact_uuid, status=status, namespace=namespace, limit=limit
        )

    def get_artifact_relationships(self, artifact_uuid: str) -> dict[str, list[dict[str, Any]]]:
        return self._work_artifacts.artifact_relationships(artifact_uuid)

    def link_artifacts(
        self, source_uuid: str, target_uuid: str, relation: str
    ) -> dict[str, Any]:
        return self._work_artifacts.link_artifacts(source_uuid, target_uuid, relation)

    def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        return self._work_artifacts.supersede_artifact(new_uuid, old_uuid)

    def transition_artifact_status(
        self, artifact_uuid: str, to_status: str, *, namespace: str | None = None
    ) -> dict[str, Any]:
        return self._work_artifacts.transition_status(
            artifact_uuid, to_status, namespace=namespace
        )

    def fetch_artifact_corpus_audit(
        self,
        *,
        repo_path: str,
        repository: str,
        from_commit: str | None = None,
        conflict_limit: int = 25,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Read-only corpus parity summary, with a bounded conflict list.

        The full ledger stays in CLI JSON output. A caller over MCP wants to know
        whether the corpus is in sync and what is wrong, not to receive several
        hundred action records through a chat transport.
        """
        from menhir.services.artifact_reconciliation_service import (
            ArtifactReconciliationService,
        )

        service = ArtifactReconciliationService(self._work_artifacts)
        report = service.audit(
            repo_path, repository=repository, from_commit=from_commit,
            namespace=namespace,
        )
        limit = max(1, min(int(conflict_limit), 100))
        conflicts = report.conflicts
        return {
            "repository": report.repository,
            "observed_commit": report.observed_commit,
            "cursor_commit": report.cursor_commit,
            "evidence_from_commit": report.evidence_from_commit,
            "evidence_base_valid": report.evidence_base_valid,
            "plan_digest": report.plan_digest,
            "counts": dict(report.counts),
            "conflicts": [c.as_dict() for c in conflicts[:limit]],
            "conflicts_truncated": max(0, len(conflicts) - limit),
            "contradictions": [c.as_dict() for c in report.contradictions[:limit]],
        }

    def relocate_artifact_source(
        self,
        *,
        artifact_uuid: str,
        old_path: str,
        new_path: str,
        repository: str | None = None,
        medium: str = "markdown",
        expected_old_integrity: str = "",
        observed_integrity: str = "",
    ) -> dict[str, Any]:
        """Move one source's locator, checked against the artifact that owns it.

        The artifact UUID is not decoration: it is the caller stating which
        record they believe is at the old path. If the path belongs to a
        different artifact, that disagreement is the whole finding, and applying
        the move anyway would silently transplant one document's history onto
        another.
        """
        from datetime import datetime, timezone

        from menhir.domain.artifact_reconciliation import (
            MatchBasis,
            SourceObservation,
        )

        if not (repository or "").strip():
            # The locator key is repository-scoped, so an empty repository can
            # only ever miss. Saying "not found" would send the caller hunting
            # for the path instead of supplying the argument they omitted.
            return {"applied": False, "reason": "repository_required"}

        source_uuid, reason = self._work_artifacts._source_uuid_at_locator(  # noqa: SLF001
            repository, medium, old_path
        )
        if source_uuid is None:
            return {"applied": False, "reason": reason}

        owner = self._work_artifacts.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource {source_uuid: $source_uuid})
            RETURN a.artifact_uuid AS artifact_uuid
            """,
            {"source_uuid": source_uuid},
        )
        owner_uuid = owner[0].get("artifact_uuid") if owner else None
        if owner_uuid != artifact_uuid:
            return {
                "applied": False,
                "reason": "uuid_locator_disagreement",
                "locator_owner": owner_uuid,
            }

        observation = SourceObservation(
            integrity=observed_integrity or None,
            observed_at=datetime.now(timezone.utc).isoformat(),
            basis=MatchBasis.DECLARED_UUID,
        )
        return self._work_artifacts.relocate_artifact_source(
            source_uuid=source_uuid,
            old_locator={"repository": repository, "path": old_path, "medium": medium},
            new_locator={"repository": repository, "path": new_path, "medium": medium},
            observation=observation,
            expected_integrity=expected_old_integrity or None,
        )

    def reconcile_file_event_source(
        self,
        *,
        path: str,
        operation: str,
        old_path: str | None = None,
        repository: str | None = None,
        after_hash: str | None = None,
        git_commit: str | None = None,
    ) -> dict[str, Any]:
        """Best-effort source reconciliation for one observed file event.

        Returns an outcome rather than raising. Structural dirty marking already
        happened by the time this runs, and a reconciliation refusal must never
        undo it or block the coding tool that reported the change.
        """
        from datetime import datetime, timezone

        from menhir.domain.artifact_reconciliation import (
            MatchBasis,
            SourceObservation,
            medium_for_path,
            route_for_path,
        )

        op = (operation or "").strip().lower()
        route = route_for_path(path)
        medium = medium_for_path(path)
        if route is None or medium is None:
            return {"attempted": False, "reason": "path_is_not_corpus_material"}

        observation = SourceObservation(
            integrity=after_hash or None,
            lane=route.lane,
            observed_commit=git_commit,
            observed_at=datetime.now(timezone.utc).isoformat(),
            basis=MatchBasis.GIT_RENAME if op == "rename" else MatchBasis.EXACT_LOCATOR,
        )
        try:
            if op == "rename" and old_path:
                result = self._work_artifacts.relocate_artifact_source_by_locator(
                    repository=repository or "",
                    medium=medium,
                    old_path=old_path,
                    new_path=path,
                    observation=observation,
                )
            elif op in ("edit", "write"):
                result = self._work_artifacts.refresh_artifact_source_by_locator(
                    repository=repository or "",
                    medium=medium,
                    path=path,
                    observation=observation,
                )
            else:
                # `create` carries no document metadata, so registering from the
                # event alone would mint an identity from a filename. The next
                # audit registers it with the record actually read.
                return {"attempted": False, "reason": f"operation_not_reconciled:{op}"}
        except Exception as exc:  # noqa: BLE001 - fail open, never block the hook
            return {"attempted": True, "applied": False, "reason": f"error:{type(exc).__name__}"}
        return {"attempted": True, **result}
