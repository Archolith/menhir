"""Consolidation, decay, session management, and conflict resolution queries.

Extracted from MemoryGraphAdapter. The adapter delegates all Entity decay,
session promotion, and conflict governance operations here.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params
from menhir.domain.recall import adjacency_edge_pattern
from menhir.domain.recall_visibility import memory_retired_cypher
from menhir.domain.retention import (
    canonical_tenant_cypher,
    destructive_retention_allowed_cypher,
    same_tenant_cypher,
)
from menhir.infrastructure.cypher import (
    Cypher, memory_recency_cypher, memory_timestamp_cypher, non_derived_view_cypher,
)
from menhir.infrastructure.erasure_fence import fence_key_cypher
from menhir.infrastructure.graph_text_inventory import ABSORBED_BLOCK_SEPARATOR
from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S, Neo4jRepository
from menhir.infrastructure.publish_fence import CopyFenced, execute_copy_fenced

logger = logging.getLogger(__name__)

# Bounded per run; least-recent selection rotates past records skipped by policy.
_DECAY_CANDIDATE_LIMIT = 500


def automatic_lifecycle_protection_cypher(variable: str = "n") -> str:
    """Exclude derived Views and evidence retained by a live FACT View.

    FACT Views are ``:Entity`` nodes; their evidence-to-View ``MENTIONS`` relationship is the
    authoritative automatic-retention signal.  Explicit erasure repositories intentionally do not
    use this predicate: authorized erasure retires dependent Views before deleting their evidence.
    """
    non_derived = non_derived_view_cypher(variable)
    return (
        f"{non_derived} "
        "AND NOT EXISTS { "
        f"MATCH ({variable})-[:MENTIONS]->(retaining_view:Entity) "
        "WHERE coalesce(retaining_view.is_view, false) "
        "AND coalesce(retaining_view.view_current, retaining_view.qs_current, true) "
        "AND NOT coalesce(retaining_view.retired, false) }"
    )


def harmful_automatic_mutation_allowed_cypher(variable: str = "n") -> str:
    """Guard destructive automation without changing beneficial lifecycle behavior."""

    return (
        f"{automatic_lifecycle_protection_cypher(variable)} "
        f"AND {destructive_retention_allowed_cypher(variable)} "
        f"AND NOT {memory_retired_cypher(variable)}"
    )


#: A removed node with more eligible neighbours than this is not bridged: k neighbours would add
#: k(k-1)/2 edges, so a hub would otherwise flood the graph with weak links.
MAX_BRIDGE_NEIGHBOURS = 20

#: Write-lock token set and removed in one statement by the conflict writers (see graph_text_inventory).
CONFLICT_RESOLVE_LOCK = "_conflict_resolve_lock"


class ConflictResolveRefused(ValueError):
    """A resolve that was refused as a whole: nothing was changed. ``reason`` names the check."""

    def __init__(self, reason: str, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(f"Conflict resolve refused ({reason}){suffix}. Nothing was changed.")
        self.reason = reason


def _bridge_neighbour_filter_cypher(
    removed: str, neighbor: str, excluded: str, extra: tuple[str, ...] = ()
) -> str:
    """WHERE body selecting the neighbours a removed node may bridge.

    Neighbours are reached over ``adjacency_edge_pattern()`` only (the edges recall treats as
    adjacency); ANCHORED_TO, SUPERSEDES, NEXT_EPISODE and evidence edges are not adjacency, so a
    cross-domain anchor or a supersession chain never becomes a semantic bridge. ``excluded`` is a
    Cypher list expression of the nodes being removed. A neighbour waiting for rederive (an erase
    scrub) is never given a new link.
    """
    clauses = [
        f"{neighbor}.structure_role IS NULL",
        non_derived_view_cypher(neighbor),
        f"coalesce({neighbor}.freshness, '') <> 'GONE'",
        f"NOT coalesce({neighbor}.rederive_pending, false)",
        f"NOT {memory_retired_cypher(neighbor)}",
        f"NOT {neighbor}.uuid IN {excluded}",
        # Bridges stay in the removed node's tenant: an existing cross-tenant edge on it must not
        # become a new link between two other tenants' nodes.
        same_tenant_cypher(removed, neighbor),
        *extra,
    ]
    return "\n              AND ".join(clauses)


def bridge_cypher(
    *,
    excluded: str,
    removed: str = "n",
    neighbor_filters: tuple[str, ...] = (),
    pair_filters: tuple[str, ...] = (),
) -> str:
    """Cypher block that bridges the neighbours of the bound node ``removed``.

    Shared by ``bridge_and_delete`` and ``bridge_edges_for_nodes``. Yields one row with
    ``edges_bridged`` (edges THIS block created), ``bridge_uuids`` (their uuids) and
    ``bridge_skipped`` (true when the node had more than ``MAX_BRIDGE_NEIGHBOURS`` eligible
    neighbours, so nothing was bridged). Neighbours are collected only up to the cap plus one.

    Each neighbour is write-locked (in uuid order; a deadlock with another locker is detected by
    Neo4j and retried) and re-checked
    before the pair MERGE: under read-committed a concurrent bridger of the same pair would
    otherwise create it twice. A pair already joined by any adjacency edge gets no new edge, and
    the MERGE is undirected between two bound nodes, so at most one RELATES_TO per unordered pair.
    A new edge carries ``created_at = datetime.statement()``, which is constant for the statement
    and cannot equal an edge made before it, and ``bridged_from``; counting both counts only edges
    created here.
    """
    adjacency = adjacency_edge_pattern()
    where = _bridge_neighbour_filter_cypher(removed, "neighbor", excluded, neighbor_filters)
    pair_where = "".join(f"\n                  AND {f}" for f in pair_filters)
    return f"""
            CALL {{
                WITH {removed}
                CALL {{
                    WITH {removed}
                    OPTIONAL MATCH ({removed})-[:{adjacency}]-(neighbor:Entity)
                    WHERE {where}
                    WITH DISTINCT neighbor LIMIT {MAX_BRIDGE_NEIGHBOURS + 1}
                    RETURN collect(neighbor) AS neighbors
                }}
                WITH {removed}.uuid AS removed_uuid, neighbors,
                     size(neighbors) > {MAX_BRIDGE_NEIGHBOURS} AS hub
                CALL {{
                    WITH removed_uuid, neighbors, hub
                    WITH removed_uuid, CASE WHEN hub THEN [] ELSE neighbors END AS pool
                    UNWIND pool AS locked
                    WITH removed_uuid, locked ORDER BY locked.uuid
                    SET locked._bridge_lock = true
                    REMOVE locked._bridge_lock
                    WITH removed_uuid, locked
                    WHERE coalesce(locked.freshness, '') <> 'GONE'
                      AND NOT coalesce(locked.rederive_pending, false)
                      AND NOT {memory_retired_cypher("locked")}
                    WITH removed_uuid, collect(locked) AS pool
                    UNWIND pool AS a
                    UNWIND pool AS b
                    WITH removed_uuid, a, b
                    WHERE a.uuid < b.uuid{pair_where}
                      AND NOT EXISTS {{ (a)-[:{adjacency}]-(b) }}
                    MERGE (a)-[r:RELATES_TO]-(b)
                    ON CREATE SET r.uuid = randomUUID(),
                                  r.type = 'bridged',
                                  r.bridged_from = removed_uuid,
                                  r.weight = 1.0,
                                  r.source = 'system-derived',
                                  r.scope = 'PERSISTENT',
                                  r.created_at = datetime.statement(),
                                  r.last_traversed = datetime()
                    RETURN count(CASE WHEN r.created_at = datetime.statement()
                                       AND r.bridged_from = removed_uuid THEN 1 END) AS edges_bridged,
                           collect(CASE WHEN r.created_at = datetime.statement()
                                         AND r.bridged_from = removed_uuid THEN r.uuid END) AS bridge_uuids
                }}
                RETURN edges_bridged, bridge_uuids, hub AS bridge_skipped
            }}"""


def summary_sha256(text: str) -> str:
    """Marker binding a stored ``original_content`` to the summary of the compression that wrote it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_overlap_ratio(left: str | None, right: str | None) -> float:
    """Compute simple Jaccard overlap ratio over lowercase token sets."""

    left_tokens = set(re.findall(r"[A-Za-z0-9_]+", (left or "").lower()))
    right_tokens = set(re.findall(r"[A-Za-z0-9_]+", (right or "").lower()))
    if not left_tokens and not right_tokens:
        return 1.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    intersection = left_tokens & right_tokens
    return len(intersection) / len(union)


