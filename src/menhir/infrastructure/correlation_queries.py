"""Neo4j queries for semantic correlation: RELATES_TO edges and entity merges.

M8 Step 8 — Correlation detection infrastructure. When two entities have high
cosine similarity but are not genuine contradictions, they should be linked
(0.7–0.85) or merged (>0.95) instead of entering the conflict queue.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.domain import merge_delta as md
from menhir.infrastructure.cypher import non_derived_view_cypher
from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S

logger = logging.getLogger(__name__)

def _unchanged_since_phase_one(node: str, prop: str, param: str) -> str:
    """Null-safe `node.prop` is still exactly what Phase 1 read -- the Phase 2 race guard.

    A bare `n.p = $expected` can never hold when the property is absent: Cypher evaluates
    `null = null` to null, so the row drops and a node with no stored provenance would be permanently
    unmergeable. Coalescing both sides to a sentinel fixes that but BLURS the comparison -- absent and
    sentinel-valued become indistinguishable, so a concurrent writer setting the property TO the
    sentinel, or clearing it, slips past. Coercing malformed values to a sentinel is worse still: the
    Cypher side sees the raw value while the parameter says sentinel, so a node whose confidence is,
    say, a string could never merge again.

    This idiom keeps the comparison EXACT on every value the graph can hold -- numeric, string, list,
    or absent -- and treats "both absent" as unchanged without inventing a value for either side.
    """
    return (
        f"coalesce({node}.{prop} = ${param}, "
        f"{node}.{prop} IS NULL AND ${param} IS NULL)"
    )


#: Every provenance value Phase 1 reads, as (node alias, property, parameter). Phase 2 guards each
#: one: a value that is read but not guarded is precisely how a concurrent writer's work gets
#: overwritten by a derivation computed from a stale snapshot.
#:
#: `absorbed.corroboration` is not a derivation input -- the merged count is recomputed from the
#: merged contributor list -- but it IS captured into the audit entry, so a concurrent change would
#: leave the durable snapshot describing a state the node never held, and any restore driven off it
#: would resurrect the wrong count.
GUARDED_PROVENANCE: tuple[tuple[str, str, str], ...] = (
    ("survivor", "source", "expected_survivor_source"),
    ("survivor", "sources", "expected_survivor_sources"),
    ("survivor", "source_confidence", "expected_survivor_confidence"),
    ("absorbed", "source", "expected_absorbed_source"),
    ("absorbed", "sources", "expected_absorbed_sources"),
    ("absorbed", "source_confidence", "expected_absorbed_confidence"),
    ("absorbed", "corroboration", "expected_absorbed_corroboration"),
)

#: Built from the table rather than written out, so a new guarded value cannot be added to one and
#: forgotten in the other. Concatenated into the Phase 2 query: that literal is full of Cypher map
#: braces, so making it an f-string would mean escaping every one of them.
_PROVENANCE_GUARD_CYPHER = "\n".join(
    f"              AND {_unchanged_since_phase_one(node, prop, param)}"
    for node, prop, param in GUARDED_PROVENANCE
)


class CorrelationRepository:
    """Neo4j operations for semantic correlation between entities."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    # ------------------------------------------------------------------
    # RELATES_TO edge creation
    # ------------------------------------------------------------------

    def create_related_to_edge(
        self,
        source_uuid: str,
        target_uuid: str,
        *,
        similarity: float,
        source: str = "correlation-detected",
    ) -> bool:
        """Create a RELATES_TO edge between two entities if none exists.

        Idempotent — MERGE avoids duplicate edges.  Returns True if an edge
        was created (first time); False if it already existed.
        """
        rows = self._neo4j.execute(
            """
            MATCH (a:Entity {uuid: $source_uuid})
            MATCH (b:Entity {uuid: $target_uuid})
            MERGE (a)-[r:RELATES_TO]->(b)
            ON CREATE SET
                r.type = 'correlation',
                r.similarity = $similarity,
                r.source = $source,
                r.scope = 'PERSISTENT',
                r.weight = $similarity,
                r.created_at = datetime(),
                r.last_traversed = datetime()
            ON MATCH SET
                r.similarity = CASE
                    WHEN $similarity > r.similarity THEN $similarity
                    ELSE r.similarity
                END,
                r.weight = CASE
                    WHEN $similarity > r.weight THEN $similarity
                    ELSE r.weight
                END,
                r.last_traversed = datetime()
            RETURN r.type AS edge_type
            """,
            params={
                "source_uuid": source_uuid,
                "target_uuid": target_uuid,
                "similarity": similarity,
                "source": source,
            },
        )
        return bool(rows)

    # ------------------------------------------------------------------
    # Entity merge (near-duplicate absorption)
    # ------------------------------------------------------------------

    #: The ONE merge-ineligibility predicate. Interpolated by BOTH the eligibility gate and the
    #: standalone veto, on the same `n` binding, so the two cannot state different rules.
    #:
    #: The previous comment claimed the two "cannot drift apart" while only the path-shaped
    #: regex was actually shared -- the veto hand-wrote its own copy of the rest and had already
    #: lost `is_view` (CF-149). Neither copy ever carried `is_quantstate`, so a legacy counter
    #: register was merge-eligible at the mutation boundary (CF-159).
    #:
    #: The derived-node clause is the same triple the typed-scalar binding queries use in
    #: `episode_lifecycle` (`is_view` / `is_quantstate` / `view_kind`), inverted: a node is
    #: INELIGIBLE if any of them says it is derived. Counters written today carry `is_view` too,
    #: so `is_quantstate` covers pre-View registers specifically.
    _INELIGIBLE_ROLE_PREDICATE = (
        f"(n.structure_role IS NOT NULL) OR NOT ({non_derived_view_cypher('n')}) OR "
        r"(n.name =~ '(?i).*([\\/]|\\.(py|md|txt|json|ya?ml|ps1|sh|java|ts|tsx|js|sql|env|toml|"
        r"cfg|ini|gradle|html|css|png|jpg|svg))(\b|$).*')"
    )

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
            )

        return me.evaluate(_signals(survivor_uuid), _signals(absorbed_uuid))

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
            RETURN out_restored, in_restored, bridges_removed, mentions_removed
            """,
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
                "operation_id": operation_id,
            },
            timeout_s=SAGA_MUTATION_TIMEOUT_S,  # bounded for ownership ageing (CF-211)
        )
        if not rows:
            return {"restored": 0}
        row = dict(rows[0])
        row["restored"] = 1
        return row

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

    def merge_entity(
        self,
        survivor_uuid: str,
        absorbed_uuid: str,
        *,
        similarity: float,
        operation_id: str | None = None,
    ) -> dict[str, int]:
        """Merge *absorbed_uuid* into *survivor_uuid*.

        ``operation_id`` stamps the survivor with ``last_merge_op_id`` so a crash-replay can
        recognize its OWN completed work instead of re-running or forking (plan Phase 4). It is an
        idempotency aid, never a substitute for full after-state verification.

        Strategy:
        1. Keep the richer summary/content (>20% longer = substantially more context).
        2. Union the source provenance (concatenate distinct values).
        3. Bump confidence (multiple independent observations = stronger signal).
        4. Bridge absorbed node's edges to the survivor.
        5. Part 3: Snapshot the absorbed node before deletion (audit trail for unmerge).
        6. Delete the absorbed node.

        Part 3: Snapshot preserves uuid, all properties, and relationships so
        the absorbed node is fully recoverable from the merge_audit trail.

        Returns dict with keys: merged, edges_bridged, deleted.
        """
        import json
        from datetime import datetime, timezone

        from menhir.domain import merge_eligibility as me

        # Phase 0 (fail-closed precondition, plan invariant 7): recheck eligibility on CURRENT graph
        # state immediately before any mutation. Earlier candidate discovery is not authorization --
        # a stale pair, a direct repository caller, or a node whose freshness/scope/flag changed
        # after discovery must abstain HERE, not merge. A structured abstention carries the reason
        # code; it never partially mutates.
        eligibility = self.evaluate_merge_eligibility(survivor_uuid, absorbed_uuid)
        if not eligibility.allowed:
            logger.info(
                "merge_entity ABSTAIN %s <- %s: %s %s",
                survivor_uuid, absorbed_uuid, eligibility.reason_code, eligibility.diagnostics,
            )
            return {
                "merged": 0, "edges_bridged": 0, "episodes_rebound": 0, "deleted": 0,
                "reason": eligibility.reason_code,
            }

        # Phase 1 (read-only): snapshot the absorbed node's properties and relationships for the
        # audit trail. This Neo4j has no JSON function (no APOC) and a node property cannot hold a
        # nested map or a list of maps, so the audit entry is serialized to a JSON string in Python
        # and stored as one element of the survivor.merge_audit string list. Returning maps to the
        # driver here is fine — the map/list restriction only applies to *stored* property values.
        snap_rows = self._neo4j.execute(
            """
            MATCH (absorbed:Entity {uuid: $absorbed_uuid})
            OPTIONAL MATCH (absorbed)-[r]-(peer:Entity)
            WITH absorbed, collect(
                CASE WHEN r IS NULL THEN NULL ELSE {
                    peer_uuid: CASE WHEN id(absorbed) = id(startNode(r)) THEN endNode(r).uuid ELSE startNode(r).uuid END,
                    rel_type: type(r),
                    weight: r.weight,
                    direction: CASE WHEN id(absorbed) = id(startNode(r)) THEN 'out' ELSE 'in' END
                } END
            ) AS rels
            // Episode provenance: the absorbed node's MENTIONS sources. Captured here so the
            // audit trail can reconstruct it -- the Entity-only peer collection above misses
            // it, and DETACH DELETE below would otherwise destroy it irrecoverably.
            OPTIONAL MATCH (ep:Episodic)-[:MENTIONS]->(absorbed)
            WITH absorbed, rels, collect(DISTINCT ep.uuid) AS episode_uuids
            // The survivor's PRE-MERGE episode set. Needed to invert the rebind exactly:
            // the merge below re-points the absorbed node's MENTIONS onto the survivor, and
            // an unmerge must remove only the edges the merge ADDED. Without this, unmerge
            // either leaves the survivor holding fabricated provenance (an episode appearing
            // to mention both identities -- which would then trip the co-mention veto), or,
            // if it removed them all, would strip provenance the survivor legitimately had.
            OPTIONAL MATCH (sep:Episodic)-[:MENTIONS]->(:Entity {uuid: $survivor_uuid})
            WITH absorbed, rels, episode_uuids, collect(DISTINCT sep.uuid) AS survivor_episode_uuids
            // The survivor's own provenance. Needed here because the merged contributor list is
            // computed in PYTHON (the tier table is the single authority and cannot be expressed in
            // Cypher), so Phase 2 receives finished values rather than deriving them itself.
            //
            // EVERY provenance input the derivation reads is returned for BOTH nodes -- source,
            // sources AND source_confidence. Reading only `source` made the derivation lossy in two
            // ways: an already-merged absorbed node's non-primary contributors were dropped (its
            // `source` holds only the lowest-tier one), and the guard below could not detect a
            // concurrent change to the values it did not read.
            MATCH (survivor:Entity {uuid: $survivor_uuid})
            RETURN survivor.source AS survivor_source, survivor.sources AS survivor_sources,
                   survivor.source_confidence AS survivor_source_confidence,
                   absorbed.uuid AS uuid, absorbed.name AS name, absorbed.summary AS summary,
                   absorbed.content AS content, absorbed.source AS source,
                   absorbed.source_confidence AS source_confidence,
                   absorbed.sources AS sources, absorbed.corroboration AS corroboration,
                   absorbed.type AS type,
                   absorbed.scope AS scope, toString(absorbed.created_at) AS created_at,
                   absorbed.namespace AS namespace,
                   [x IN rels WHERE x IS NOT NULL] AS relationships,
                   [x IN episode_uuids WHERE x IS NOT NULL] AS mentioned_by_episodes,
                   [x IN survivor_episode_uuids WHERE x IS NOT NULL] AS survivor_episodes_before
            """,
            params={"absorbed_uuid": absorbed_uuid, "survivor_uuid": survivor_uuid},
        )
        if not snap_rows:
            return {"merged": 0, "edges_bridged": 0, "episodes_rebound": 0, "deleted": 0}
        snap = dict(snap_rows[0])
        # `sources` and `corroboration` are part of the absorbed node's provenance, so they belong in
        # the audit entry too. Omitting them would make the graph/sidecar snapshot lossy in exactly
        # the direction that matters: a degraded restore driven off this entry would resurrect the
        # node with its contributor list erased and its corroboration reset.
        snapshot = {
            k: snap.get(k)
            for k in (
                "uuid", "name", "summary", "content", "source",
                "source_confidence", "sources", "corroboration",
                "type", "scope", "created_at", "namespace",
            )
        }
        audit_entry = json.dumps(
            {
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "absorbed_uuid": absorbed_uuid,
                "survivor_uuid": survivor_uuid,
                "similarity": similarity,
                "properties": snapshot,
                "relationships": [r for r in (snap.get("relationships") or []) if r],
                # Episode provenance of the absorbed node. Needed for unmerge (the
                # MENTIONS edges are re-pointed onto the survivor below, so unmerge must
                # know which ones came from the absorbed node) and for auditing merges
                # against the co-mention veto after the fact.
                "mentioned_by_episodes": list(snap.get("mentioned_by_episodes") or []),
                # Survivor's episode set BEFORE this merge. Unmerge subtracts this from
                # mentioned_by_episodes to remove exactly the edges the merge added.
                "survivor_episodes_before": list(snap.get("survivor_episodes_before") or []),
            },
            default=str,
        )

        # Phase 1.5 (derive, in Python): the merged contributor list and everything read off it.
        # Done here, not in Cypher, because `source_confidence_for` is the single authority for the
        # source -> tier mapping and cannot be expressed as a Cypher map lookup (it has substring
        # rules). Deriving here keeps ONE implementation of the ladder; the alternative -- a tier
        # table duplicated into Cypher -- is exactly how structure_queries once drifted from it.
        survivor_provenance = {
            "source": snap.get("survivor_source"),
            "sources": snap.get("survivor_sources"),
            "source_confidence": snap.get("survivor_source_confidence"),
        }
        # `derive_merged_provenance` is the SAME function merge_delta replays for the unmerge guard.
        # Calling it here rather than re-assembling the four values keeps the write and its inverse
        # provably identical; an exact unmerge is only possible while that holds.
        provenance = md.derive_merged_provenance(survivor_provenance, snapshot)

        # Phase 2 (mutate): pick the richer text, write derived provenance, append the
        # audit entry, bridge the absorbed node's edges onto the survivor, then delete it. FOREACH
        # bridges over the (possibly empty) neighbor list without gating the row, so the merge always
        # reaches DETACH DELETE and RETURN even when the absorbed node had no bridgeable neighbors.
        rows = self._neo4j.execute(
            """
            MATCH (survivor:Entity {uuid: $survivor_uuid})
            MATCH (absorbed:Entity {uuid: $absorbed_uuid})
            // Defense in depth (plan Phase 3): repeat the MUTABLE material predicates inside the
            // mutation. The preflight above already gated the pair, but another writer could change
            // freshness/scope/flag/conflict/namespace in the window before this statement runs.
            // Fail closed: if any no longer holds, this MATCH yields no row and the merge abstains
            // (see the empty-rows guard below). Structural role and existence cannot change mid-merge,
            // so they are not re-checked here.
            WHERE """
            + me.mutable_eligibility_cypher()
            + """
              // Provenance was read in Phase 1 and derived in Python; if another writer changed ANY
              // of it in the window, the derived values are stale and writing them would silently
              // discard the concurrent writer's work. Guarding `source` alone is not enough: a
              // concurrent merge that adds a HIGHER-tier contributor leaves the lowest-tier primary
              // `source` untouched, so that check passes while `sources` is overwritten and the new
              // contributor disappears. See GUARDED_PROVENANCE for the full set and why each is in
              // it; the comparison is exact and null-safe, never collapsed onto a sentinel.
