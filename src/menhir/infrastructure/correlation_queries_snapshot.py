"""Merge snapshot capture, restore, and node-state reads.

Moved verbatim from ``correlation_queries.py`` and composed into
``CorrelationRepository`` via this mixin.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.retention import same_tenant_cypher
from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S


class CorrelationSnapshotMixin:
    """Snapshot half of ``CorrelationRepository``: lossless node-state capture, the
    versioned merge snapshot, its atomic inverse, and peer-existence checks."""

    def capture_node_state(self, uuid: str) -> dict[str, Any] | None:
        """Read ONE node's complete state for a lossless snapshot (plan Phase 4, section 2).

        Complete means: all labels, ``properties(n)`` with driver types intact (no stringifying), and
        EVERY incident relationship instance regardless of peer label -- with its type, direction, and
        full ``properties(r)``, plus the peer's uuid/labels and a best-effort identity used only to
        REPORT an unresolvable peer.

        Parallel edges are preserved: the OPTIONAL MATCH yields one row per relationship INSTANCE, so
        two edges of the same type to the same peer appear twice (the legacy snapshot collapsed them
        into a (peer, type, direction) set and could not restore multiplicity).

        Returns None when the node does not exist.
        """
        rows = self._neo4j.execute(
            """
            MATCH (n {uuid: $u})
            OPTIONAL MATCH (n)-[r]-(peer)
            WITH n, r, peer,
                 CASE WHEN r IS NULL THEN NULL
                      WHEN elementId(startNode(r)) = elementId(n) THEN 'out' ELSE 'in' END AS direction
            RETURN n.uuid AS uuid,
                   labels(n) AS labels,
                   properties(n) AS properties,
                   collect(CASE WHEN r IS NULL THEN NULL ELSE {
                       type: type(r),
                       direction: direction,
                       properties: properties(r),
                       peer_uuid: peer.uuid,
                       peer_labels: labels(peer),
                       peer_identity: coalesce(peer.uuid, peer.name, elementId(peer))
                   } END) AS relationships
            """,
            params={"u": uuid},
        )
        if not rows:
            return None
        row = dict(rows[0])
        rels = [dict(r) for r in (row.get("relationships") or []) if r]
        from menhir.domain import merge_snapshot as ms

        return ms.encode_node(
            uuid=str(row["uuid"]),
            labels=list(row.get("labels") or []),
            properties=dict(row.get("properties") or {}),
            relationships=rels,
        )

    def capture_merge_snapshot(
        self, survivor_uuid: str, absorbed_uuid: str, *, similarity: float | None = None
    ) -> dict[str, Any]:
        """Complete, versioned, checksummed snapshot of BOTH merge participants (plan Phase 4).

        The survivor is snapshotted too: an exact unmerge must reverse the survivor's merge-owned
        delta (content/summary/source/confidence/provenance/bridges), not just recreate the absorbed
        node. Raises if either node is missing -- a snapshot that cannot express the inverse must not
        be written.
        """
        from menhir.domain import merge_snapshot as ms

        survivor = self.capture_node_state(survivor_uuid)
        absorbed = self.capture_node_state(absorbed_uuid)
        missing = [u for u, s in ((survivor_uuid, survivor), (absorbed_uuid, absorbed)) if s is None]
        if missing:
            raise ms.SnapshotSchemaError(f"cannot snapshot merge: node(s) not found: {missing}")
        return ms.build_snapshot(survivor=survivor, absorbed=absorbed, similarity=similarity)

    def restore_merge_snapshot(
        self,
        *,
        survivor_uuid: str,
        absorbed_uuid: str,
        absorbed_labels: list[str],
        absorbed_properties: dict[str, Any],
        out_rels: list[dict[str, Any]],
        in_rels: list[dict[str, Any]],
        survivor_properties: dict[str, Any],
        rebound_episodes: list[str],
        operation_id: str,
        rebound_retention_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Invert a merge in ONE atomic Cypher transaction (plan Phase 5).

        The legacy script did this in five separate statements with a skip-if-the-node-exists guard,
        so a crash midway left a bare node that every rerun then SKIPPED -- permanently half-restored.
        One statement removes that failure mode entirely: it all lands or none of it does.

        Neo4j 5.26 dynamic labels (``:$(...)``) and dynamic relationship types (``-[r:$(t)]->``) let
        this stay fully parameterized -- no string interpolation of graph identifiers, so a label or
        type from a snapshot cannot inject Cypher.

        What it does:
          * recreate the absorbed node with its EXACT labels and typed properties;
          * recreate every incident relationship instance (both directions, with properties);
          * restore the survivor's merge-owned property delta to its pre-merge values;
          * delete only the bridges this merge created (``bridged_from = absorbed``);
          * delete only the MENTIONS this merge rebound onto the survivor;
          * remove this absorption from the survivor's lineage SUBTRACTIVELY, so absorptions made by
            OTHER merges after this one survive.

        Callers must have already verified that every referenced peer exists -- this query does not
        fabricate peers, and a missing one must abstain upstream, not silently drop an edge.
        """
        rows = self._neo4j.execute(
            """
            MATCH (s:Entity {uuid: $survivor})
            CREATE (a:$($absorbed_labels))
            SET a = $absorbed_properties,
                a.restored_from_merge = $survivor,
                a.restored_by_op = $operation_id,
                a.restored_at = datetime()
            WITH s, a
            CALL {
                WITH a
                UNWIND $out_rels AS rel
                MATCH (peer {uuid: rel.peer_uuid})
                CREATE (a)-[r:$(rel.type)]->(peer)
                SET r += rel.properties
                RETURN count(r) AS out_restored
            }
            CALL {
                WITH a
                UNWIND $in_rels AS rel
                MATCH (peer {uuid: rel.peer_uuid})
                CREATE (peer)-[r:$(rel.type)]->(a)
                SET r += rel.properties
                RETURN count(r) AS in_restored
            }
            // Reverse the survivor's merge-owned delta (content/summary/source/confidence).
            SET s += $survivor_properties
            WITH s, a, out_restored, in_restored
            CALL {
                WITH s
                MATCH (s)-[b:RELATES_TO {bridged_from: $absorbed}]-()
                DELETE b
                RETURN count(b) AS bridges_removed
            }
            CALL {
                WITH s
                MATCH (ep:Episodic)-[m:MENTIONS]->(s)
                WHERE ep.uuid IN $rebound_episodes
                DELETE m
                RETURN count(m) AS mentions_removed
            }
            CALL {
                WITH s
                MATCH (source:Episodic)-[r:RETENTION_SOURCE]->(s)
                WHERE source.uuid IN $rebound_retention_sources
                  AND __RETENTION_SOURCE_TENANT__
                DELETE r
                RETURN count(r) AS retention_sources_removed
            }
            // Subtractive lineage: drop ONLY this absorption, keeping any later ones.
            //
            // merge_audit is matched on the absorbed_uuid FIELD, not a bare-substring CONTAINS of the
            // uuid. An audit entry embeds the absorbed node's relationships, each with a peer_uuid --
            // so when two RELATED nodes are absorbed into the same survivor, one entry's JSON contains
            // the OTHER's uuid. A substring match would strip that sibling entry too, silently
            // corrupting its recoverability record. $absorbed_audit_marker is the serialized field
            // (`"absorbed_uuid": "<uuid>"`), which is unique to this absorption's own entry.
            SET s.merged_from = [x IN coalesce(s.merged_from, []) WHERE x <> $absorbed],
                s.merge_audit = [x IN coalesce(s.merge_audit, [])
                                 WHERE NOT x CONTAINS $absorbed_audit_marker]
            RETURN out_restored, in_restored, bridges_removed, mentions_removed,
                   retention_sources_removed
            """.replace(
                "__RETENTION_SOURCE_TENANT__", same_tenant_cypher("source", "s")
            ),
            params={
                "survivor": survivor_uuid,
                "absorbed": absorbed_uuid,
                # Mirrors merge_entity's json.dumps(default separators ": ") for the top-level field.
                "absorbed_audit_marker": f'"absorbed_uuid": "{absorbed_uuid}"',
                "absorbed_labels": absorbed_labels,
                "absorbed_properties": absorbed_properties,
                "out_rels": out_rels,
                "in_rels": in_rels,
                "survivor_properties": survivor_properties,
                "rebound_episodes": rebound_episodes,
                "rebound_retention_sources": rebound_retention_sources or [],
                "operation_id": operation_id,
            },
            timeout_s=SAGA_MUTATION_TIMEOUT_S,  # bounded for ownership ageing (CF-211)
        )
        if not rows:
            return {"restored": 0}
        row = dict(rows[0])
        row["restored"] = 1
        return row

    def peers_exist(self, uuids: list[str]) -> set[str]:
        """Which of these uuids currently exist (any label). Used to detect a snapshot peer that has
        since been deleted -- unmerge must REPORT that, never fabricate the peer."""
        if not uuids:
            return set()
        rows = self._neo4j.execute(
            "MATCH (n) WHERE n.uuid IN $uuids RETURN collect(DISTINCT n.uuid) AS found",
            params={"uuids": list(uuids)},
        )
        return {str(u) for u in (rows[0].get("found") or [])} if rows else set()