@dataclass
class ConsolidationRepository:
    """Encapsulates Entity decay, session promotion, and conflict resolution."""

    neo4j: Neo4jRepository

    # -------------------------------------------------------------------------
    # Graph maintenance
    # -------------------------------------------------------------------------

    def sync_edge_counts(self) -> int:
        """Bulk recount entity edge_count values from the current graph.

        Excludes ANCHORED_TO edges so structural cross-links don't inflate
        decay-protection thresholds.

        Scaling note: this touches every Entity node in a single transaction.
        Fine for personal-scale graphs (hundreds to low thousands of nodes).
        If the graph reaches tens of thousands of densely-connected entities,
        consider migrating to CALL {} IN TRANSACTIONS for batched writes.
        """

        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            OPTIONAL MATCH (n)-[r]-()
            WHERE NOT type(r) = 'ANCHORED_TO'
            WITH n, count(DISTINCT r) AS edge_count
            SET n.edge_count = edge_count
            RETURN count(n) AS synced
            """,
        )
        return int(rows[0].get("synced", 0)) if rows else 0

    #: DIRECTED on purpose, and it is a bug fix rather than a style choice.
    #:
    #: This match was `()-[r]-()`, which yields every relationship TWICE -- once per direction --
    #: so the SET ran twice and each call raised the weight by 0.2 against a documented "+0.1 per
    #: traversal", reaching the 5.0 cap in half the traversals the ratchet was designed for.
    #: Confirmed by execution against Neo4j 5, not by reading: weight 1.0 -> 1.2 and
    #: `count(r)` -> 2. `test_edge_weight_cap_contract` could not catch it because it greps this
    #: source for the string "0.1" rather than running the query. `()-[r]->()` matches each
    #: relationship exactly once, so it fixes the rate and halves the scan.
    #:
    #: TYPED, with `adjacency_edge_pattern()`: the same list `fetch_adjacency_pairs` emits for
    #: `edge_index`, so recall reinforces exactly the edges it ranked. See CF-247.
    def increment_edge_weight(self, edge_uuid: str) -> bool:
        """Increment traversal weight for an edge while capping at the v1 max."""

        rows = self.neo4j.execute(
            f"""
            MATCH ()-[r:{adjacency_edge_pattern()}]->()
            WHERE r.uuid = $edge_uuid
            SET r.weight = CASE
                    WHEN coalesce(toFloat(r.weight), 1.0) + 0.1 < 5.0
                    THEN coalesce(toFloat(r.weight), 1.0) + 0.1
                    ELSE 5.0
                END,
                r.last_traversed = datetime()
            RETURN count(r) AS edges_updated
            """,
            params={"edge_uuid": edge_uuid},
        )
        return bool(rows and int(rows[0].get("edges_updated", 0)) > 0)

    def increment_edge_weights(self, edge_uuids: list[str]) -> int:
        """Ratchet many edges in ONE round trip. Returns the number of edges updated.

        CF-75's recall path called `increment_edge_weight` once per traversed edge, serially, and
        each of those calls is an all-relationship scan. The cost was therefore N scans of the
        whole relationship store: 3,150,400 dbHits for 50 edges on a 21k-edge graph, growing with
        BOTH the result size and the graph. One `IN` collapses that to a single scan (42,100),
        because the scan is per-query, not per-uuid.
        """
        if not edge_uuids:
            return 0
        rows = self.neo4j.execute(
            f"""
            MATCH ()-[r:{adjacency_edge_pattern()}]->()
            WHERE r.uuid IN $edge_uuids
            SET r.weight = CASE
                    WHEN coalesce(toFloat(r.weight), 1.0) + 0.1 < 5.0
                    THEN coalesce(toFloat(r.weight), 1.0) + 0.1
                    ELSE 5.0
                END,
                r.last_traversed = datetime()
            RETURN count(r) AS edges_updated
            """,
            params={"edge_uuids": list(dict.fromkeys(edge_uuids))},
        )
        return int(rows[0].get("edges_updated", 0)) if rows else 0

    def update_edge_facts(self, updates: list[dict[str, str]]) -> int:
        """Bulk-update edge facts and provenance. Returns count updated.

        DIRECTED, for the same reason `increment_edge_weight` above is (CF-75). An anonymous
        undirected match yields every relationship twice -- once per assignment of its two free
        endpoints -- so this returned exactly double the number of edges it had updated. The `SET`
        is idempotent, so no fact was ever corrupted by the second visit; the count was wrong and
        the scan did twice the work it needed to. Confirmed by execution, 2 reported against 1
        real edge, not by reading. CF-250.

        This is the instance the CF-75 fix missed: the hazard was written up 70 lines above, in
        this same file, while its sibling kept the defect. Anchored patterns like `(n)-[r]-(peer)`
        do NOT have this problem -- one endpoint is bound, so each incident relationship is
        yielded once -- which is why every other undirected match in the codebase is fine.
        """
        if not updates:
            return 0
        rows = self.neo4j.execute(
            """
            UNWIND $updates AS update
            MATCH ()-[r]->()
            WHERE r.uuid = update.uuid
            SET r.fact = update.fact, r.fact_source = update.fact_source
            RETURN count(r) AS updated
            """,
            params={"updates": updates},
        )
        return int(rows[0].get("updated", 0)) if rows else 0

    # -------------------------------------------------------------------------
    # Decay / compression
    # -------------------------------------------------------------------------

    def fetch_decay_candidates(
        self,
        freshness: str,
        *,
        min_days_since_accessed: float,
        max_edge_count: int,
        max_sharpness: float | None = None,
        limit: int = _DECAY_CANDIDATE_LIMIT,
    ) -> list[dict[str, object]]:
        """Fetch PERSISTENT decay candidates using current cached graph signals.

        Sharpness filtering:
        - Compress phase (max_sharpness=None): include nodes with null sharpness
          for inline recomputation before deciding.
        - Delete phase (max_sharpness set): exclude null sharpness (protective gate:
          unknown uniqueness never justifies deletion).
        """

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'PERSISTENT'",
                   "n.freshness = $freshness",
                   harmful_automatic_mutation_allowed_cypher("n"),
                   f"{memory_recency_cypher()} < datetime() - duration({{days: $min_days_since_accessed}})",
                   "coalesce(toInteger(n.edge_count), 0) < $max_edge_count",
                   # CF-338: a node an erase scrubbed waits for rederive; never compress it.
                   "NOT coalesce(n.rederive_pending, false)")
            .where_if(max_sharpness is not None, "n.sharpness IS NOT NULL AND toFloat(n.sharpness) < $max_sharpness")
            .with_clause(
                f"n, {memory_timestamp_cypher('n.decay_last_selected_at')} AS selected_at, "
                f"toString({memory_recency_cypher()}) AS recency"
            )
            .return_raw("""n.uuid AS uuid,
       recency AS recency,
       n.type AS type,
       n.name AS name,
       n.content AS content,
       n.summary AS summary,
       n.scope AS scope,
       n.sharpness AS sharpness,
       n.edge_count AS edge_count,
       n.freshness AS freshness,
       n.user_flagged AS user_flagged,
       n.last_accessed AS last_accessed,
       """ + canonical_tenant_cypher("n") + """ AS namespace,
       coalesce(toInteger(n.rehydration_count), 0) AS rehydration_count,
       CASE WHEN n.target_date IS NOT NULL AND date(n.target_date) < date() THEN true ELSE false END AS target_date_passed,
       n.created_at AS created_at,
       """ + fence_key_cypher("n") + """ AS fence_key,
       head(COLLECT { MATCH (f:EvidenceNamespaceFence) WHERE f.namespace_key = """
                + fence_key_cypher("n") + """
                      RETURN coalesce(f.erasure_epoch, 0) }) AS fence_epoch""")
            .order_by(
                f"selected_at IS NOT NULL ASC, selected_at ASC, "
                f"coalesce({memory_timestamp_cypher('n.created_at')}, "
                f"{memory_timestamp_cypher('n.last_accessed')}) ASC, n.uuid"
            )
            .limit()
            .build())
        params: dict[str, object] = {
            "freshness": freshness,
            "min_days_since_accessed": min_days_since_accessed,
            "max_edge_count": max_edge_count,
            "limit": limit,
        }
        if max_sharpness is not None:
            params["max_sharpness"] = max_sharpness
        return self.neo4j.execute(query, params=params)

    def mark_decay_candidates_selected(self, node_uuids: list[str]) -> int:
        """Persist scheduling progress, independently of eligibility or processing success."""
        if not node_uuids:
            return 0
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity) WHERE n.uuid IN $uuids
            SET n.decay_last_selected_at = datetime()
            RETURN count(n) AS marked
            """,
            {"uuids": list(dict.fromkeys(node_uuids))},
        )
        return int(rows[0].get("marked", 0)) if rows else 0

    @staticmethod
    def _updated_unless_fenced(node_uuid: str, fenced_write: Callable[[], Any]) -> bool:
        """Run a G-copy single-node write; False when it matched nothing or was fenced.

        ``fenced_write`` calls ``execute_copy_fenced`` with ``key_from_uuid=node_uuid``: without a
        fence key the node's key is resolved inside the fenced transaction, and without a read
        epoch only the epoch comparison is skipped.
        """
        try:
            rows = fenced_write()
        except CopyFenced as fenced:
            logger.info(
                "Lifecycle write to %s refused by the erasure fence (%s)", node_uuid, fenced.reason
            )
            return False
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def compress_node(
        self,
        node_uuid: str,
        compressed_summary: str,
        *,
        read_content: str | None,
        min_days_since_accessed: float,
        read_summary: str | None = None,
        read_epoch: int | None = None,
        fence_key: str | None = None,
    ) -> bool:
        """Transition a PERSISTENT ACTIVE node to COMPRESSED with a summary.

        G-copy (CF-338): the summary is derived from the content the decay sweep read, so the
        write is refused if an erase touched the namespace since ``read_epoch`` (read with that
        content by ``fetch_decay_candidates``), and never lands on a node an erase scrubbed.

        ``read_content`` and ``read_summary`` are the raw ``n.content`` and ``n.summary`` the
        candidate row carried; the text summarised is the content, or the summary when the content
        is blank. Under the node lock the write re-checks that the node still qualifies and still
        holds what was read, then stores the summarised text as ``original_content`` together with
        a digest of the new summary that binds it to this compression.
        """

        source_text = read_content or read_summary or None

        # The MATCH reads without a lock and Neo4j does not re-check a WHERE after a write waits
        # for one: take the node's write lock, then decide.
        query = f"""
            MATCH (n:Entity {{uuid: $node_uuid}})
            SET n._decay_compress_lock = true
            REMOVE n._decay_compress_lock
            WITH n
            WHERE n.scope = 'PERSISTENT' AND n.freshness = 'ACTIVE'
              AND NOT coalesce(n.rederive_pending, false)
              AND {harmful_automatic_mutation_allowed_cypher("n")}
              AND coalesce(n.content, '') = coalesce($read_content, '')
              AND (coalesce($read_content, '') <> ''
                   OR coalesce(n.summary, '') = coalesce($read_summary, ''))
              AND {memory_recency_cypher()} < datetime() - duration({{days: $min_days}})
            SET n.original_content = $source_text,
                n.content = $compressed_summary,
                n.compressed_summary_sha256 = $summary_sha,
                n.freshness = 'COMPRESSED'
            RETURN count(n) AS updated
            """
        params = {
            "node_uuid": node_uuid,
            "compressed_summary": compressed_summary,
            "summary_sha": summary_sha256(compressed_summary),
            "read_content": read_content,
            "read_summary": read_summary,
            "source_text": source_text,
            "min_days": min_days_since_accessed,
        }
        return self._updated_unless_fenced(node_uuid, lambda: execute_copy_fenced(
            self.neo4j, fence_key, read_epoch, [node_uuid],
            lambda tx: tx.execute(query, params), key_from_uuid=node_uuid,
        ))

    def fetch_node_freshness(self, node_uuids: list[str]) -> dict[str, str]:
        """Return {uuid: freshness} for the given Entity nodes."""
        if not node_uuids:
            return {}
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids")
            .return_raw("n.uuid AS uuid, coalesce(n.freshness, 'ACTIVE') AS freshness")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": list(dict.fromkeys(node_uuids))})
        return {
            str(row["uuid"]): str(row.get("freshness") or "ACTIVE")
            for row in rows
            if row.get("uuid")
        }

    def fetch_rehydration_state(self, node_uuid: str) -> dict[str, object]:
        """What a rehydrate reads from the node, in one statement.

        ``content``, ``original_content`` and the summary digest are null when the node is
        ``rederive_pending``; the fence key and epoch are read with them.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{uuid: $node_uuid}})
            RETURN CASE WHEN coalesce(n.rederive_pending, false) THEN null ELSE n.content END AS content,
                   CASE WHEN coalesce(n.rederive_pending, false) THEN null
                        ELSE n.original_content END AS original_content,
                   CASE WHEN coalesce(n.rederive_pending, false) THEN null
                        ELSE n.compressed_summary_sha256 END AS compressed_summary_sha256,
                   coalesce(toInteger(n.rehydration_count), 0) AS rehydration_count,
                   size(coalesce(n.absorbed_from, [])) AS absorbed_count,
                   {fence_key_cypher("n")} AS fence_key,
                   head(COLLECT {{ MATCH (f:EvidenceNamespaceFence)
                                   WHERE f.namespace_key = {fence_key_cypher("n")}
                                   RETURN coalesce(f.erasure_epoch, 0) }}) AS fence_epoch
            """,
            params={"node_uuid": node_uuid},
        )
        if not rows:
            return {"content": None, "original_content": None, "compressed_summary_sha256": None,
                    "rehydration_count": 0, "absorbed_count": 0, "fence_key": None,
                    "fence_epoch": None}
        return dict(rows[0])

    def complete_rehydration(
        self,
        node_uuid: str,
        updated_content: str | None = None,
        *,
        read_summary: str | None,
        read_base: str | None,
        source_uuids: tuple[str, ...] = (),
        read_epoch: int | None = None,
    ) -> bool:
        """Transition a PERSISTENT COMPRESSED node back to ACTIVE.

        With ``updated_content`` (an LLM merge of the compression's replaced body and new context,
        or that body restored as it was) the write runs under G-copy with the node and
        ``source_uuids`` (the context's episode) as sources, and never lands on a node an erase
        scrubbed (CF-338). Without ``read_epoch`` the epoch comparison is skipped: the source
        markers, ``erasing_ops`` and ``rederive_pending`` still refuse a copy of erased text.
        ``read_summary`` and ``read_base`` are the ``content`` and ``original_content`` the caller
        read: a text-copying write refuses if either moved.

        Without ``updated_content`` the node only flips state.
        """
        copies_text = updated_content is not None
        params: dict[str, object] = {
            "node_uuid": node_uuid,
            "read_summary": read_summary,
            "read_base": read_base,
            "updated_content": updated_content,
        }

        # Lock, then decide: the MATCH reads without a lock and Neo4j does not re-check a WHERE
        # after a write waits for one.
        query = f"""
            MATCH (n:Entity {{uuid: $node_uuid}})
            SET n._decay_compress_lock = true
            REMOVE n._decay_compress_lock
            WITH n
            WHERE n.scope = 'PERSISTENT' AND n.freshness = 'COMPRESSED'
              AND {automatic_lifecycle_protection_cypher("n")}
              AND ($updated_content IS NULL OR (
                    coalesce(n.content, '') = coalesce($read_summary, '')
                    AND coalesce(n.original_content, '') = coalesce($read_base, '')
                    AND NOT coalesce(n.rederive_pending, false)))
            SET n.content = coalesce($updated_content, n.content),
                n.freshness = 'ACTIVE',
                n.last_accessed = datetime(),
                n.rehydration_count = coalesce(toInteger(n.rehydration_count), 0) + 1
            RETURN count(n) AS updated
            """
        if not copies_text:
            # A state flip; no text is copied.
            rows = self.neo4j.execute(query, params=params)
            return bool(rows and int(rows[0].get("updated", 0)) > 0)
        sources = [node_uuid, *(u for u in source_uuids if u)]
        return self._updated_unless_fenced(node_uuid, lambda: execute_copy_fenced(
            self.neo4j, None, read_epoch, sources,
            lambda tx: tx.execute(query, params), key_from_uuid=node_uuid,
        ))

    @staticmethod
    def _decay_delete_gate_cypher() -> str:
        """Every fact the delete decision was made from, as a WHERE body on the bound ``n``.

        The read values are the ones the caller saw; the live node may only be as stale or staler
        (no higher edge count or sharpness, no fresher recency), of the same type, not exempt, and
        still carrying exactly the incident relationships the before-snapshot recorded.
        """
        return f"""n.scope = 'PERSISTENT'
              AND n.freshness = 'COMPRESSED'
              AND NOT coalesce(n.rederive_pending, false)
              AND NOT coalesce(n.decay_exempt, false)
              AND coalesce(n.type, 'SEMANTIC') = $read_type
              AND ($requires_target_date = false
                   OR (n.target_date IS NOT NULL AND date(n.target_date) < date()))
              AND {memory_recency_cypher("n")} < datetime() - duration({{days: $min_days}})
              AND {memory_recency_cypher("n")} <= datetime($read_recency)
              AND coalesce(toInteger(n.edge_count), 0) < $max_edge_count
              AND coalesce(toInteger(n.edge_count), 0) <= $read_edge_count
              AND n.sharpness IS NOT NULL AND toFloat(n.sharpness) < $max_sharpness
              AND toFloat(n.sharpness) <= $read_sharpness
              AND size([(n)-[incident]-() | incident]) = $read_rel_count
              AND all(sig IN $read_rel_signatures WHERE sig IN [
                    (n)-[incident]-(peer) | type(incident) + '|'
                    + CASE WHEN startNode(incident) = n THEN 'out' ELSE 'in' END + '|'
                    + coalesce(peer.uuid, peer.name, elementId(peer))])"""

    def bridge_and_delete(
        self,
        node_uuid: str,
        *,
        min_days: float,
        max_edge_count: int,
        max_sharpness: float,
        read_type: str,
        read_edge_count: int,
        read_sharpness: float,
        read_recency: str,
        requires_target_date: bool,
        read_rel_count: int,
        read_rel_signatures: list[str],
    ) -> dict[str, Any]:
        """Bridge entity neighbors around a node, then delete it, in one statement.

        Only ``DeleteCoordinator`` (DECAY_DELETE) calls this, after journaling a before-snapshot.
        There is no ungated form: the node is locked and every fact the decision was made from is
        re-checked on the live node (``_decay_delete_gate_cypher``), so a node rehydrated, recalled,
        retyped or re-linked after selection is left alone: ``deleted`` is 0.

        A node with more than ``MAX_BRIDGE_NEIGHBOURS`` live neighbours is also refused
        (``reason`` ``bridge_cap_exceeded``, ``bridge_skipped_hubs``): that degree proves its cached
        edge count stale, and deleting it unbridged would sever them. Bridges follow
        ``bridge_cypher``; ``bridge_uuids`` lists the edges this call created.
        """
        params: dict[str, Any] = {
            "node_uuid": node_uuid,
            "min_days": min_days,
            "max_edge_count": max_edge_count,
            "max_sharpness": max_sharpness,
            "read_type": read_type,
            "read_edge_count": read_edge_count,
            "read_sharpness": read_sharpness,
            "read_recency": read_recency,
            "requires_target_date": requires_target_date,
            "read_rel_count": read_rel_count,
            "read_rel_signatures": read_rel_signatures,
        }
        query = f"""
            MATCH (n:Entity {{uuid: $node_uuid}})
            WHERE {harmful_automatic_mutation_allowed_cypher("n")}
            SET n._decay_delete_lock = true
            REMOVE n._decay_delete_lock
            WITH n
            // Re-check under the lock: a concurrent rehydrate or recall may have changed the node.
            WHERE {harmful_automatic_mutation_allowed_cypher("n")}
              AND {self._decay_delete_gate_cypher()}
            {bridge_cypher(excluded="[$node_uuid]")}
            WITH n, edges_bridged, bridge_uuids, bridge_skipped
            FOREACH (_ IN CASE WHEN bridge_skipped THEN [] ELSE [1] END | DETACH DELETE n)
            RETURN edges_bridged AS edges_bridged, bridge_uuids AS bridge_uuids,
                   CASE WHEN bridge_skipped THEN 0 ELSE 1 END AS deleted,
                   bridge_skipped AS bridge_skipped
            """
        rows = self.neo4j.execute(query, params=params, timeout_s=SAGA_MUTATION_TIMEOUT_S)
        if not rows:
            return {"edges_bridged": 0, "deleted": 0}
        row = rows[0]
        result: dict[str, Any] = {
            "edges_bridged": int(row.get("edges_bridged", 0) or 0),
            "deleted": int(row.get("deleted", 0) or 0),
            "bridge_uuids": [str(u) for u in (row.get("bridge_uuids") or [])],
        }
        if row.get("bridge_skipped"):
            result["bridge_skipped_hubs"] = [node_uuid]
            result["reason"] = "bridge_cap_exceeded"
        return result

    def decay_delete_gate_passes(
        self,
        node_uuid: str,
        *,
        min_days: float,
        max_edge_count: int,
        max_sharpness: float,
        read_type: str,
        read_edge_count: int,
        read_sharpness: float,
        read_recency: str,
        requires_target_date: bool,
        read_rel_count: int,
        read_rel_signatures: list[str],
    ) -> bool:
        """READ-ONLY: would ``bridge_and_delete`` delete this node right now (same gate, cap)."""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{uuid: $node_uuid}})
            WHERE {harmful_automatic_mutation_allowed_cypher("n")}
              AND {self._decay_delete_gate_cypher()}
            CALL {{
                WITH n
                OPTIONAL MATCH (n)-[:{adjacency_edge_pattern()}]-(neighbor:Entity)
                WHERE {_bridge_neighbour_filter_cypher("n", "neighbor", "[$node_uuid]")}
                WITH DISTINCT neighbor LIMIT {MAX_BRIDGE_NEIGHBOURS + 1}
                RETURN count(neighbor) AS k
            }}
            RETURN k <= {MAX_BRIDGE_NEIGHBOURS} AS passes
            """,
            params={
                "node_uuid": node_uuid, "min_days": min_days, "max_edge_count": max_edge_count,
                "max_sharpness": max_sharpness, "read_type": read_type,
                "read_edge_count": read_edge_count, "read_sharpness": read_sharpness,
                "read_recency": read_recency, "requires_target_date": requires_target_date,
                "read_rel_count": read_rel_count, "read_rel_signatures": read_rel_signatures,
            },
        )
        return bool(rows and rows[0].get("passes"))

    def update_sharpness(self, node_uuid: str, sharpness: float) -> bool:
        """Write computed sharpness to a node."""

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid = $node_uuid", automatic_lifecycle_protection_cypher("n"))
            .set("n.sharpness = $sharpness")
            .return_raw("count(n) AS updated")
            .build())
        rows = self.neo4j.execute(query, params={"node_uuid": node_uuid, "sharpness": sharpness})
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    # -------------------------------------------------------------------------
    # Session / scope management
    # -------------------------------------------------------------------------

    def fetch_session_entities(
        self,
        session_id: str | None = None,
        max_age_hours: float = 0,
    ) -> list[dict[str, Any]]:
        """Fetch SESSION-scoped Entity nodes eligible for consolidation.

        Includes ttl_expires for observability; the expiry decision is DB-side only.
        """

        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id
        if max_age_hours > 0:
            params["max_age_hours"] = max_age_hours

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'", automatic_lifecycle_protection_cypher("n"))
            .where_if(session_id is not None, "n.session_id = $session_id")
            .where_if(
                max_age_hours > 0,
                f"{memory_timestamp_cypher('n.created_at')} < datetime() - duration({{hours: $max_age_hours}})",
            )
            .return_raw("""n.uuid AS uuid,
       n.name AS name,
       n.content AS content,
       n.summary AS summary,
       n.type AS type,
       n.sharpness AS sharpness,
       n.user_flagged AS user_flagged,
       n.session_id AS session_id,
       """ + canonical_tenant_cypher("n") + """ AS namespace,
       n.created_at AS created_at,
       n.ttl_expires AS ttl_expires""")
            .order_by(f"{memory_timestamp_cypher('n.created_at')} ASC, n.uuid")
            .build())
        return self.neo4j.execute(query, params=params)

    def count_persistent_edges(self, node_uuid: str) -> int:
        """Count edges from a node to PERSISTENT or PROMOTED semantic nodes.

        Excludes ANCHORED_TO edges, GONE neighbors and structural neighbors
        (structure_role IS NOT NULL) so cross-domain anchor links don't inflate the promotion
        threshold during session consolidation.
        """

        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)-[r]-(other:Entity)
            WHERE n.uuid = $node_uuid
              AND other.scope IN ['PERSISTENT', 'PROMOTED']
              AND NOT type(r) = 'ANCHORED_TO'
              AND other.structure_role IS NULL
              AND coalesce(other.freshness, '') <> 'GONE'
            RETURN count(DISTINCT other) AS persistent_neighbors
            """,
            params={"node_uuid": node_uuid},
        )
        return int(rows[0].get("persistent_neighbors", 0)) if rows else 0

    def promote_to_persistent(self, node_uuids: list[str]) -> list[str]:
        """Promote SESSION nodes to PERSISTENT with ACTIVE freshness.

        Clears any pending demotion (ttl_expires) — a rescued node is no longer demoted.

        Returns the uuids actually promoted. A requested node that is gone (absorbed by a merge),
        no longer SESSION, or lifecycle-protected is not in it, so callers audit only real changes.
        """

        if not node_uuids:
            return []
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'",
                   automatic_lifecycle_protection_cypher("n"))
            .set(("n.scope = 'PERSISTENT'",
                  "n.freshness = 'ACTIVE'",
                  "n.promoted_at = datetime()",
                  "n.ttl_expires = null"))
            .return_raw("collect(n.uuid) AS promoted_uuids")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids})
        return [str(u) for u in (rows[0].get("promoted_uuids") or [])] if rows else []

    def delete_session_nodes(self, node_uuids: list[str]) -> int:
        """DETACH DELETE the given SESSION Entity nodes."""

        if not node_uuids:
            return 0
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .detach_delete("n")
            .return_raw("count(n) AS deleted")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids})
        return int(rows[0].get("deleted", 0)) if rows else 0

    def delete_entities_returning_uuids(
        self,
        node_uuids: list[str],
        *,
        require_scope: str | None = None,
        protect_retention: bool = False,
        require_expired_ttl: bool = False,
    ) -> list[str]:
        """DETACH DELETE the given Entity nodes and return the uuids ACTUALLY deleted (plan Phase 6).

        ``require_expired_ttl`` (TTL sweep only) re-checks ``ttl_expires < datetime()`` on the live
        node inside this statement, so a merge that cleared the TTL after selection spares it.

        The existing `delete_session_nodes` returns only a COUNT, so a caller could never tell WHICH
        of its targets died -- and because the scope filter silently skips a node that was promoted in
        the race window, the pre-logged audit could claim a deletion that never happened. Returning
        the exact uuids from the mutation itself is what lets the coordinator audit the truth instead
        of its intent.
        """
        if not node_uuids:
            return []
        scope_clause = "AND n.scope = $scope" if require_scope else ""
        if require_expired_ttl:
            scope_clause += " AND n.ttl_expires IS NOT NULL AND n.ttl_expires < datetime()"
        params: dict[str, Any] = {"uuids": node_uuids}
        if require_scope:
            params["scope"] = require_scope
        protection = (
            harmful_automatic_mutation_allowed_cypher("n")
            if protect_retention
            else automatic_lifecycle_protection_cypher("n")
        )
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids {scope_clause}
              AND {protection}
            WITH collect(n) AS doomed, collect(n.uuid) AS deleted_uuids
            FOREACH (d IN doomed | DETACH DELETE d)
            RETURN deleted_uuids
            """,
            params=params,
            timeout_s=SAGA_MUTATION_TIMEOUT_S,  # bounded for ownership ageing (CF-211)
        )
        return [str(u) for u in (rows[0].get("deleted_uuids") or [])] if rows else []

    def newly_unreferenced_evidence(self, node_uuids: list[str]) -> list[str]:
        """Evidence that would be left referenced by NOTHING once these nodes are deleted.

        REPORT ONLY -- this never deletes (plan section 8). Evidence ownership and sharing semantics
        are a separate design decision, and cascade-deleting it merely because it fell to degree zero
        is precisely the inference that destroyed ~24 nodes on 2026-07-12. Isolation is not
        authorization.
        """
        if not node_uuids:
            return []
        rows = self.neo4j.execute(
            """
            MATCH (e:Evidence)<-[:SUPPORTED_BY]-(n:Entity)
            WHERE n.uuid IN $uuids
            WITH e, collect(DISTINCT n.uuid) AS doomed_refs
            MATCH (e)<-[:SUPPORTED_BY]-(other:Entity)
            WITH e, doomed_refs, collect(DISTINCT other.uuid) AS all_refs
            WHERE size([x IN all_refs WHERE NOT x IN doomed_refs]) = 0
            RETURN collect(DISTINCT e.uuid) AS orphaned
            """,
            params={"uuids": node_uuids},
        )
        return [str(u) for u in (rows[0].get("orphaned") or [])] if rows else []

    def set_demote_ttl(self, node_uuids: list[str], ttl_days: int) -> list[str]:
        """Set a time-to-live for SESSION nodes (F5 demotion grace window).

        Sets once: a node that already has ttl_expires is NOT reset.
        Only nodes without a prior TTL are newly demoted.

        Returns the uuids of the nodes that had NO prior TTL (newly demoted).
        """

        if not node_uuids:
            return []

        # Decide and mutate in one statement so a source flag added after candidate discovery
        # cannot race the TTL write.
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'", "n.ttl_expires IS NULL",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .set("n.ttl_expires = datetime() + duration({days: $days})")
            .return_raw("collect(n.uuid) AS newly_demoted_uuids")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids, "days": ttl_days})
        return [str(u) for u in (rows[0].get("newly_demoted_uuids") or [])] if rows else []

    def fetch_ttl_expired_session_uuids(
        self,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch SESSION nodes whose ttl_expires has passed (expired demoted nodes to delete).

        Returns uuid, name, session_id for each expired node (for audit records before deletion).
        """

        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'",
                   "n.ttl_expires IS NOT NULL",
                   "n.ttl_expires < datetime()",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .where_if(session_id is not None, "n.session_id = $session_id")
            .return_raw("""n.uuid AS uuid,
       n.name AS name,
       n.session_id AS session_id""")
            .build())
        return self.neo4j.execute(query, params=params)

    # -------------------------------------------------------------------------
    # Conflict governance (M5)
    # -------------------------------------------------------------------------

    def set_conflict(
        self,
        node_uuid_a: str,
        node_uuid_b: str,
        new_group_id: str,
        *,
        initial_status: str = "pending_llm_review",
    ) -> tuple[str, int]:
        """Mark two nodes as conflicting, joining or merging any existing groups.

        The caller passes ``new_group_id`` as a fallback; if either node already
        belongs to a conflict group the existing ID is used as canonical.  When
        both nodes are in *different* existing groups all members of the retired
        group are migrated into the canonical one.

        ``initial_status`` defaults to ``'pending_llm_review'`` so new detections
        wait for LLM confirmation before surfacing to the user as ``'unresolved'``.

        ``a``, ``b`` and every member of either group are write-locked in one transaction before
        the groups are read, so a concurrent resolve, removal or join cannot be overwritten.

        Returns (canonical_group_id, nodes_updated).
        """
        decide_query = f"""
            MATCH (a:Entity), (b:Entity)
            WHERE a.uuid = $uuid_a AND b.uuid = $uuid_b
              AND {automatic_lifecycle_protection_cypher("a")}
              AND {automatic_lifecycle_protection_cypher("b")}
              // A conflict group must not span tenants: resolving it would bridge and retire
              // across them.
              AND {same_tenant_cypher("a", "b")}
            WITH a, b,
              CASE
                WHEN a.conflict_group_id IS NOT NULL THEN a.conflict_group_id
                WHEN b.conflict_group_id IS NOT NULL THEN b.conflict_group_id
                ELSE $new_group_id
              END AS canonical_group_id,
              CASE
                WHEN a.conflict_group_id IS NOT NULL
                     AND b.conflict_group_id IS NOT NULL
                     AND a.conflict_group_id <> b.conflict_group_id
                THEN b.conflict_group_id
                ELSE null
              END AS retired_group_id
            OPTIONAL MATCH (orphan:Entity)
            WHERE orphan.conflict_group_id = retired_group_id
              AND {automatic_lifecycle_protection_cypher("orphan")}
            WITH a, b, canonical_group_id, collect(orphan) AS orphans,
              // #654: a confirmed member is never sent back to review by a new pair.
              // Read before the SET below assigns the group id.
              (a.conflict_group_id IS NOT NULL AND a.conflict_status = 'unresolved') AS a_confirmed,
              (b.conflict_group_id IS NOT NULL AND b.conflict_status = 'unresolved') AS b_confirmed,
              // A node joining fresh starts a new age; a stale stamp from an earlier,
              // resolved conflict would make it look overdue at once.
              a.conflict_group_id IS NULL AS a_fresh,
              b.conflict_group_id IS NULL AS b_fresh,
              (a.conflict_group_id IS NOT NULL AND a.conflict_status = 'pending_llm_review') AS a_was_pending,
              (b.conflict_group_id IS NOT NULL AND b.conflict_status = 'pending_llm_review') AS b_was_pending
            SET a.conflict_group_id  = canonical_group_id,
                a.conflict_status    = CASE WHEN a_confirmed THEN 'unresolved' ELSE $initial_status END,
                a.conflict_created_at = CASE WHEN a_fresh THEN datetime()
                                             ELSE coalesce(a.conflict_created_at, datetime()) END,
                a.conflict_confirmed_at = CASE WHEN NOT a_confirmed AND $initial_status = 'unresolved'
                                               THEN datetime() ELSE a.conflict_confirmed_at END,
                a.conflict_pending_since = CASE
                    WHEN NOT a_confirmed AND $initial_status = 'pending_llm_review' AND NOT a_was_pending
                    THEN datetime() ELSE a.conflict_pending_since END,
                b.conflict_group_id  = canonical_group_id,
                b.conflict_status    = CASE WHEN b_confirmed THEN 'unresolved' ELSE $initial_status END,
                b.conflict_created_at = CASE WHEN b_fresh THEN datetime()
                                             ELSE coalesce(b.conflict_created_at, datetime()) END,
                b.conflict_confirmed_at = CASE WHEN NOT b_confirmed AND $initial_status = 'unresolved'
                                               THEN datetime() ELSE b.conflict_confirmed_at END,
                b.conflict_pending_since = CASE
                    WHEN NOT b_confirmed AND $initial_status = 'pending_llm_review' AND NOT b_was_pending
                    THEN datetime() ELSE b.conflict_pending_since END
            // #654: members absorbed from a merged group keep their own status and age.
            // A member resolved after the orphan read must not be left in a live group
            // with a terminal status, so anything not live goes back to review.
            FOREACH (o IN orphans |
                SET o.conflict_group_id = canonical_group_id,
                    o.conflict_pending_since = CASE
                        WHEN o.conflict_status IN ['pending_llm_review', 'unresolved']
                        THEN o.conflict_pending_since ELSE datetime() END,
                    o.conflict_status = CASE
                        WHEN o.conflict_status IN ['pending_llm_review', 'unresolved']
                        THEN o.conflict_status ELSE 'pending_llm_review' END,
                    o.conflict_created_at = coalesce(o.conflict_created_at, datetime())
            )
            // b is among the orphans of its own retired group, so it is not counted twice.
            RETURN canonical_group_id,
                   2 + size([o IN orphans WHERE o.uuid <> b.uuid]) AS updated
            """
        params = {
            "uuid_a": node_uuid_a,
            "uuid_b": node_uuid_b,
            "new_group_id": new_group_id,
            "initial_status": initial_status,
        }

        def work(tx: Any) -> list[dict[str, Any]]:
            tx.execute(
                f"""
                MATCH (x:Entity) WHERE x.uuid IN [$uuid_a, $uuid_b]
                WITH x ORDER BY x.uuid
                SET x.{CONFLICT_RESOLVE_LOCK} = true REMOVE x.{CONFLICT_RESOLVE_LOCK}
                RETURN count(x) AS locked
                """,
                params,
            )
            # a and b are locked now, so their group ids cannot move while the members are locked.
            tx.execute(
                f"""
                MATCH (a:Entity {{uuid: $uuid_a}}), (b:Entity {{uuid: $uuid_b}})
                WITH [a.conflict_group_id, b.conflict_group_id] AS group_ids
                MATCH (m:Entity) WHERE m.conflict_group_id IN group_ids
                WITH m ORDER BY m.uuid
                SET m.{CONFLICT_RESOLVE_LOCK} = true REMOVE m.{CONFLICT_RESOLVE_LOCK}
                RETURN count(m) AS locked
                """,
                params,
            )
            return tx.execute(decide_query, params)

        rows = self.neo4j.execute_write(work)
        if not rows:
            return new_group_id, 0
        canonical = str(rows[0].get("canonical_group_id") or new_group_id)
        updated = int(rows[0].get("updated", 0))
        return canonical, updated

    def set_conflict_group_status(self, group_id: str, status: str) -> int:
        """Update conflict_status for all members of a group. Returns nodes updated."""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.conflict_group_id = $group_id
              AND {automatic_lifecycle_protection_cypher("n")}
            SET n.conflict_status = $status
            RETURN count(n) AS updated
            """,
            params={"group_id": group_id, "status": status},
        )
        return int(rows[0].get("updated", 0)) if rows else 0

    def set_conflict_member_status(
        self,
        group_id: str,
        node_uuid: str,
        *,
        from_status: str,
        to_status: str,
        namespace: str | None = None,
    ) -> bool:
        """Move one member from ``from_status`` to ``to_status`` (#654).

        Compare-and-set: a member that left the group or changed status since it was
        read is not touched, so a concurrent review or resolution wins. Returns whether
        the member was updated.
        """
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.uuid = $uuid
              AND n.conflict_group_id = $group_id
              AND n.conflict_status = $from_status
              AND {tenant_scope_cypher("n")}
              AND {automatic_lifecycle_protection_cypher("n")}
            SET n.conflict_status = $to_status,
                n.conflict_confirmed_at = CASE WHEN $to_status = 'unresolved'
                                               THEN datetime() ELSE n.conflict_confirmed_at END
            RETURN count(n) AS updated
            """,
            params={
                "uuid": node_uuid,
                "group_id": group_id,
                "from_status": from_status,
                "to_status": to_status,
                **tenant_scope_params(ns or None),
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def remove_conflict_member(
        self,
        group_id: str,
        node_uuid: str,
        *,
        from_status: str,
        resolution_status: str = "false_positive",
        namespace: str | None = None,
        reviewed_uuids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Take one member out of a group after review cleared all its pairs (#654).

        ``reviewed_uuids`` names the partners the verdict covered. The removal is refused when
        the group now holds any other member, since that pair was never reviewed.

        Compare-and-set on ``from_status`` like ``set_conflict_member_status``. When fewer
        than two members remain, the group is dissolved: a group of one is not a conflict.

        Every current member is write-locked before the remainder is counted. A concurrent
        ``set_conflict`` joining through an existing member writes that member, so it either
        commits before the recount (and is counted) or waits for this statement.

        Returns ``removed`` (bool), ``dissolved`` (bool) and ``remaining_uuids``. A refusal also
        carries ``reason`` (``unreviewed_members`` with ``unreviewed_uuids``, or ``member_changed``
        when the member left the group, changed status or is out of scope).
        """
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (target:Entity)
            WHERE target.uuid = $uuid
              AND target.conflict_group_id = $group_id
              AND target.conflict_status = $from_status
              AND {tenant_scope_cypher("target")}
              AND {automatic_lifecycle_protection_cypher("target")}
            MATCH (lockm:Entity)
            WHERE lockm.conflict_group_id = $group_id
            SET lockm._conflict_group_lock = true
            REMOVE lockm._conflict_group_lock
            WITH DISTINCT target
            // Re-check under the lock: a concurrent review may have moved it already.
            WHERE target.conflict_group_id = $group_id
              AND target.conflict_status = $from_status
            OPTIONAL MATCH (o:Entity)
            WHERE o.conflict_group_id = $group_id AND o.uuid <> $uuid
            WITH target, collect(o) AS others
            // The verdict covers only the partners that were reviewed. A member that joined
            // since then has not been compared, so the removal waits for the next run.
            WHERE $reviewed_uuids IS NULL
               OR all(x IN others WHERE x.uuid IN $reviewed_uuids)
            SET target.conflict_group_id = null,
                target.conflict_status = $resolution_status
            // Dissolve on the true member count, not only the members this caller may write.
            WITH size(others) < 2 AS dissolved, [x IN others | x.uuid] AS remaining_uuids
            OPTIONAL MATCH (m:Entity)
            WHERE dissolved
              AND m.conflict_group_id = $group_id
              AND {tenant_scope_cypher("m")}
              AND {automatic_lifecycle_protection_cypher("m")}
            WITH dissolved, remaining_uuids, collect(m) AS writable
            FOREACH (r IN writable |
                SET r.conflict_group_id = null,
                    r.conflict_status = $resolution_status
            )
            RETURN dissolved, remaining_uuids
            """,
            params={
                "uuid": node_uuid,
                "group_id": group_id,
                "from_status": from_status,
                "resolution_status": resolution_status,
                "reviewed_uuids": list(reviewed_uuids) if reviewed_uuids is not None else None,
                **tenant_scope_params(ns or None),
            },
        )
        if not rows:
            # Advisory diagnosis of the refusal, so the caller can log why nothing was removed.
            # The refusal counts every member, as the removal does; only this namespace's uuids
            # are named.
            unreviewed: list[str] = []
            any_unreviewed = False
            if reviewed_uuids is not None:
                unreviewed_rows = self.neo4j.execute(
                    f"""
                    MATCH (o:Entity)
                    WHERE o.conflict_group_id = $group_id AND o.uuid <> $uuid
                      AND NOT o.uuid IN $reviewed_uuids
                    RETURN o.uuid AS uuid, {tenant_scope_cypher("o")} AS visible
                    ORDER BY o.uuid
                    """,
                    params={
                        "group_id": group_id,
                        "uuid": node_uuid,
                        "reviewed_uuids": list(reviewed_uuids),
                        **tenant_scope_params(ns or None),
                    },
                )
                any_unreviewed = bool(unreviewed_rows)
                unreviewed = [
                    str(r["uuid"]) for r in unreviewed_rows if r.get("uuid") and r.get("visible")
                ]
            return {
                "removed": False,
                "dissolved": False,
                "remaining_uuids": [],
                "reason": "unreviewed_members" if any_unreviewed else "member_changed",
                "unreviewed_uuids": unreviewed,
            }
        row = rows[0]
        return {
            "removed": True,
            "dissolved": bool(row.get("dissolved")),
            "remaining_uuids": [str(u) for u in (row.get("remaining_uuids") or [])],
        }

    def auto_resolve_settled_conflict_group(self, group_id: str) -> list[str]:
        """Age out a confirmed group as ``auto-resolved``, only if every member is confirmed.

        A member still pending review was never compared, so marking it auto-resolved would
        label it a confirmed contradiction and suppress its pairs unreviewed (#654 review F1).
        Such a group is skipped until review settles it. Members are write-locked first, so a
        pair joining concurrently either commits before the check (and blocks the resolve) or
        waits for it. Returns the resolved member uuids, empty when skipped.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (lockm:Entity)
            WHERE lockm.conflict_group_id = $group_id
            SET lockm._conflict_group_lock = true
            REMOVE lockm._conflict_group_lock
            WITH count(lockm) AS locked
            WHERE locked > 0
            MATCH (m:Entity)
            WHERE m.conflict_group_id = $group_id
            WITH collect(m) AS members
            WHERE size(members) > 0
              AND all(x IN members WHERE x.conflict_status = 'unresolved')
            UNWIND members AS m
            WITH m
            WHERE {automatic_lifecycle_protection_cypher("m")}
            SET m.conflict_group_id = null,
                m.conflict_status = 'auto-resolved'
            RETURN collect(m.uuid) AS member_uuids
            """,
            params={"group_id": group_id},
        )
        if not rows:
            return []
        return [str(u) for u in (rows[0].get("member_uuids") or [])]

    def requeue_conflicts_for_llm_review(
        self,
        *,
        from_status: str = "unresolved",
        limit: int = 200,
        namespace: str | None = None,
    ) -> int:
        """Re-set conflict_status to pending_llm_review for groups in from_status.

        Returns distinct group count re-queued.

        ``namespace`` is opt-in and filters BOTH halves of this statement. Filtering only
        the selecting match would pick groups by the caller's silo and then mutate every
        member of them, including members in another silo if a legacy group is mixed --
        a cross-namespace write authorized by a same-namespace read. The predicate on ``m`` is
        what makes the write set a subset of what the caller may see. Both use
        ``tenant_scope_cypher`` so a node carrying only ``group_id`` is matched by its group,
        not treated as default.
        """
        safe_limit = max(1, min(limit, 1000))
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.conflict_group_id IS NOT NULL
              AND n.conflict_status = $from_status
              AND {tenant_scope_cypher("n")}
              AND {automatic_lifecycle_protection_cypher("n")}
            WITH DISTINCT n.conflict_group_id AS gid
            LIMIT $limit
            MATCH (m:Entity)
            WHERE m.conflict_group_id = gid
              AND {tenant_scope_cypher("m")}
              AND {automatic_lifecycle_protection_cypher("m")}
            SET m.conflict_pending_since = CASE WHEN m.conflict_status = 'pending_llm_review'
                                                THEN m.conflict_pending_since ELSE datetime() END,
                m.conflict_status = 'pending_llm_review'
            RETURN count(DISTINCT gid) AS groups_requeued
            """,
            params={
                "from_status": from_status,
                "limit": safe_limit,
                **tenant_scope_params(ns or None),
            },
        )
        return int(rows[0].get("groups_requeued", 0)) if rows else 0

    def list_conflict_groups(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
        namespace: str | None = None,
        created_before: datetime | None = None,
        oldest_first: bool = False,
        include_all_members: bool = False,
    ) -> list[dict[str, Any]]:
        """Return grouped conflicts keyed by conflict_group_id.

        ``include_all_members`` (opt-in, #654) selects groups that have at least one member
        in ``status`` but returns every live member of each, with its own status. The
        per-member LLM review needs the whole group to compare a member against.

        When ``status`` is ``None`` all conflict statuses are included.

        ``namespace`` filters members to one silo. It is opt-in per the namespace
        contract: ``None``/empty does not filter, preserving today's behavior exactly.

        ``created_before`` (a cutoff datetime) and ``oldest_first`` (bool) are opt-in for
        the stale-auto-resolver (CF-120) only. When both are left at their defaults the
        emitted Cypher and params are byte-for-byte identical to the pre-CF-120 form, so
        the MCP/explorer callers keep their newest-first listing.

        The predicate is applied per MEMBER, not per group. Conflict groups are
        namespace-homogeneous by construction -- the only writer of
        ``conflict_group_id`` is ``set_conflict``, whose single caller
        (``lifecycle_consolidation.record_conflict_pair``) is fed by consolidation and by
        enrichment correlation, both of which search for pair candidates scoped to the
        node's namespace (``group_ids=namespace_to_group_ids(...)``), so a pair
        can only form inside one silo, and the group-merge branch of ``set_conflict``
        preserves that inductively. A member-level predicate is nonetheless the correct
        form: it is what makes a legacy group that predates that scoping return only the
        caller's own half rather than leaking the other silo's content.
        """
        safe_limit = max(1, min(limit, 200))
        ns = str(namespace).strip() if namespace is not None else ""
        params: dict[str, Any] = {
            "status": status,
            "limit": safe_limit,
            **tenant_scope_params(ns or None),
        }

        # Filtered POST-aggregation. A group's age is min(conflict_created_at) over its members,
        # so a member-level predicate would change what min() sees and could age a group by a
        # member the filter excluded. The trade is that the conflict_created_at index cannot
        # serve a post-aggregation filter; correctness of the age wins over the index here.
        cutoff_clause = ""
        if created_before is not None:
            params["created_before"] = created_before
            cutoff_clause = "\n            WHERE created_at < $created_before"

        # Both fragments below are constants chosen here, never caller text -- Cypher cannot
        # parameterize a sort direction, so this stays interpolation and stays a closed set.
        order = "ASC" if oldest_first else "DESC"

        if include_all_members:
            return self.neo4j.execute(
                f"""
                MATCH (n:Entity)
                WHERE n.conflict_group_id IS NOT NULL
                  AND ($status IS NULL OR n.conflict_status = $status)
                  AND {tenant_scope_cypher("n")}
                  AND {automatic_lifecycle_protection_cypher("n")}
                WITH n.conflict_group_id AS group_id,
                     min(n.conflict_created_at) AS created_at{cutoff_clause}
                WITH group_id, created_at
                ORDER BY created_at {order}
                LIMIT $limit
                MATCH (m:Entity)
                WHERE m.conflict_group_id = group_id
                  AND {tenant_scope_cypher("m")}
                  AND {automatic_lifecycle_protection_cypher("m")}
                WITH group_id, created_at, collect({{
                       uuid: m.uuid,
                       name: m.name,
                       content: coalesce(m.summary, m.content, ''),
                       status: m.conflict_status,
                       scope: m.scope,
                       node_created_at: m.created_at
                     }}) AS members
                RETURN group_id, created_at, members
                ORDER BY created_at {order}
                """,
                params=params,
            )

        return self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.conflict_group_id IS NOT NULL
              AND ($status IS NULL OR n.conflict_status = $status)
              AND {tenant_scope_cypher("n")}
              AND {automatic_lifecycle_protection_cypher("n")}
            WITH n.conflict_group_id AS group_id,
                 min(n.conflict_created_at) AS created_at,
                 collect({{
                   uuid: n.uuid,
                   name: n.name,
                   content: coalesce(n.summary, n.content, ''),
                   status: n.conflict_status,
                   scope: n.scope,
                   node_created_at: n.created_at
                 }}) AS members{cutoff_clause}
            RETURN
              group_id,
              created_at,
              members
            ORDER BY created_at {order}
            LIMIT $limit
            """,
            params=params,
        )

    def list_conflict_pairs(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for older callers.

        Returns grouped rows from ``list_conflict_groups``.
        """
        return self.list_conflict_groups(status=status, limit=limit)

    def bridge_edges_for_node(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> int:
        """Bridge edges around a single GONE node without deleting it.

        Called immediately after setting ``freshness = 'GONE'`` during manual
        conflict resolution so that the removed node's former neighbors are
        reconnected before the 24 h decay sweep runs.

        Bridges follow ``bridge_cypher`` (adjacency edges only; live, non-derived, same-tenant
        neighbours; none for a hub). When ``namespace`` is supplied, the node and its neighbors
        stay in that silo.
        """
        return self.bridge_edges_for_nodes([node_uuid], namespace=namespace)

    def bridge_edges_for_nodes(
        self, node_uuids: list[str], *, namespace: str | None = None
    ) -> int:
        """Bridge edges around multiple GONE nodes efficiently in one query.

        Called immediately after setting ``freshness = 'GONE'`` during manual
        conflict resolution so that the removed nodes' former neighbors are
        reconnected before the 24 h decay sweep runs.

        Bridges follow ``bridge_cypher``: adjacency edges only, live non-derived same-tenant
        neighbours that are not themselves being removed, one edge per pair, none for a hub. When
        ``namespace`` is supplied, all participants are restricted to that silo. Returns the number
        of edges created.
        """
        return int(self.bridge_edges_for_nodes_detailed(node_uuids, namespace=namespace)[
            "edges_bridged"
        ])

    def bridge_edges_for_nodes_detailed(
        self, node_uuids: list[str], *, namespace: str | None = None, tx: Any | None = None
    ) -> dict[str, Any]:
        """``bridge_edges_for_nodes`` plus the removed nodes skipped as hubs.

        With ``tx`` the statement runs in that open transaction instead of its own. Returns ``{"edges_bridged": int, "bridge_skipped_hubs": [uuid, ...]}``.
        """
        node_uuids = list(dict.fromkeys(node_uuids))
        if not node_uuids:
            return {"edges_bridged": 0, "bridge_skipped_hubs": []}

        bridge = bridge_cypher(
            excluded="$node_uuids",
            neighbor_filters=(tenant_scope_cypher("neighbor"),),
            pair_filters=(tenant_scope_cypher("a"), tenant_scope_cypher("b")),
        )
        query = f"""
            UNWIND $node_uuids AS node_uuid
            MATCH (n:Entity {{uuid: node_uuid}})
            WHERE n.freshness = 'GONE'
              AND {tenant_scope_cypher("n")}
              AND {automatic_lifecycle_protection_cypher("n")}
            {bridge}
            RETURN sum(coalesce(edges_bridged, 0)) AS total_edges_bridged,
                   collect(CASE WHEN bridge_skipped THEN node_uuid END) AS skipped_hubs
            """
        bridge_params = {"node_uuids": node_uuids, **tenant_scope_params(namespace)}
        if tx is not None:
            rows = tx.execute(query, bridge_params)
        else:
            rows = self.neo4j.execute(query, params=bridge_params)
        if not rows:
            return {"edges_bridged": 0, "bridge_skipped_hubs": []}
        return {
            "edges_bridged": int(rows[0].get("total_edges_bridged", 0) or 0),
            "bridge_skipped_hubs": [str(u) for u in (rows[0].get("skipped_hubs") or [])],
        }

    def estimate_bridges_for_nodes(
        self, node_uuids: list[str], *, namespace: str | None = None
    ) -> dict[str, Any]:
        """READ-ONLY upper bound on the bridges removing these nodes would create.

        Counts each node's eligible neighbours k with the same filters ``bridge_cypher`` uses.
        Returns ``{"neighbour_counts": {uuid: k}, "estimated_edges": sum k(k-1)/2 over nodes with
        k <= MAX_BRIDGE_NEIGHBOURS, "over_cap": [uuid, ...]}``. It is an upper bound: pairs that
        already have an edge, or that two removed nodes share, are not netted out.
        """
        node_uuids = list(dict.fromkeys(node_uuids))
        if not node_uuids:
            return {"neighbour_counts": {}, "estimated_edges": 0, "over_cap": []}
        where = _bridge_neighbour_filter_cypher(
            "n", "neighbor", "$node_uuids", (tenant_scope_cypher("neighbor"),)
        )
        rows = self.neo4j.execute(
            f"""
            UNWIND $node_uuids AS node_uuid
            MATCH (n:Entity {{uuid: node_uuid}})
            WHERE {tenant_scope_cypher("n")}
            OPTIONAL MATCH (n)-[:{adjacency_edge_pattern()}]-(neighbor:Entity)
            WHERE {where}
            RETURN n.uuid AS uuid, count(DISTINCT neighbor) AS k
            """,
            params={"node_uuids": node_uuids, **tenant_scope_params(namespace)},
        )
        counts = {str(r["uuid"]): int(r.get("k", 0) or 0) for r in rows}
        over = sorted(u for u, k in counts.items() if k > MAX_BRIDGE_NEIGHBOURS)
        estimated = sum(k * (k - 1) // 2 for k in counts.values() if k <= MAX_BRIDGE_NEIGHBOURS)
        return {"neighbour_counts": counts, "estimated_edges": estimated, "over_cap": over}

    def resolve_conflict_group(
        self,
        conflict_group_id: str,
        action: str,
        *,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        resolution_status: str = "resolved",
        allow_promoted_removal: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a conflict group via one of three actions.

        Actions:
        - ``keep_both``    — both nodes are valid; clear group, set resolved on all.
        - ``replace``      — ``remove_uuid`` is superseded; content absorbed into
                             ``keep_uuid``, removed node set to GONE, edges bridged.
        - ``discard_new``  — same mechanics as ``replace``.

        ``replace`` and ``discard_new`` commit all-or-nothing in one transaction: every member is
        write-locked, then membership, keep and PROMOTED are re-checked, each target is absorbed
        at most once, and only then are members marked GONE and bridged. A refused absorption
        (erasure fence) or a changed member raises ``ConflictResolveRefused`` and changes nothing.

        ``resolution_status`` defaults to ``'resolved'``; the scheduler's auto-resolve path is
        ``auto_resolve_settled_conflict_group``, not this method.

        ``namespace`` is opt-in. When supplied, every read and mutation is
        restricted to that silo, including mixed legacy groups.

        Returns a summary dict with action, group_id, resolved count, and
        remove_uuid when applicable.
        """
        _VALID_ACTIONS = {"keep_both", "replace", "discard_new"}
        if action not in _VALID_ACTIONS:
            raise ValueError(f"Invalid action {action!r}; must be one of {_VALID_ACTIONS}")

        ns = str(namespace).strip() if namespace is not None else ""
        namespace_value = ns or None
        namespace_predicate = tenant_scope_cypher("n")
        namespace_params = tenant_scope_params(namespace_value)
        lock = CONFLICT_RESOLVE_LOCK

        # Locks every member, in uuid order, for the transaction. Lockers that take other nodes
        # first (set_conflict's a and b, the bridge's neighbours) can still deadlock with it;
        # Neo4j detects that and execute_write retries.
        lock_members_query = (
            f"MATCH (n:Entity) WHERE n.conflict_group_id = $group_id AND {namespace_predicate} "
            f"AND {automatic_lifecycle_protection_cypher('n')} "
            f"WITH n ORDER BY n.uuid SET n.{lock} = true REMOVE n.{lock} "
            "RETURN count(n) AS locked"
        )
        group_params = {"group_id": conflict_group_id, **namespace_params}

        if action == "keep_both":

            def keep_both_work(tx: Any) -> list[dict[str, Any]]:
                tx.execute(lock_members_query, group_params)
                return tx.execute(
                    (Cypher()
                        .match("(n:Entity)")
                        .where("n.conflict_group_id = $group_id",
                               namespace_predicate,
                               automatic_lifecycle_protection_cypher("n"))
                        .set(("n.conflict_status = $resolution_status", "n.conflict_group_id = null"))
                        .return_raw("count(n) AS resolved, collect(n.uuid) AS member_uuids")
                        .build()),
                    {**group_params, "resolution_status": resolution_status},
                )

            rows = self.neo4j.execute_write(keep_both_work)
            resolved = int(rows[0].get("resolved", 0)) if rows else 0
            member_uuids_written = (
                sorted(str(u) for u in (rows[0].get("member_uuids") or []) if u) if rows else []
            )
            return {
                "action": action,
                "group_id": conflict_group_id,
                "resolved": resolved,
                "removed_uuid": None,
                "removed_uuids": [],
                "bridged_edges": 0,
                "member_uuids": member_uuids_written,
            }

        if not keep_uuid:
            raise ValueError(f"action={action!r} requires keep_uuid")

        def validated_removals(rows: list[dict[str, Any]]) -> list[str]:
            """The python guards: keep and targets are members, targets are not PROMOTED."""
            member_uuids = {str(row.get("uuid") or "") for row in rows if row.get("uuid")}
            if keep_uuid not in member_uuids:
                raise ValueError(
                    f"keep_uuid {keep_uuid!r} is not a member of conflict_group_id={conflict_group_id!r}"
                )

            if remove_uuid is None:
                remove_uuids = sorted(uuid for uuid in member_uuids if uuid != keep_uuid)
            else:
                if remove_uuid == keep_uuid:
                    raise ValueError("remove_uuid cannot equal keep_uuid")
                if remove_uuid not in member_uuids:
                    raise ValueError(
                        f"remove_uuid {remove_uuid!r} is not a member of "
                        f"conflict_group_id={conflict_group_id!r}"
                    )
                remove_uuids = [remove_uuid]

            if not remove_uuids:
                raise ValueError("No removable members found in conflict group")

            if not allow_promoted_removal:
                promoted = sorted(
                    (
                        str(row.get("name") or "(unnamed)"),
                        str(row.get("uuid") or ""),
                    )
                    for row in rows
                    if str(row.get("uuid") or "") in set(remove_uuids)
                    and str(row.get("scope") or "") == "PROMOTED"
                )
                if promoted:
                    raise ValueError(
                        "Refusing to GONE PROMOTED node(s): "
                        + ", ".join(f"{name} ({uuid})" for name, uuid in promoted)
                        + ". Pass allow_promoted_removal=True to override."
                    )
            return remove_uuids

        # Early read, outside the lock: fails fast on a bad request and supplies the G-copy fence
        # key and epoch (CF-332). The epoch is read before any content, so an erase landing before
        # the transaction's fence check refuses the copy instead of resurrecting scrubbed text.
        members = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id",
                       namespace_predicate,
                       automatic_lifecycle_protection_cypher("n"))
                .return_raw("n.uuid AS uuid, n.scope AS scope, n.name AS name, "
                            f"{fence_key_cypher('n')} AS fence_key, "
                            "head(COLLECT { MATCH (f:EvidenceNamespaceFence) WHERE "
                            f"f.namespace_key = {fence_key_cypher('n')} "
                            "RETURN coalesce(f.erasure_epoch, 0) }) AS fence_epoch")
                .build()),
            params=group_params,
        )
        validated_removals(members)
        fence_keys = {
            str(r["uuid"]): str(r.get("fence_key") or "default") for r in members if r.get("uuid")
        }
        fence_epochs = {str(r["uuid"]): int(r.get("fence_epoch") or 0) for r in members if r.get("uuid")}
        copy_key = fence_keys.get(keep_uuid, "default")
        copy_epoch = fence_epochs.get(keep_uuid, 0)

        read_members_query = (
            Cypher()
            .match("(n:Entity)")
            .where("n.conflict_group_id = $group_id",
                   namespace_predicate,
                   automatic_lifecycle_protection_cypher("n"))
            .return_raw("n.uuid AS uuid, n.scope AS scope, n.name AS name, "
                        f"{fence_key_cypher('n')} AS fence_key, n.freshness AS freshness, "
                        "n.content AS content, n.original_content AS original_content, "
                        "n.compressed_summary_sha256 AS marker, n.absorbed_from AS absorbed_from")
            .build()
        )

        def work(tx: Any) -> dict[str, Any]:
            # execute_write may re-run this body, so every piece of state is local to it.
            tx.execute(lock_members_query, group_params)
            locked_rows = tx.execute(read_members_query, group_params)
            by_uuid = {str(r["uuid"]): r for r in locked_rows if r.get("uuid")}
            if set(by_uuid) != set(fence_keys):
                # A member joined or left since the early read. The fence's ErasureMarker check
                # covered only the early members, so a late joiner's text must not be copied.
                raise ConflictResolveRefused(
                    "member_changed",
                    "the group's members changed since they were read: "
                    + ", ".join(sorted(set(by_uuid) ^ set(fence_keys))),
                )
            # Re-check under the lock. The early read passed, so a failure here is a concurrent
            # change (a promotion), reported as a refusal rather than a bad request.
            try:
                remove_uuids = validated_removals(locked_rows)
            except ConflictResolveRefused:
                raise
            except ValueError as exc:
                raise ConflictResolveRefused("member_changed", str(exc)) from exc
            keep_row = by_uuid[keep_uuid]
            if str(keep_row.get("fence_key") or "default") != copy_key:
                raise ConflictResolveRefused("fence_key_changed", f"keep {keep_uuid}")

            # The keep's content as read under the lock is authoritative; it is advanced after
            # each target so that every block survives the next write.
            keep_content = str(keep_row.get("content") or "")
            keep_original = keep_row.get("original_content")
            keep_marker = keep_row.get("marker")
            keep_compressed = keep_row.get("freshness") == "COMPRESSED"
            absorbed_from = [str(u) for u in (keep_row.get("absorbed_from") or [])]

            for target_uuid in remove_uuids:
                target = by_uuid[target_uuid]
                remove_content = str(target.get("content") or "")
                if not remove_content.strip() or target_uuid in absorbed_from:
                    continue
                if action == "discard_new":
                    overlap = _content_overlap_ratio(keep_content, remove_content)
                    if overlap >= 0.85:
                        logger.debug(
                            "Skipping absorption for discard_new due to high overlap keep=%s remove=%s ratio=%.3f",
                            keep_uuid,
                            target_uuid,
                            overlap,
                        )
                        continue
                if str(target.get("fence_key") or "default") != copy_key:
                    raise ConflictResolveRefused(
                        "fence_key_mismatch",
                        f"{target_uuid} is in another erasure namespace than keep {keep_uuid}",
                    )
                absorbed_date = datetime.now(timezone.utc).date().isoformat()
                block = (
                    f"{ABSORBED_BLOCK_SEPARATOR}{target_uuid}) on {absorbed_date} ---\n"
                    f"{remove_content}"
                )
                merged = f"{keep_content}{block}"
                assignments = [
                    "n.content = $content",
                    # The erase's selector for this block.
                    "n.absorbed_from = coalesce(n.absorbed_from, []) + $target",
                ]
                absorb_params: dict[str, Any] = {
                    "uuid": keep_uuid,
                    "content": merged,
                    "target": target_uuid,
                    **namespace_params,
                }
                next_original, next_marker = keep_original, keep_marker
                if keep_compressed:
                    # A COMPRESSED keep holds its replaced body in original_content: the block goes
                    # there too and the digest follows the new content, but only over a base
                    # already trusted (marker = digest of the content read). Otherwise the base
                    # stays and the marker is removed.
                    if keep_original is not None and keep_marker == summary_sha256(keep_content):
                        next_original = f"{keep_original}{block}"
                        next_marker = summary_sha256(merged)
                        assignments.append("n.original_content = $original")
                        absorb_params["original"] = next_original
                    else:
                        next_marker = None
                    assignments.append("n.compressed_summary_sha256 = $marker")
                    absorb_params["marker"] = next_marker
                elif keep_original is None and keep_content:
                    # First absorption keeps a snapshot of the text it is about to extend.
                    next_original = keep_content
                    assignments.append("n.original_content = $original")
                    absorb_params["original"] = next_original
                absorbed_rows = tx.execute(
                    (Cypher()
                        .match("(n:Entity)")
                        .where("n.uuid = $uuid", namespace_predicate,
                               automatic_lifecycle_protection_cypher("n"),
                               # A target already absorbed is a no-op, never a second block.
                               "NOT $target IN coalesce(n.absorbed_from, [])")
                        .set(tuple(assignments))
                        .return_raw("count(n) AS absorbed")
                        .build()),
                    absorb_params,
                )
                if not absorbed_rows or int(absorbed_rows[0].get("absorbed") or 0) != 1:
                    raise ConflictResolveRefused("keep_changed", f"absorb into {keep_uuid} matched nothing")
                logger.debug(
                    "Content absorbed from %s into %s (%d chars appended)",
                    target_uuid,
                    keep_uuid,
                    len(remove_content),
                )
                keep_content = merged
                keep_original, keep_marker = next_original, next_marker
                absorbed_from.append(target_uuid)

            # --- Step 1: GONE selected members, re-checking group and PROMOTED ---
            gone_rows = tx.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.conflict_group_id = $group_id", "n.uuid IN $remove_uuids",
                           namespace_predicate,
                           automatic_lifecycle_protection_cypher("n"),
                           "($allow_promoted OR coalesce(n.scope, '') <> 'PROMOTED')")
                    .set(("n.freshness = 'GONE'",
                          "n.conflict_status = $resolution_status",
                          "n.conflict_group_id = null"))
                    .return_raw("collect(n.uuid) AS removed_uuids")
                    .build()),
                {
                    **group_params,
                    "remove_uuids": remove_uuids,
                    "resolution_status": resolution_status,
                    "allow_promoted": bool(allow_promoted_removal),
                },
            )
            removed_uuids = (
                sorted(str(u) for u in (gone_rows[0].get("removed_uuids") or []) if str(u))
                if gone_rows else []
            )
            if removed_uuids != sorted(remove_uuids):
                raise ConflictResolveRefused(
                    "member_changed", "a member left the group or was promoted during the resolve"
                )

            # --- Step 2: resolve all remaining group members ---
            rest_rows = tx.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.conflict_group_id = $group_id",
                           namespace_predicate,
                           automatic_lifecycle_protection_cypher("n"))
                    .set(("n.conflict_status = $resolution_status", "n.conflict_group_id = null"))
                    .return_raw("count(n) AS resolved")
                    .build()),
                {**group_params, "resolution_status": resolution_status},
            )
            resolved = int(rest_rows[0].get("resolved", 0)) if rest_rows else 0

            if action == "discard_new":
                tx.execute(
                    (Cypher()
                        .match("(n:Entity)")
                        .where("n.uuid = $keep_uuid", namespace_predicate,
                               automatic_lifecycle_protection_cypher("n"))
                        .set(("n.sharpness = CASE"
                              " WHEN n.sharpness IS NULL THEN 0.0"
                              " WHEN toFloat(n.sharpness) - 0.1 < 0.0 THEN 0.0"
                              " ELSE toFloat(n.sharpness) - 0.1 END",
                              "n.pending_review = true"))
                        .build()),
                    {"keep_uuid": keep_uuid, **namespace_params},
                )

            # --- Immediate edge bridge (avoid 24 h dead zone), in the same transaction ---
            bridged = self.bridge_edges_for_nodes_detailed(
                removed_uuids, namespace=namespace_value, tx=tx
            )
            return {
                "action": action,
                "group_id": conflict_group_id,
                "resolved": resolved,
                "removed_uuid": removed_uuids[0] if removed_uuids else None,
                "removed_uuids": removed_uuids,
                "bridged_edges": bridged["edges_bridged"],
                "bridge_skipped_hubs": bridged["bridge_skipped_hubs"],
                "member_uuids": sorted(by_uuid),
            }

        try:
            return execute_copy_fenced(
                self.neo4j, copy_key, copy_epoch, sorted(fence_keys), work
            )
        except CopyFenced as fenced:
            # An erase touched the namespace since the reads. Refuse the whole resolve rather
            # than retire members whose text was never absorbed.
            logger.info(
                "Resolve of %s into %s refused by the erasure fence (%s)",
                conflict_group_id, keep_uuid, fenced.reason,
            )
            raise ConflictResolveRefused(
                "erasure_fence", f"{fenced.reason}; retry after the erasure finishes"
            ) from fenced
