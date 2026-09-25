"""Search-driven correlation checks for the correlation service (facade split).

The single-node and batch check methods moved verbatim from
``correlation_service.py`` onto a mixin that ``CorrelationService`` inherits, so
they remain bound methods with unchanged signatures. Routing, deterministic
vetoes, and the judge-gated merge decision stay on the facade (SSOT-03).
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.services.correlation_service_types import CorrelationBatchResult, CorrelationResult

logger = logging.getLogger(__name__)


class CorrelationChecksMixin:
    """Namespace-scoped correlation searches; mixed into ``CorrelationService``."""

    # ------------------------------------------------------------------
    # Single-node correlation check (used during enrichment)
    # ------------------------------------------------------------------

    async def check_correlation(
        self,
        node_uuid: str,
        query: str,
        *,
        exclude_uuids: set[str] | None = None,
        num_results: int = 10,
        namespace: str | None = None,
    ) -> CorrelationBatchResult:
        """Check a single node for correlation with existing entities.

        Called during enrichment after new nodes are committed to Neo4j.
        The ``query`` is typically the node's content or summary, used
        for the similarity search.

        Args:
            node_uuid: The UUID of the node being checked.
            query: The text to search for similar entities.
            exclude_uuids: Set of UUIDs to exclude from results.
            num_results: Maximum number of similar entities to return.
            namespace: Optional namespace to scope the search (for deterministic consistency).
        """
        if not query.strip():
            return CorrelationBatchResult(checked=1, skipped=1)

        try:
            # Namespace-scoped search (Part 1, deterministic veto: verify namespace-scoped)
            search_kwargs = {"num_results": num_results}
            if namespace:
                from menhir.domain.namespace import namespace_to_group_ids
                search_kwargs["group_ids"] = namespace_to_group_ids(namespace)

            similar = await self._graphiti.search_scored(query, **search_kwargs)
        except Exception:
            logger.warning(
                "Correlation search failed for node=%s", node_uuid, exc_info=True
            )
            return CorrelationBatchResult(checked=1, skipped=1)

        exclude = (exclude_uuids or set()) | {node_uuid}
        result = CorrelationBatchResult(checked=1)

        for other_uuid, other_name, score in similar:
            if other_uuid in exclude:
                continue

            final_action, details = await self.classify_pair(node_uuid, other_uuid, score)
            if final_action == "none":
                continue

            if final_action == "related":
                if details and details.get("created"):
                    result.related += 1
            elif final_action == "merged":
                result.merged += 1
            elif final_action == "conflict":
                result.conflicts += 1

            cr = CorrelationResult(
                action=final_action,
                source_uuid=node_uuid,
                target_uuid=other_uuid,
                similarity=score,
                details=details,
            )
            if result.results is not None:
                result.results.append(cr)

        return result

    # ------------------------------------------------------------------
    # Batch correlation check (NO production caller -- see the docstring)
    # ------------------------------------------------------------------

    async def check_correlation_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        namespace: str | None = None,
    ) -> CorrelationBatchResult:
        """Check a batch of promotion candidates for correlations.

        Returns results segmented by action so the caller can route
        conflicts to the conflict pipeline and handle merges/edges
        independently.

        **This method has no production caller.** The banner and docstring previously said it was
        "used during consolidation/promotion"; nothing in consolidation or promotion reaches it,
        and the only callers in the corpus are in ``tests/test_correlation_service.py``. That
        claim mattered because anyone wiring this up would reasonably have believed it had already
        been exercised in that role -- with an UNSCOPED search behind it.

        Args:
            candidates: Promotion candidates. Each may carry its own ``namespace``.
            namespace: Scopes the search for every candidate. A candidate's own ``namespace`` key
                is used when this is not supplied, because a batch can span namespaces and pinning
                the whole batch to one would be wrong.

        Scoping matters here specifically: results feed ``classify_pair()`` and merge proposal
        handling, so an unscoped hit is a cross-namespace merge, which is permanent.

        Known residual, shared with ``check_correlation`` and NOT introduced here: when no
        namespace can be resolved the search is global rather than refused. Making that path
        fail closed is a change to both methods and to their existing tests, so it is deliberately
        not made under this finding.
        """
        batch_result = CorrelationBatchResult(checked=len(candidates))

        for node in candidates:
            uuid = str(node["uuid"])
            query = str(node.get("content") or node.get("name") or "")
            if not query.strip():
                batch_result.skipped += 1
                continue

            # Scoped the way `check_correlation` scopes it (see the search there). A batch can
            # span namespaces, so resolve per candidate rather than once for the batch.
            node_namespace = namespace or node.get("namespace")
            search_kwargs: dict[str, Any] = {"num_results": 5}
            if node_namespace:
                from menhir.domain.namespace import namespace_to_group_ids
                search_kwargs["group_ids"] = namespace_to_group_ids(str(node_namespace))

            try:
                similar = await self._graphiti.search_scored(query, **search_kwargs)
            except Exception:
                logger.warning(
                    "Correlation search failed for node=%s", uuid, exc_info=True
                )
                batch_result.skipped += 1
                continue

            for other_uuid, other_name, score in similar:
                if other_uuid == uuid:
                    continue

                final_action, details = await self.classify_pair(uuid, other_uuid, score)
                if final_action == "none":
                    continue

                if final_action == "related":
                    if details and details.get("created"):
                        batch_result.related += 1
                elif final_action == "merged":
                    batch_result.merged += 1
                elif final_action == "conflict":
                    batch_result.conflicts += 1

                cr = CorrelationResult(
                    action=final_action,
                    source_uuid=uuid,
                    target_uuid=other_uuid,
                    similarity=score,
                    details=details,
                )
                if batch_result.results is not None:
                    batch_result.results.append(cr)

                # Only process one match per node to avoid cascading effects
                break

        return batch_result
