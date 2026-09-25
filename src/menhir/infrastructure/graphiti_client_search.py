"""Search-side implementation for :mod:`menhir.infrastructure.graphiti_client`.

The four async functions below are the ``GraphitiClient`` search methods, moved verbatim and
bound onto the class as methods (see the assignments in the ``GraphitiClient`` class body), so
their signatures, docstrings and behavior are identical to the pre-split implementation.

Module globals these functions reference resolve in THIS module: the logger deliberately keeps
the original ``graphiti_client`` logger name so log records emitted from the moved search
methods are unchanged, and ``_is_vector_dimension_mismatch_error`` moved here with its callers
(still importable from ``graphiti_client``, which re-exports it).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


#: Preserves the pre-split logger name for records emitted by the moved search methods.
logger = logging.getLogger("menhir.infrastructure.graphiti_client")


def _is_vector_dimension_mismatch_error(exc: Exception) -> bool:
    """Return True when Neo4j/Graphiti failed due to mixed embedding dimensions."""

    text = str(exc).lower()
    return (
        "vector.similarity.cosine" in text
        and "dimension" in text
    ) or "do not have the same number of dimensions" in text


async def search_scored(
    self,
    query: str,
    *,
    num_results: int = 50,
    group_ids: list[str] | None = None,
) -> list[tuple[str, str, float]]:
    """Vector + fulltext search returning entity nodes with similarity scores.

    Returns a list of (node_uuid, node_name, similarity_score) tuples.
    Episode nodes are excluded: Graphiti's SearchResults.nodes is typed
    list[EntityNode] (separate from .episodes), and we additionally skip
    any node whose labels include 'Episodic' as a defensive check.
    """
    from graphiti_core.search.search_config import (
        NodeSearchConfig,
        NodeSearchMethod,
        NodeReranker,
        SearchConfig,
    )

    config = SearchConfig(
        node_config=NodeSearchConfig(
            search_methods=[
                NodeSearchMethod.bm25,
                NodeSearchMethod.cosine_similarity,
            ],
            reranker=NodeReranker.rrf,
        ),
        limit=num_results,
    )
    try:
        results = await self.client.search_(
            query,
            config,
            group_ids=group_ids,
        )
    except Exception as exc:
        if _is_vector_dimension_mismatch_error(exc):
            logger.warning(
                "Graphiti search_scored falling back to bm25-only after vector dimension mismatch: %s",
                exc,
            )
            fallback_config = SearchConfig(
                node_config=NodeSearchConfig(
                    search_methods=[NodeSearchMethod.bm25],
                    reranker=NodeReranker.rrf,
                ),
                limit=num_results,
            )
            results = await self.client.search_(
                query,
                fallback_config,
                group_ids=group_ids,
            )
        else:
            logger.error(
                "Graphiti fused node search failed query=%r; retrying isolated BM25/cosine lanes: %s: %s",
                query,
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )
            ranked = await self.search_ranked_by_method(
                query,
                methods=["bm25", "cosine_similarity"],
                num_results=num_results,
                group_ids=group_ids,
            )
            from graphiti_core.search.search_utils import rrf

            lane_order = ["bm25", "cosine_similarity"]
            names = {
                uuid: name
                for method in lane_order
                for uuid, name in ranked.get(method, [])
            }
            uuids, scores = rrf([
                [uuid for uuid, _name in ranked.get(method, [])]
                for method in lane_order
                if ranked.get(method)
            ])
            return [
                (uuid, names.get(uuid, uuid), float(score))
                for uuid, score in zip(uuids[:num_results], scores[:num_results])
            ]

    scored: list[tuple[str, str, float]] = []
    for node, score in zip(results.nodes, results.node_reranker_scores):
        try:
            if "Episodic" in getattr(node, "labels", []):
                continue
            uuid = str(node.uuid or "").strip()
            if not uuid:
                raise ValueError("candidate node has no uuid")
            scored.append((uuid, str(node.name or uuid), float(score)))
        except Exception as exc:
            logger.error(
                "Graphiti search_scored skipped malformed result uuid=%r name=%r: %s: %s",
                getattr(node, "uuid", None),
                getattr(node, "name", None),
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )

    return scored


async def count_similar_by_cosine(
    self,
    query: str,
    *,
    exclude_uuid: str,
    min_cosine: float,
    limit: int = 50,
    group_ids: list[str] | None = None,
) -> int:
    """Count DISTINCT entity nodes whose true cosine similarity to `query` is > min_cosine.

    Cosine-only search with sim_min_score as a genuine cosine floor (applied in the vector
    search, strict `>`, before reranking). Returns the count of qualifying neighbors — distinct
    by uuid, excluding `exclude_uuid` and Episodic nodes. Returns -1 if the similarity search is
    unavailable (advisory, not critical — mirrors the -1 contract _count_similar_nodes has today).

    The cap (limit+1 requested, up to limit counted) is intentional: compute_sharpness =
    1/(1+count) saturates below 0.1 by ~9 neighbors and below 0.2 by ~4, so any cap >= ~10
    cannot change a decision — the cap is immaterial to the gates.
    """
    from graphiti_core.search.search_config import (
        NodeSearchConfig,
        NodeSearchMethod,
        NodeReranker,
        SearchConfig,
    )

    config = SearchConfig(
        node_config=NodeSearchConfig(
            search_methods=[NodeSearchMethod.cosine_similarity],
            reranker=NodeReranker.rrf,
            sim_min_score=min_cosine,
        ),
        limit=limit + 1,
    )
    try:
        results = await self.client.search_(
            query,
            config,
            group_ids=group_ids,
        )
    except Exception as exc:
        # Any exception (including vector-dimension-mismatch) returns -1.
        # A dimension mismatch means the cosine index is unusable and there is no
        # lawful similarity signal. No fallback to BM25 — lexical count is not a
        # uniqueness measure and would reintroduce the scale mismatch.
        logger.warning(
            "Graphiti count_similar_by_cosine failed (cosine index unavailable): %s",
            exc,
        )
        return -1

    # Collect distinct uuids, excluding self and Episodic nodes
    seen_uuids: set[str] = set()
    for node in results.nodes:
        if "Episodic" in getattr(node, "labels", []):
            continue
        if node.uuid == exclude_uuid:
            continue
        seen_uuids.add(node.uuid)

    return len(seen_uuids)


async def search_edges_scored(
    self,
    query: str,
    *,
    num_results: int = 20,
    group_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search over RELATES_TO fact EDGES, returning dated facts with scores.

    The entity-node search paths (:meth:`search_scored`,
    :meth:`search_ranked_by_method`) return entity *names*; this returns the
    *facts* that connect entities — ``EntityEdge.fact`` strings like
    "the dealership replaced the GPS system on 2023-03-22". For "what
    happened" queries the answer lives on the edge, not the node, so this is
    the candidate source node search structurally cannot surface.

    Returns a list of dicts in best-first order, each with keys ``uuid``,
    ``fact``, ``score`` (edge RRF reranker score) and the edge's bitemporal
    anchors ``created_at`` / ``valid_at`` / ``invalid_at`` / ``expired_at``
    (ISO-8601 strings or ``None``). Those anchors are the edge's advantage
    over entity nodes — the TemporalOracle scores on them — so the recall
    path threads them into the oracle metadata. Edges with an empty ``fact``
    are skipped. Mirrors :meth:`search_scored`'s BM25-only fallback on a
    vector-dimension mismatch.
    """
    from graphiti_core.search.search_config import (
        EdgeSearchConfig,
        EdgeSearchMethod,
        EdgeReranker,
        SearchConfig,
    )

    config = SearchConfig(
        edge_config=EdgeSearchConfig(
            search_methods=[
                EdgeSearchMethod.bm25,
                EdgeSearchMethod.cosine_similarity,
            ],
            reranker=EdgeReranker.rrf,
        ),
        limit=num_results,
    )
    try:
        results = await self.client.search_(query, config, group_ids=group_ids)
    except Exception as exc:
        if not _is_vector_dimension_mismatch_error(exc):
            raise
        logger.warning(
            "Graphiti search_edges_scored falling back to bm25-only after vector dimension mismatch: %s",
            exc,
        )
        fallback_config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bm25],
                reranker=EdgeReranker.rrf,
            ),
            limit=num_results,
        )
        results = await self.client.search_(query, fallback_config, group_ids=group_ids)

    def _iso(dt: Any) -> str | None:
        return dt.isoformat() if dt is not None else None

    scored: list[dict[str, Any]] = []
    for edge, score in zip(results.edges, results.edge_reranker_scores):
        fact = (getattr(edge, "fact", None) or "").strip()
        if not fact:
            continue
        scored.append(
            {
                "uuid": edge.uuid,
                "fact": fact,
                "score": score,
                "created_at": _iso(getattr(edge, "created_at", None)),
                "valid_at": _iso(getattr(edge, "valid_at", None)),
                "invalid_at": _iso(getattr(edge, "invalid_at", None)),
                "expired_at": _iso(getattr(edge, "expired_at", None)),
                # Endpoint entity nodes: the RICH candidates the edge points at (a
                # dated fact edge connects e.g. "dealership" -> "GPS system", whose
                # node summaries carry the answer with surrounding context). Used by
                # the "pointer" fact-edge mode to hydrate node context instead of
                # injecting the terse fact as a standalone (context-shredding) answer.
                "source_node_uuid": getattr(edge, "source_node_uuid", None),
                "target_node_uuid": getattr(edge, "target_node_uuid", None),
            }
        )

    return scored


