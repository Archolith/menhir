"""Read-only graph inputs for Projection Coverage audits.

This repository is intentionally separate from the assertion and View write facades.  It exposes only
the durable rows needed by the audit, so running Projection Coverage cannot acquire a mutation surface
by accident.
"""

from __future__ import annotations

import json
from typing import Any


class ProjectionCoverageRepository:
    """Purpose-built Neo4j reader for scalar projection coverage."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    @staticmethod
    def _hydrate_assertion(row: dict[str, Any]) -> dict[str, Any]:
        """Restore the typed scalar from its canonical JSON payload when available."""
        payload = row.get("value_json")
        if isinstance(payload, str) and payload:
            try:
                row["value"] = json.loads(payload)
            except (TypeError, ValueError):
                # Corrupt legacy payloads stay visible to the audit as their stored scalar surface.
                pass
        return row

    def assertions_for_projection_audit(
        self,
        subject_uuid: str,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every durable assertion version plus its current-head ownership in one read.

        The audit needs historical versions for lifecycle accounting, while fold membership is decided
        later from lifecycle/binding/eligibility.  Head/current data is collected in the same query so
        a binding mismatch never requires a per-assertion lookup.
        """
        subject_uuid = str(subject_uuid or "").strip()
        if not subject_uuid:
            raise ValueError("projection coverage audit requires a non-blank subject_uuid")

        rows = self._neo4j.execute(
            """
            MATCH (a:TypedAssertion {subject_uuid: $subject_uuid})
            WHERE $namespace IS NULL OR a.namespace = $namespace
            OPTIONAL MATCH (h:TypedAssertionHead {source_key: a.source_key})
            OPTIONAL MATCH (h)-[:CURRENT]->(cur:TypedAssertion)
            WITH a, h, collect(DISTINCT cur) AS currents
            RETURN a.assertion_id AS assertion_id, a.assertion_key AS assertion_key,
                   a.claim_key AS claim_key, a.source_key AS source_key,
                   a.subject_uuid AS subject_uuid, a.subject_display AS subject_display,
                   a.namespace AS namespace,
                   a.attribute AS attribute, coalesce(a.scope, '') AS scope,
                   a.value_kind AS value_kind, coalesce(a.unit, '') AS unit,
                   a.operation AS operation, a.value AS value, a.value_json AS value_json,
                   a.stated_span AS stated_span,
                   toString(a.valid_at) AS valid_at, toString(a.learned_at) AS learned_at,
                   a.episode_uuid AS episode_uuid, a.evidence_tier AS evidence_tier,
                   a.perceiver_version AS perceiver_version,
                   coalesce(a.absolute_semantics, 'ordinary') AS absolute_semantics,
                   coalesce(a.superseded, false) AS superseded,
                   a.superseded_by AS superseded_by,
                   coalesce(a.binding_pending, false) AS binding_pending,
                   coalesce(a.projection_pending, false) AS projection_pending,
                   coalesce(a.activation_pending, false) AS activation_pending,
                   CASE WHEN h IS NULL THEN false ELSE true END AS head_present,
                   h.subject_uuid AS head_subject_uuid,
                   size(currents) AS head_current_count,
                   CASE
                       WHEN size(currents) = 1 THEN currents[0].assertion_id
                       WHEN size(currents) > 1 THEN '__multiple_current__'
                       WHEN h IS NOT NULL THEN '__missing_current__'
                       ELSE null
                   END AS head_current_assertion_id,
                   CASE
                       WHEN size(currents) = 1 THEN currents[0].subject_uuid
                       WHEN size(currents) > 1 THEN '__multiple_current__'
                       WHEN h IS NULL THEN '__missing_head__'
                       ELSE null
                   END AS head_current_subject_uuid
            ORDER BY coalesce(a.namespace, ''), a.attribute, coalesce(a.scope, ''),
                     a.value_kind, coalesce(a.unit, ''), a.valid_at, a.learned_at,
                     a.assertion_id
            """,
            params={"subject_uuid": subject_uuid, "namespace": namespace},
        )
        return [self._hydrate_assertion(dict(row)) for row in rows]

    def list_scalar_state_views_for_audit(
        self,
        *,
        subject_uuid: str,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return complete current ScalarStateView parity rows for one entity.

        Namespace filtering is exact when supplied; a foreign View is never allowed to satisfy a
        namespace-scoped audit.
        """
        subject_uuid = str(subject_uuid or "").strip()
        if not subject_uuid:
            raise ValueError("projection coverage audit requires a non-blank subject_uuid")

        rows = self._neo4j.execute(
            """
            MATCH (n:Entity {view_kind: 'scalar_state', view_subject_uuid: $subject_uuid})
            WHERE coalesce(n.view_current, true)
              AND ($namespace IS NULL OR n.group_id = $namespace)
            RETURN n.uuid AS uuid, n.view_key AS view_key,
                   n.view_subject_uuid AS subject_uuid, n.group_id AS namespace,
                   coalesce(n.view_current, true) AS view_current,
                   n.ss_attribute AS attribute, coalesce(n.ss_scope, '') AS scope,
                   n.ss_kind AS value_kind, coalesce(n.ss_unit, '') AS unit,
                   n.ss_value AS view_value, toString(n.valid_at) AS valid_at,
                   coalesce(n.scalar_contributors, []) AS scalar_contributors,
                   coalesce(n.scalar_effective_tier, '') AS scalar_effective_tier,
                   coalesce(n.episode_uuids, []) AS episode_uuids,
                   coalesce(n.retired, false) AS retired,
                   toString(n.expired_at) AS expired_at,
                   n.superseded_by AS superseded_by
            ORDER BY coalesce(n.group_id, ''), n.ss_attribute, coalesce(n.ss_scope, ''),
                     n.ss_kind, coalesce(n.ss_unit, ''), n.view_key, n.uuid
            """,
            params={"subject_uuid": subject_uuid, "namespace": namespace},
        )
        return [dict(row) for row in rows]
