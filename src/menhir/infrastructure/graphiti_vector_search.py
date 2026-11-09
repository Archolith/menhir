"""Index-backed node and edge similarity search for Graphiti's Neo4j driver (#574).

Graphiti routes all twelve of its search primitives through ``driver.search_interface`` when one
is set. ``MenhirSearchInterface`` answers the two vector searches from a Neo4j vector index when
that is safe (see ``vector_indexes``) and hands everything else -- and every vector search it
cannot answer from the index -- to Graphiti's stock functions through a driver view with no
search interface, so the stock path runs exactly as it does without the hook.
"""

from __future__ import annotations

import logging
from typing import Any

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.search_interface.search_interface import SearchInterface
from graphiti_core.edges import get_entity_edge_from_record
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query
from graphiti_core.models.nodes.node_db_queries import get_entity_node_return_query
from graphiti_core.nodes import get_entity_node_from_record
from graphiti_core.search import search_utils
from graphiti_core.search.search_filters import (
    edge_search_filter_query_constructor,
    node_search_filter_query_constructor,
)
from pydantic import PrivateAttr

from menhir.infrastructure.vector_indexes import (
    ENTITY_NAME_EMBEDDING,
    RELATES_TO_FACT_EMBEDDING,
    SHOW_VECTOR_INDEXES,
    VectorIndexCatalog,
    VectorIndexSpec,
    VectorIndexStatus,
    ann_call,
    ann_result_complete,
    ann_stats,
    ann_stats_query,
    initial_k,
    is_vector_index_query_error,
    next_k,
)

logger = logging.getLogger(__name__)


class _StockDriverView:
    """The wrapped driver with ``search_interface = None``, so stock search code cannot re-enter
    the hook (it checks that attribute first) and runs unchanged."""

    search_interface = None

    def __init__(self, driver: Any) -> None:
        object.__setattr__(self, "_driver", driver)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)


def _stock(name: str) -> Any:
    async def method(self: Any, driver: Any, *args: Any, **kwargs: Any) -> Any:
        return await getattr(search_utils, name)(_StockDriverView(driver), *args, **kwargs)

    method.__name__ = name
    return method


def _where(conditions: list[str]) -> str:
    return " WHERE " + " AND ".join(conditions) if conditions else ""


def _edge_filter_is_selective(search_filter: Any) -> bool:
    """Filters that pin the result to a few known rows. The scan is exact and cheap there, while
    ANN oversampling would rarely reach them (edge dedup's endpoint-pair search is one)."""
    return bool(
        getattr(search_filter, "edge_uuids", None)
        or getattr(search_filter, "edge_source_node_uuids", None)
        or getattr(search_filter, "edge_target_node_uuids", None)
    )