async def search_ranked_by_method(
    self,
    query: str,
    *,
    methods: list[str],
    num_results: int = 50,
    group_ids: list[str] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Run each search method as its own pass; return rank-ordered hits per method.

    Unlike :meth:`search_scored` -- which fuses BM25 + cosine into one opaque
    RRF score and discards which method found what -- this keeps each method
    separate so the caller can attribute a candidate's source and blend on
    rank (see ``services/hybrid_retrieval.py``).

    Returns a dict keyed by the method name (``"cosine_similarity"`` /
    ``"bm25"``); each value is a list of ``(node_uuid, node_name)`` tuples in
    best-first rank order. Episode nodes are excluded, as in
    :meth:`search_scored`. If the cosine pass hits a vector-dimension
    mismatch its result is an empty list (BM25 is unaffected); any other
    method's mismatch is not silently swallowed.
    """
    from graphiti_core.search.search_filters import SearchFilters
    from graphiti_core.search.search_utils import (
        node_fulltext_search,
        node_similarity_search,
    )

    supported = {"bm25", "cosine_similarity"}
    effective_group_ids = group_ids if group_ids and group_ids != [""] else None
    search_filter = SearchFilters()

    # CF-175: the lanes are independent -- each writes its own `ranked`/`failures` key, the
    # query vector is read only by the cosine branch, and neither reads the other's output --
    # so total latency was bm25 + embed + cosine where it only had to be
    # max(bm25, embed + cosine). Running them concurrently is the whole fix.
    #
    # Validation is hoisted OUT of the lanes on purpose. Serially, an unsupported method
    # raised only after every earlier lane had already run its query; concurrently there is no
    # "earlier", so the check has to happen before anything is launched or the ValueError
    # would race the work it is meant to prevent. Production callers pass a fixed
    # {"bm25", "cosine_similarity"} list, so this moves the raise earlier for nobody.
    for method in methods:
        if method not in supported:
            raise ValueError(f"Unsupported search method: {method!r}")

    ranked: dict[str, list[tuple[str, str]]] = {}
    failures: dict[str, Exception] = {}

    async def _run_lane(method: str) -> None:
        try:
            if method == "bm25":
                nodes = await node_fulltext_search(
                    self.client.driver,
                    query,
                    search_filter,
                    effective_group_ids,
                    2 * num_results,
                )
            else:
                query_vector = await self.embed_query(query)
                nodes = await node_similarity_search(
                    self.client.driver,
                    query_vector,
                    search_filter,
                    effective_group_ids,
                    2 * num_results,
                )
        except Exception as exc:
            if method == "cosine_similarity" and _is_vector_dimension_mismatch_error(exc):
                logger.warning(
                    "Graphiti cosine pass skipped after vector dimension mismatch: %s", exc
                )
                ranked[method] = []
                failures[method] = exc
                return
            logger.error(
                "Graphiti %s lane failed query=%r; other retrieval lanes will continue: %s: %s",
                method,
                query,
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )
            ranked[method] = []
            failures[method] = exc
            return

        hits: list[tuple[str, str]] = []
        for node in nodes:
            try:
                if "Episodic" in getattr(node, "labels", []):
                    continue
                uuid = str(node.uuid or "").strip()
                if not uuid:
                    raise ValueError("candidate node has no uuid")
                hits.append((uuid, str(node.name or uuid)))
            except Exception as exc:
                logger.error(
                    "Graphiti %s lane skipped malformed result uuid=%r name=%r: %s: %s",
                    method,
                    getattr(node, "uuid", None),
                    getattr(node, "name", None),
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )
        ranked[method] = hits

    # `_run_lane` swallows its own failures into `failures`, so nothing here can raise and
    # `return_exceptions` would only hide a genuine bug in the lane body.
    await asyncio.gather(*(_run_lane(method) for method in methods))

    if methods and len(failures) == len(methods):
        detail = ", ".join(
            f"{method}={exc.__class__.__name__}" for method, exc in failures.items()
        )
        raise RuntimeError(f"all requested Graphiti search lanes failed ({detail})")

    return ranked