"""
            + _PROVENANCE_GUARD_CYPHER
            + """
            // Pick richer text — prefer the longer content when >20% difference
            WITH survivor, absorbed,
                CASE
                    WHEN size(coalesce(absorbed.summary, absorbed.content, '')) >
                         size(coalesce(survivor.summary, survivor.content, '')) * 1.2
                    THEN absorbed
                    ELSE survivor
                END AS richer
            // Update survivor properties + append the Python-serialized audit entry
            SET survivor.summary = coalesce(richer.summary, richer.content, survivor.summary),
                survivor.content = CASE
                    WHEN size(coalesce(absorbed.content, '')) >
                         size(coalesce(survivor.content, '')) * 1.2
                    THEN absorbed.content
                    ELSE survivor.content
                END,
                // Provenance is DERIVED, not accumulated. These four values are computed in Python
                // above via the domain tier table, so this statement performs NO arithmetic and no
                // string surgery -- the previous version added 0.1 per merge and comma-appended the
                // source, which made trust a function of how many duplicates a node absorbed (four
                // absorptions took a claude-code entity from 0.7 to 1.0, the user tier). Keeping the
                // tier logic in one Python function is also what stops this clause from drifting
                // away from `source_confidence_for` the way the project-scan mapping once did.
                survivor.sources = $sources,
                survivor.corroboration = $corroboration,
                survivor.source_confidence = $authority,
                survivor.source = $primary_source,
                survivor.last_accessed = datetime(),
                // Carry the absorbed node's OWN lineage forward. If the absorbed node was
                // itself a survivor of an earlier merge, its merged_from/merge_audit are the
                // only record of those earlier absorptions -- DETACH DELETE below would
                // destroy them, making the chain B -> A -> C lose B entirely (unrecoverable,
                // since B's snapshot lived in A.merge_audit).
                survivor.merged_from = coalesce(survivor.merged_from, []) + [$absorbed_uuid]
                                       + coalesce(absorbed.merged_from, []),
                survivor.merge_audit = coalesce(survivor.merge_audit, []) + [$audit_entry]
                                       + coalesce(absorbed.merge_audit, []),
                // Idempotency marker (plan Phase 4): lets a crash-replay recognize its OWN completed
                // work. coalesce keeps any prior value when this is an unjournaled direct call.
                survivor.last_merge_op_id = coalesce($op_id, survivor.last_merge_op_id)
            // Bridge edges from absorbed to survivor (skip ANCHORED_TO and structural)
            WITH survivor, absorbed
            OPTIONAL MATCH (absorbed)-[r]-(neighbor:Entity)
            WHERE NOT type(r) = 'ANCHORED_TO'
            AND neighbor.structure_role IS NULL
            AND neighbor.uuid <> survivor.uuid
            WITH survivor, absorbed, collect(DISTINCT neighbor) AS neighbors
            FOREACH (n IN neighbors |
                MERGE (survivor)-[bridge:RELATES_TO]->(n)
                ON CREATE SET
                    bridge.type = 'bridged',
                    bridge.bridged_from = $absorbed_uuid,
                    bridge.weight = 1.0,
                    bridge.source = 'system-derived',
                    bridge.scope = 'PERSISTENT',
                    bridge.created_at = datetime(),
                    bridge.last_traversed = datetime()
            )
            WITH survivor, absorbed, size(neighbors) AS edges_bridged
            // Preserve episode provenance. The Entity-only bridge above does not touch
            // (:Episodic)-[:MENTIONS]->(absorbed), so DETACH DELETE used to destroy the
            // absorbed node's source episodes outright. Re-point them onto the survivor:
            // the survivor now carries the union of both nodes' provenance, which is also
            // what the co-mention veto reads.
            OPTIONAL MATCH (ep:Episodic)-[:MENTIONS]->(absorbed)
            WITH survivor, absorbed, edges_bridged, collect(DISTINCT ep) AS episodes
            FOREACH (e IN episodes | MERGE (e)-[:MENTIONS]->(survivor))
            WITH survivor, absorbed, edges_bridged, size(episodes) AS episodes_rebound
            // Remove the absorbed node
            DETACH DELETE absorbed
            RETURN edges_bridged, episodes_rebound, 1 AS deleted,
                   coalesce(survivor.namespace, survivor.group_id, 'default') AS merge_namespace
            """,
            params={
                # CF-47: the mutable predicates in the WHERE above are emitted by
                # `domain.merge_eligibility`, and these are the values they bind. They come from
                # the same constants the preflight decides on, so the two can no longer disagree
                # by being edited apart.
                **me.MUTABLE_PREDICATE_PARAMS,
                "survivor_uuid": survivor_uuid,
                "absorbed_uuid": absorbed_uuid,
                "audit_entry": audit_entry,
                "op_id": operation_id,
                "sources": provenance["sources"],
                "corroboration": provenance["corroboration"],
                "authority": provenance["source_confidence"],
                "primary_source": provenance["source"],
                # Fail closed if EITHER node's provenance changed between Phase 1 and Phase 2: the
                # values above were derived from what Phase 1 read, so writing them over a
                # concurrently-changed input would silently discard the other writer's work.
                # Mirrors the mutable-predicate re-checks already in the WHERE.
                #
                # Passed RAW, exactly as Phase 1 read them -- no `or ''`, no list coercion, no
                # sentinel for a malformed value. Normalizing here would compare the tidied parameter
                # against the untidied graph: a node whose `sources` is absent is NOT the same as one
                # holding `[]` (the derivation reads them differently), and a node whose confidence is
                # malformed would never again equal a sentinel the graph does not contain, making it
                # permanently unmergeable. `_unchanged_since_phase_one` handles null on both sides.
                "expected_survivor_source": snap.get("survivor_source"),
                "expected_survivor_sources": snap.get("survivor_sources"),
                "expected_survivor_confidence": snap.get("survivor_source_confidence"),
                "expected_absorbed_source": snap.get("source"),
                "expected_absorbed_sources": snap.get("sources"),
                "expected_absorbed_confidence": snap.get("source_confidence"),
                "expected_absorbed_corroboration": snap.get("corroboration"),
            },
            # Bounded so an ownership claim on this saga can be aged out safely (CF-211). The
            # read-only snapshot above is deliberately left unbounded: only the MUTATION needs a
            # provable upper bound, and capping reads would be a behaviour change recovery has no
            # reason to require.
            timeout_s=SAGA_MUTATION_TIMEOUT_S,
        )
        if not rows:
            # The preflight passed but the mutation matched nothing: a concurrent writer changed a
            # material predicate (freshness/scope/flag/conflict/namespace) or one of the provenance
            # inputs the Python derivation read in Phase 1, and the in-mutation WHERE failed closed.
            # No partial merge occurred.
            logger.info(
                "merge_entity ABSTAIN %s <- %s: eligibility or provenance changed between "
                "preflight and mutation",
                survivor_uuid, absorbed_uuid,
            )
            return {
                "merged": 0, "edges_bridged": 0, "episodes_rebound": 0, "deleted": 0,
                "reason": "ELIGIBILITY_CHANGED_AT_MUTATION",
            }

        # Durable audit. survivor.merge_audit (written above) is a convenience
        # denormalization that dies with the node -- decay, orphan cleanup, and user
        # deletes all DETACH DELETE the survivor, destroying the only snapshot of every
        # node it ever absorbed. This sidecar row outlives the graph, so an absorbed node
        # stays recoverable regardless of what later happens to its survivor.
        # Best-effort: telemetry must never break the merge.
        from menhir.infrastructure.telemetry import record_merge

        row = rows[0]
        merge_namespace = str(row.get("merge_namespace") or "default")
        record_merge(
            survivor_uuid=survivor_uuid,
            absorbed_uuid=absorbed_uuid,
            similarity=similarity,
            snapshot_json=audit_entry,
            survivor_namespace=merge_namespace,
            absorbed_namespace=merge_namespace,
        )

        return {
            "merged": 1,
            "edges_bridged": int(row.get("edges_bridged", 0) or 0),
            "episodes_rebound": int(row.get("episodes_rebound", 0) or 0),
            "deleted": int(row.get("deleted", 0) or 0),
        }

    # ------------------------------------------------------------------
    # Correlation lookup — check if a RELATES_TO edge already exists
    # ------------------------------------------------------------------

    def correlation_exists(self, uuid_a: str, uuid_b: str) -> bool:
        """Check whether a RELATES_TO edge exists between two entities."""
        rows = self._neo4j.execute(
            """
            MATCH (a:Entity {uuid: $a})-[r:RELATES_TO]-(b:Entity {uuid: $b})
            WHERE r.type = 'correlation'
            RETURN count(r) AS cnt
            """,
            params={"a": uuid_a, "b": uuid_b},
        )
        return bool(rows and int(rows[0].get("cnt", 0)) > 0)

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