class MenhirSearchInterface(SearchInterface):
    _catalog: VectorIndexCatalog = PrivateAttr(default_factory=VectorIndexCatalog)

    edge_fulltext_search = _stock("edge_fulltext_search")
    node_fulltext_search = _stock("node_fulltext_search")
    episode_fulltext_search = _stock("episode_fulltext_search")
    edge_bfs_search = _stock("edge_bfs_search")
    node_bfs_search = _stock("node_bfs_search")
    community_fulltext_search = _stock("community_fulltext_search")
    community_similarity_search = _stock("community_similarity_search")
    get_embeddings_for_communities = _stock("get_embeddings_for_communities")
    node_distance_reranker = _stock("node_distance_reranker")
    episode_mentions_reranker = _stock("episode_mentions_reranker")

    @property
    def catalog(self) -> VectorIndexCatalog:
        return self._catalog

    async def _usable(self, driver: Any, spec: VectorIndexSpec, vector: list[float]) -> VectorIndexStatus | None:
        if getattr(driver, "provider", None) != GraphProvider.NEO4J:
            return None
        catalog = self._catalog
        if catalog.needs_refresh():
            try:
                records, _, _ = await driver.execute_query(SHOW_VECTOR_INDEXES, routing_="r")
                catalog.update([record.data() for record in records])
            except Exception as exc:
                logger.warning("Reading vector index state failed; similarity search will scan: %s", exc)
                catalog.mark_unavailable()
        return catalog.usable(spec, len(vector))

    async def _index_search(
        self,
        driver: Any,
        spec: VectorIndexSpec,
        status: VectorIndexStatus,
        vector: list[float],
        query: str,
        filter_params: dict[str, Any],
        limit: int,
        min_score: float,
    ) -> list[Any] | None:
        """Records from the index, or None when the caller must scan."""
        k = initial_k(limit)
        try:
            while True:
                params = {"index_name": status.name, "k": k, "search_vector": vector}
                stats, _, _ = await driver.execute_query(
                    ann_stats_query(spec, "search_vector"), routing_="r", **params
                )
                raw, lowest = ann_stats(stats[0].data() if stats else None)
                records: list[Any] = []
                if raw:
                    found, _, _ = await driver.execute_query(
                        query, min_score=min_score, limit=limit, routing_="r", **params, **filter_params
                    )
                    records = list(found)
                if ann_result_complete(
                    raw_count=raw,
                    k=k,
                    lowest_index_score=lowest,
                    exact_scores=[float(r["score"]) for r in records],
                    limit=limit,
                    min_score=min_score,
                    margin=status.score_margin,
                ):
                    return records
                bigger = next_k(k)
                if bigger is None:
                    return None
                k = bigger
        except Exception as exc:
            if not is_vector_index_query_error(exc):
                raise
            logger.warning("Vector index %s query failed; scanning instead: %s", status.name, exc)
            self._catalog.invalidate()
            return None

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = search_utils.RELEVANT_SCHEMA_LIMIT,
        min_score: float = search_utils.DEFAULT_MIN_SCORE,
    ) -> list[Any]:
        stock = _StockDriverView(driver)
        status = None
        if group_ids != [] and search_vector:
            status = await self._usable(driver, ENTITY_NAME_EMBEDDING, search_vector)
        if status is not None:
            filter_queries, filter_params = node_search_filter_query_constructor(search_filter, driver.provider)
            if group_ids is not None:
                filter_queries.append("n.group_id IN $group_ids")
                filter_params["group_ids"] = group_ids
            query = (
                ann_call(ENTITY_NAME_EMBEDDING, "n", "search_vector")
                + " WITH n"
                + _where(filter_queries)
                + " WITH n, vector.similarity.cosine(n.name_embedding, $search_vector) AS score "
                "WHERE score > $min_score RETURN "
                + get_entity_node_return_query(driver.provider)
                + ", score ORDER BY score DESC LIMIT $limit"
            )
            records = await self._index_search(
                driver, ENTITY_NAME_EMBEDDING, status, search_vector, query, filter_params, limit, min_score
            )
            if records is not None:
                return [get_entity_node_from_record(r, driver.provider) for r in records]
        return await search_utils.node_similarity_search(
            stock, search_vector, search_filter, group_ids, limit, min_score
        )

    async def edge_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = search_utils.RELEVANT_SCHEMA_LIMIT,
        min_score: float = search_utils.DEFAULT_MIN_SCORE,
    ) -> list[Any]:
        stock = _StockDriverView(driver)
        # Stock applies the endpoint arguments only when group_ids is set; keep that.
        endpoint_args = group_ids is not None and (source_node_uuid is not None or target_node_uuid is not None)
        status = None
        if group_ids != [] and search_vector and not endpoint_args and not _edge_filter_is_selective(search_filter):
            status = await self._usable(driver, RELATES_TO_FACT_EMBEDDING, search_vector)
        if status is not None:
            filter_queries, filter_params = edge_search_filter_query_constructor(search_filter, driver.provider)
            if group_ids is not None:
                filter_queries.append("e.group_id IN $group_ids")
                filter_params["group_ids"] = group_ids
            query = (
                ann_call(RELATES_TO_FACT_EMBEDDING, "e", "search_vector")
                # e is bound: this projects its endpoints, it does not search.
                + " MATCH (n:Entity)-[e]->(m:Entity)"
                + _where(filter_queries)
                + " WITH DISTINCT e, n, m, vector.similarity.cosine(e.fact_embedding, $search_vector) AS score "
                "WHERE score > $min_score RETURN "
                + get_entity_edge_return_query(driver.provider)
                + ", score ORDER BY score DESC LIMIT $limit"
            )
            records = await self._index_search(
                driver, RELATES_TO_FACT_EMBEDDING, status, search_vector, query, filter_params, limit, min_score
            )
            if records is not None:
                return [get_entity_edge_from_record(r, driver.provider) for r in records]
        return await search_utils.edge_similarity_search(
            stock, search_vector, source_node_uuid, target_node_uuid, search_filter, group_ids, limit, min_score
        )
