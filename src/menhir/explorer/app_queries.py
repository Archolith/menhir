"""Cypher read helpers backing the explorer dashboard, partials, and graph views."""

from __future__ import annotations

from typing import Any

from menhir.infrastructure import Neo4jRepository


def _scope_badge(scope: str | None) -> str:
    return (scope or "unknown").lower()


def _node_kind(labels: list[str]) -> str:
    if "Episodic" in labels:
        return "episode"
    if "Entity" in labels:
        return "entity"
    return (labels[0] if labels else "node").lower()


def _graph_node_class(labels: list[str], scope: str | None) -> str:
    return f"{_node_kind(labels)} {_scope_badge(scope)}"


def _recent_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            toString(e.created_at) AS created_at,
            left(coalesce(e.content, ''), 140) AS preview,
            coalesce(e.enriched_nodes_touched, 0) AS nodes_touched,
            coalesce(e.enriched_edges_touched, 0) AS edges_touched
        ORDER BY e.created_at DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _queued_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state IN ['PENDING', 'ENRICHING']
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_state AS processing_state,
            e.processing_stage AS processing_stage,
            coalesce(toFloat(e.processing_progress), 0.0) AS processing_progress,
            coalesce(toInteger(e.processing_steps_total), 0) AS processing_steps_total,
            coalesce(toInteger(e.processing_steps_completed), 0) AS processing_steps_completed,
            coalesce(toInteger(e.processing_llm_tasks_attempt), 0) AS processing_llm_tasks_attempt,
            coalesce(toInteger(e.processing_llm_tasks_total), 0) AS processing_llm_tasks_total,
            toString(e.processing_llm_last_task_at) AS processing_llm_last_task_at,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_started_at, e.queued_at, e.created_at)) AS queued_at,
            toString(e.processing_heartbeat_at) AS processing_heartbeat_at,
            e.processing_error AS processing_error,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_started_at, e.queued_at, e.created_at) ASC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _failed_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state = 'FAILED'
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_state AS processing_state,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_completed_at, e.processing_started_at, e.queued_at, e.created_at)) AS failed_at,
            e.processing_error AS processing_error,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_completed_at, e.processing_started_at, e.queued_at, e.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _recovered_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state = 'READY' AND toInteger(e.processing_attempts) > 1
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_stage AS processing_stage,
            coalesce(toFloat(e.processing_progress), 0.0) AS processing_progress,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_completed_at, e.created_at)) AS recovered_at,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_completed_at, e.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _successful_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state = 'READY' AND coalesce(toInteger(e.processing_attempts), 0) <= 1
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_stage AS processing_stage,
            coalesce(toFloat(e.processing_progress), 0.0) AS processing_progress,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_completed_at, e.created_at)) AS completed_at,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_completed_at, e.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _recent_sessions(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n)
        WHERE n.session_id IS NOT NULL
        RETURN
            n.session_id AS session_id,
            count(DISTINCT n) AS node_count,
            sum(CASE WHEN n:Episodic THEN 1 ELSE 0 END) AS episode_count,
            max(toString(n.created_at)) AS last_seen
        ORDER BY last_seen DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _flagged_nodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n)
        WHERE coalesce(n.user_flagged, false) = true
        RETURN
            n.uuid AS uuid,
            coalesce(n.name, n.uuid) AS name,
            labels(n) AS labels,
            n.scope AS scope,
            n.session_id AS session_id
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _candidates(repo: Neo4jRepository, limit: int = 50) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n:Entity)
        WHERE n.scope = 'CANDIDATE'
        RETURN n.uuid AS uuid, n.name AS name, n.content AS content, n.source AS source,
               n.candidate_cluster_id AS cluster_id, n.candidate_kind AS kind,
               n.candidate_type AS type, n.candidate_label AS label,
               n.candidate_evidence_strength AS evidence_strength,
               n.candidate_distinct_sessions AS distinct_sessions,
               n.candidate_first_seen AS first_seen, n.candidate_last_seen AS last_seen,
               n.candidate_notes AS notes, n.created_at AS created_at
        ORDER BY coalesce(n.candidate_last_seen, n.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": int(limit)},
    )


