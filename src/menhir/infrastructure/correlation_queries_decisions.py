"""Deterministic merge vetoes and merge-decision metadata reads.

Moved verbatim from ``correlation_queries.py`` and composed into
``CorrelationRepository`` via this mixin.
"""

from __future__ import annotations

from typing import Any


class CorrelationVetoMixin:
    """Veto half of ``CorrelationRepository``: read-only checks that must veto a merge
    before any mutation, plus the minimal metadata fetch that feeds merge decisions."""

    # ------------------------------------------------------------------
    # Fetch node metadata for merge decisions
    # ------------------------------------------------------------------

    def fetch_entity_merge_metadata(
        self,
        uuids: list[str],
    ) -> list[dict[str, Any]]:
        """Fetch minimal metadata for a list of entity UUIDs to support merge decisions."""
        if not uuids:
            return []
        rows = self._neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.summary AS summary,
                   n.content AS content,
                   n.source AS source,
                   n.source_confidence AS source_confidence,
                   n.scope AS scope,
                   n.created_at AS created_at,
                   n.structure_role AS structure_role
            """,
            params={"uuids": uuids},
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Deterministic merge vetoes (Part 1)
    # ------------------------------------------------------------------

    def check_co_mention_veto(
        self,
        uuid_a: str,
        uuid_b: str,
    ) -> bool:
        """Co-mention veto: if both entities are MENTIONED by the same episode, they are distinct.

        Returns True if a veto applies (both nodes co-mentioned), False otherwise.

        The provenance edge is ``(:Episodic)-[:MENTIONS]->(:Entity)`` -- see
        ``schema.EDGE_LABELS``. This query previously used a non-existent
        ``MENTIONED_IN`` type, so it matched nothing and the veto never fired
        (fail-open). Matched undirected so the direction of the edge cannot
        silently break it again.
        """
        rows = self._neo4j.execute(
            """
            MATCH (a:Entity {uuid: $a})-[:MENTIONS]-(ep:Episodic)
            MATCH (b:Entity {uuid: $b})-[:MENTIONS]-(ep)
            RETURN count(ep) AS shared_episodes
            """,
            params={"a": uuid_a, "b": uuid_b},
        )
        if rows:
            shared_count = int(rows[0].get("shared_episodes", 0) or 0)
            return shared_count > 0
        return False

    def check_anchor_project_veto(
        self,
        uuid_a: str,
        uuid_b: str,
    ) -> bool:
        """Anchor-project veto: if both entities are anchored to different single projects, veto.

        Returns True if a veto applies (both anchored to different projects), False otherwise.
        """
        rows = self._neo4j.execute(
            """
            MATCH (a:Entity {uuid: $a})-[:ANCHORED_TO]->(proj_a)
            MATCH (b:Entity {uuid: $b})-[:ANCHORED_TO]->(proj_b)
            WHERE proj_a <> proj_b
            RETURN count(*) > 0 AS veto_applies
            """,
            params={"a": uuid_a, "b": uuid_b},
        )
        if rows:
            return bool(rows[0].get("veto_applies", False))
        return False

    def check_ineligible_node_veto(
        self,
        survivor_uuid: str,
        absorbed_uuid: str,
    ) -> bool:
        r"""Ineligible-node veto: structural or path-shaped nodes must never merge.

        The rule lives in `_INELIGIBLE_ROLE_PREDICATE` and is interpolated here rather than
        restated, because restating it is exactly how this drifted: structural nodes, derived
        nodes (`is_view` / `is_quantstate` / `view_kind`), and path-shaped names.

        Returns True if EITHER node is ineligible (veto applies), False otherwise.
        """
        rows = self._neo4j.execute(
            f"""
            MATCH (n:Entity) WHERE n.uuid IN [$a, $b]
            WITH n, ({self._INELIGIBLE_ROLE_PREDICATE}) AS ineligible_node
            RETURN count(CASE WHEN ineligible_node THEN 1 END) > 0 AS ineligible
            """,
            params={"a": survivor_uuid, "b": absorbed_uuid},
        )
        if rows:
            return bool(rows[0].get("ineligible", False))
        return False
