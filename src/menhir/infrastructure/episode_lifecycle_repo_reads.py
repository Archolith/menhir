"""Episode read helpers: processing snapshots and linked-entity lookups.

Mixin of :class:`menhir.infrastructure.episode_lifecycle.EpisodeLifecycleRepository`; moved
verbatim from that module, which still re-exports the composed class.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.cypher import (
    Cypher,
    MEMORY_RETURN_FIELDS,
    non_derived_view_cypher,
)


class _EpisodeReadsMixin:
    """Read-only queries over episodes and their linked entities."""

    def fetch_episode_processing(self, episode_uuid: str) -> dict[str, Any] | None:
        # MEMORY_RETURN_FIELDS already includes processing_substage/
        # processing_substage_started_at and the active LLM task/kind/model/endpoint
        # fields (SSOT-11, see cypher.py's _PROCESSING_DETAIL_FIELDS docstring). This
        # call site used to re-add them explicitly from before that fix and was never
        # cleaned up, producing a RETURN clause with duplicate column aliases --
        # tolerated silently until now, but a genuine syntax error
        # (Neo.ClientError.Statement.SyntaxError: "Multiple result columns with the
        # same name are not supported"), caught live during a 2026-07-12 graphiti-core
        # 0.29.2 upgrade canary (this path only runs during background enrichment
        # finalize, which the offline test suite doesn't exercise against a real
        # Neo4j server).
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $episode_uuid")
            .return_fields(MEMORY_RETURN_FIELDS)
            .limit("1")
            .build()
        )
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
        return rows[0] if rows else None

    def fetch_linked_entity_uuids_for_episode(self, episode_uuid: str) -> list[str]:
        query = (
            Cypher()
            .match("(e:Episodic)-[]-(n:Entity)")
            .where("e.uuid = $episode_uuid")
            .return_raw("DISTINCT n.uuid AS uuid")
            .build()
        )
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
        return [str(row["uuid"]) for row in rows if row.get("uuid")]

    def fetch_linked_entity_uuids_for_episodes(
        self, episode_uuids: list[str]
    ) -> dict[str, list[str]]:
        """Same as the singular form, for many episodes in ONE round trip.

        CF-75: the recall path ran the singular query once per resolved episode, serially, each
        awaiting a full round trip before starting the next. The saving here is round trips, NOT
        database work -- measured against Neo4j 5, the batched form costs slightly MORE dbHits
        (1,150 vs 900 for 50 episodes) because of the UNWIND and the aggregation. That is the
        opposite of the edge-weight loop, where batching removes N whole-store scans, and the
        difference is why the two were measured separately rather than assumed to share a profile.

        Returns a uuid -> linked-entity-uuids map. The caller re-orders by its own input list, so
        this deliberately does not promise UNWIND's row order survives the aggregation.
        """
        if not episode_uuids:
            return {}
        rows = self.neo4j.execute(
            """
            UNWIND $episode_uuids AS episode_uuid
            MATCH (e:Episodic)-[]-(n:Entity)
            WHERE e.uuid = episode_uuid
            RETURN episode_uuid AS episode_uuid, collect(DISTINCT n.uuid) AS uuids
            """,
            params={"episode_uuids": list(dict.fromkeys(episode_uuids))},
        )
        return {
            str(row["episode_uuid"]): [str(u) for u in (row.get("uuids") or []) if u]
            for row in rows
            if row.get("episode_uuid")
        }

    def fetch_linked_entities_for_episode(self, episode_uuid: str) -> list[dict[str, str]]:
        """Surviving REAL entities linked to an episode as {uuid, name} rows — the post-finalization
        binding candidates for ScalarStateView typed-scalar perception (C.4.3). Name-carrying variant
        of `fetch_linked_entity_uuids_for_episode`: binding matches the extracted subject_text against
        these names, so the uuid list alone is insufficient.

        `episode_uuid` accepts EITHER identifier kind, because the scalar batch does not always key on
        an episode. When `:TurnEvidence` exists, `load_next_scalar_batch` takes the G14 branch and
        returns rows keyed by `turn_id` ("turn_id as the grounding anchor"), which then flows through
        `build_episodes` into the proposal's `episode_uuid`. Matching that id against `Episodic.uuid`
        alone can NEVER hit, so the candidate list came back empty on every call and every non-self
        subject abstained from authority — silently, because `_resolve_subject` tries the self seam
        first and that path bypasses this lookup entirely. So a turn_id is resolved to its episode
        through the `ADMITTED_ON` admission join. Accepting both kinds here (rather than at the call
        site) keeps the single-id contract the binder and the repair pass share.

        DERIVED View nodes are EXCLUDED. Recallable Views (counter and scalar_state) are themselves
        stored as `:Entity` (carrying `is_view`/`view_kind`, and counters `is_quantstate`) and are
        linked to their source episodes via `(:Episodic)-[:MENTIONS]->(view:Entity)` — and the
        scheduler writes counter Views BEFORE typed-scalar binding runs, so an unfiltered lookup would
        offer a View as a binding target and could bind a scalar assertion to a projection. Filtered
        BOTH in Cypher (at source) and in Python (defense-in-depth on the returned rows). DISTINCT by
        uuid."""
        # Entities attach to GRAPHITI's episode node, not to menhir's pending one. The two are
        # content-identical twins created by different writers; the pending node points at its twin
        # through `resolved_episode_uuid`. Anchoring on the pending node alone returns nothing, so
        # the anchor set is BOTH: whichever node the id names, plus its resolved twin.
        # f-string: the helper is interpolated, so every LITERAL Cypher brace below is doubled.
        query = f"""
            MATCH (a:Episodic)
            WHERE a.uuid = $episode_uuid
               OR EXISTS {{ (a)-[:ADMITTED_ON]->(:TurnEvidence {{turn_id: $episode_uuid}}) }}
            OPTIONAL MATCH (g:Episodic {{uuid: a.resolved_episode_uuid}})
            WITH collect(DISTINCT a) + collect(DISTINCT g) AS anchors
            UNWIND anchors AS e
            MATCH (e)-[]-(n:Entity)
            WHERE {non_derived_view_cypher("n")}
            RETURN DISTINCT n.uuid AS uuid, n.name AS name,
                   coalesce(n.is_view, false) AS is_view,
                   coalesce(n.is_quantstate, false) AS is_quantstate,
                   n.view_kind AS view_kind
        """
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
        out: list[dict[str, str]] = []
        for row in rows:
            if not row.get("uuid"):
                continue
            if row.get("is_view") or row.get("is_quantstate") or row.get("view_kind"):
                continue  # defense-in-depth: never bind a scalar assertion to a derived View node
            out.append({"uuid": str(row["uuid"]), "name": str(row.get("name") or "")})
        return out
