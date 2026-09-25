"""Merge precondition and state-fingerprint queries.

Moved verbatim from ``correlation_queries.py`` and composed into
``CorrelationRepository`` via this mixin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from menhir.domain.merge_eligibility import MergeEligibility

from menhir.domain.retention import source_retention_protected_cypher


class CorrelationMergeStateMixin:
    """Merge-decision half of ``CorrelationRepository``: the eligibility gate and the
    read-only state fingerprints the merge saga compares before and after."""

    # ------------------------------------------------------------------
    # Entity merge (near-duplicate absorption)
    # ------------------------------------------------------------------

    def evaluate_merge_eligibility(self, survivor_uuid: str, absorbed_uuid: str) -> "MergeEligibility":
        """Gather both nodes' material state in ONE read and decide eligibility (plan Phase 3).

        The decision itself is the pure ``domain.merge_eligibility.evaluate``; this method only owns
        the Cypher that reads the signals. Used as the final mutation-time precondition in
        ``merge_entity`` AND callable by the service classifier, so one policy governs both.
        """
        from menhir.domain import merge_eligibility as me

        rows = self._neo4j.execute(
            f"""
            MATCH (n:Entity) WHERE n.uuid IN [$s, $a]
            RETURN n.uuid AS uuid,
                   ({self._INELIGIBLE_ROLE_PREDICATE}) AS ineligible_role,
                   coalesce(n.namespace, n.group_id, 'default') AS namespace,
                   n.freshness AS freshness,
                   n.scope AS scope,
                   coalesce(n.user_flagged, false) AS user_flagged,
                   {source_retention_protected_cypher("n")} AS source_retention_protected,
                   n.conflict_status AS conflict_status
            """,
            params={"s": survivor_uuid, "a": absorbed_uuid},
        )
        by_uuid = {str(r["uuid"]): dict(r) for r in rows}

        def _signals(uuid: str) -> me.NodeSignals:
            r = by_uuid.get(uuid)
            if r is None:
                return me.NodeSignals(
                    uuid=uuid, exists=False, ineligible_role=False, namespace="",
                    freshness=None, scope=None, user_flagged=False, conflict_status=None,
                    source_retention_protected=False,
                )
            return me.NodeSignals(
                uuid=uuid,
                exists=True,
                ineligible_role=bool(r.get("ineligible_role", False)),
                namespace=str(r.get("namespace") or ""),
                freshness=(str(r["freshness"]) if r.get("freshness") is not None else None),
                scope=(str(r["scope"]) if r.get("scope") is not None else None),
                user_flagged=bool(r.get("user_flagged", False)),
                conflict_status=(
                    str(r["conflict_status"]) if r.get("conflict_status") is not None else None
                ),
                source_retention_protected=bool(r.get("source_retention_protected", False)),
            )

        return me.evaluate(_signals(survivor_uuid), _signals(absorbed_uuid))

    def fetch_survivor_properties(self, survivor_uuid: str) -> dict[str, Any] | None:
        """The survivor's current merge-owned properties, for the invariant-9 newer-state check.

        Returns EVERY key in ``merge_delta.MERGE_OWNED_SURVIVOR_PROPERTIES``. Guard 2 compares this
        against the replayed merge output key by key, so a property the merge writes but this query
        omits reads back as None and can never match -- which is why the four-field version refused
        every unmerge of a post-`01a10e4` merge with SURVIVOR_CHANGED_SINCE_MERGE, `sources` and
        `corroboration` reported as missing on a survivor that in fact held them.
        """
        rows = self._neo4j.execute(
            "MATCH (s:Entity {uuid: $u}) RETURN s.summary AS summary, s.content AS content, "
            "s.source AS source, s.source_confidence AS source_confidence, "
            "s.sources AS sources, s.corroboration AS corroboration",
            params={"u": survivor_uuid},
        )
        return dict(rows[0]) if rows else None

    def fetch_merge_state(self, survivor_uuid: str, absorbed_uuid: str) -> dict[str, Any]:
        """The material after-state of a merge, for the saga's before/after fingerprint (Phase 4).

        Deliberately small and total: presence of both nodes, whether the survivor's lineage records
        this absorption, and which operation last merged into the survivor. That is exactly what
        distinguishes 'not yet merged' from 'merged by THIS op' from 'something else happened'.
        """
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (s:Entity {uuid: $s})
            OPTIONAL MATCH (a:Entity {uuid: $a})
            RETURN s IS NOT NULL AS survivor_present,
                   a IS NOT NULL AS absorbed_present,
                   coalesce($a IN coalesce(s.merged_from, []), false) AS lineage_recorded,
                   s.last_merge_op_id AS last_merge_op_id
            """,
            params={"s": survivor_uuid, "a": absorbed_uuid},
        )
        if not rows:
            return {
                "survivor_present": False, "absorbed_present": False,
                "lineage_recorded": False, "last_merge_op_id": None,
            }
        return dict(rows[0])