def _search_entities(repo: Neo4jRepository, query: str, limit: int = 25) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n:Entity)
        WHERE $search_term = ''
           OR toLower(coalesce(n.name, '')) CONTAINS toLower($search_term)
           OR toLower(coalesce(n.summary, '')) CONTAINS toLower($search_term)
        RETURN
            n.uuid AS uuid,
            coalesce(n.name, n.uuid) AS name,
            n.scope AS scope,
            n.session_id AS session_id,
            n.source AS source,
            coalesce(n.user_flagged, false) AS user_flagged,
            toString(n.last_accessed) AS last_accessed,
            COUNT { (n)-[]-() } AS connection_count,
            left(coalesce(n.summary, ''), 80) AS summary_preview
        ORDER BY connection_count DESC, coalesce(n.user_flagged, false) DESC, n.name ASC
        LIMIT $limit
        """,
        params={"search_term": query.strip(), "limit": limit},
    )


def _node_detail(repo: Neo4jRepository, uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        """
        MATCH (n {uuid: $uuid})
        OPTIONAL MATCH (n)-[r]-(neighbor)
        WITH n, collect(DISTINCT {
            uuid: neighbor.uuid,
            name: coalesce(neighbor.name, neighbor.uuid),
            labels: labels(neighbor),
            scope: neighbor.scope,
            rel_type: type(r)
        })[0..12] AS neighbors
        OPTIONAL MATCH (e:Episodic)-[:MENTIONS]-(n)
        WITH n, neighbors, collect(DISTINCT {
            uuid: e.uuid,
            name: coalesce(e.name, e.uuid),
            created_at: toString(e.created_at),
            session_id: e.session_id
        })[0..10] AS episodes
        RETURN {
            uuid: n.uuid,
            name: coalesce(n.name, n.uuid),
            labels: labels(n),
            scope: n.scope,
            session_id: n.session_id,
            user_id: n.user_id,
            source: n.source,
            user_flagged: coalesce(n.user_flagged, false),
            freshness: n.freshness,
            sharpness: n.sharpness,
            emotions: n.emotions,
            nodes_touched: n.enriched_nodes_touched,
            edges_touched: n.enriched_edges_touched,
            created_at: toString(n.created_at),
            last_accessed: toString(n.last_accessed),
            content: n.content,
            summary: n.summary,
            neighbors: [entry IN neighbors WHERE entry.uuid IS NOT NULL],
            episodes: [entry IN episodes WHERE entry.uuid IS NOT NULL]
        } AS detail
        """,
        params={"uuid": uuid},
    )
    if not rows:
        return None
    return rows[0]["detail"]


def _session_detail(repo: Neo4jRepository, session_id: str) -> dict[str, Any] | None:
    rows = repo.execute(
        """
        MATCH (n)
        WHERE n.session_id = $session_id
        RETURN
            $session_id AS session_id,
            count(DISTINCT n) AS node_count,
            sum(CASE WHEN n:Episodic THEN 1 ELSE 0 END) AS episode_count,
            collect(DISTINCT {
                uuid: n.uuid,
                name: coalesce(n.name, n.uuid),
                labels: labels(n),
                scope: n.scope,
                source: n.source
            })[0..30] AS nodes
        """,
        params={"session_id": session_id},
    )
    if not rows or rows[0]["node_count"] == 0:
        return None
    return rows[0]


def _graph_elements(repo: Neo4jRepository, uuid: str, depth: int) -> dict[str, list[dict[str, Any]]]:
    clamped_depth = max(1, min(depth, 2))
    node_query = f"""
        MATCH (seed {{uuid: $uuid}})
        OPTIONAL MATCH p=(seed)-[*1..{clamped_depth}]-(neighbor)
        WITH seed, collect(DISTINCT p) AS paths
        UNWIND CASE WHEN size(paths) = 0 THEN [null] ELSE paths END AS path
        UNWIND CASE WHEN path IS NULL THEN [seed] ELSE nodes(path) END AS node
        RETURN DISTINCT
            node.uuid AS uuid,
            coalesce(node.name, node.uuid) AS label,
            labels(node) AS labels,
            node.scope AS scope
    """
    edge_query = f"""
        MATCH (seed {{uuid: $uuid}})
        OPTIONAL MATCH p=(seed)-[*1..{clamped_depth}]-(neighbor)
        WITH collect(DISTINCT p) AS paths
        UNWIND CASE WHEN size(paths) = 0 THEN [null] ELSE paths END AS path
        UNWIND CASE WHEN path IS NULL THEN [] ELSE relationships(path) END AS rel
        RETURN DISTINCT
            rel.uuid AS uuid,
            type(rel) AS rel_type,
            startNode(rel).uuid AS source,
            endNode(rel).uuid AS target,
            rel.scope AS scope
    """
    node_rows = repo.execute(node_query, params={"uuid": uuid})
    edge_rows = repo.execute(edge_query, params={"uuid": uuid})
    return {
        "nodes": [
            {
                "data": {"id": row["uuid"], "label": row["label"], "scope": row["scope"] or "unknown"},
                "classes": _graph_node_class(row["labels"], row["scope"]),
            }
            for row in node_rows
            if row.get("uuid")
        ],
        "edges": [
            {
                "data": {
                    "id": row["uuid"] or f"{row['source']}::{row['target']}::{row['rel_type']}",
                    "source": row["source"],
                    "target": row["target"],
                    "label": row["rel_type"],
                    "scope": row["scope"] or "unknown",
                }
            }
            for row in edge_rows
            if row.get("source") and row.get("target")
        ],
    }


def _session_graph_elements(
    repo: Neo4jRepository,
    session_id: str,
) -> dict[str, list[dict[str, Any]]]:
    node_rows = repo.execute(
        """
        MATCH (n)
        WHERE n.session_id = $session_id
        RETURN DISTINCT
            n.uuid AS uuid,
            coalesce(n.name, n.uuid) AS label,
            labels(n) AS labels,
            n.scope AS scope
        ORDER BY label ASC
        """,
        params={"session_id": session_id},
    )
    edge_rows = repo.execute(
        """
        MATCH (a)-[r]-(b)
        WHERE a.session_id = $session_id AND b.session_id = $session_id
        RETURN DISTINCT
            r.uuid AS uuid,
            type(r) AS rel_type,
            a.uuid AS source,
            b.uuid AS target,
            r.scope AS scope
        """,
        params={"session_id": session_id},
    )
    return {
        "nodes": [
            {
                "data": {"id": row["uuid"], "label": row["label"], "scope": row["scope"] or "unknown"},
                "classes": _graph_node_class(row["labels"], row["scope"]),
            }
            for row in node_rows
            if row.get("uuid")
        ],
        "edges": [
            {
                "data": {
                    "id": row["uuid"] or f"{row['source']}::{row['target']}::{row['rel_type']}",
                    "source": row["source"],
                    "target": row["target"],
                    "label": row["rel_type"],
                    "scope": row["scope"] or "unknown",
                }
            }
            for row in edge_rows
            if row.get("source") and row.get("target")
        ],
    }
