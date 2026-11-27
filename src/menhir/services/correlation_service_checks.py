"""Search-driven correlation checks: the one scan-one-node loop and the enrichment entry point.

``scan_node`` is the only loop that walks one node's similarity hits through
``CorrelationService.classify_pair``. Enrichment reaches it through ``check_correlation`` (a
method of the mixin ``CorrelationService`` inherits) and consolidation through
``LifecycleConsolidationMixin._check_contradictions_batch``. Routing, deterministic vetoes, and the
judge-gated merge decision stay on the facade (SSOT-03).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from menhir.infrastructure.graph_operations_model import SagaWritesPausedError
from menhir.infrastructure.observability import LlmUsageControlSignal
from menhir.services.correlation_service_types import CorrelationBatchResult, CorrelationResult

if TYPE_CHECKING:
    from menhir.infrastructure.graphiti_client import GraphitiClient

logger = logging.getLogger(__name__)

#: ``classify_pair(source_uuid, target_uuid, rrf_score) -> (final_action, details)``.
PairClassifier = Callable[[str, str, float], Awaitable[tuple[str, "dict[str, Any] | None"]]]
#: Called with each conflict result; returns True to end the node's scan there.
ConflictHandler = Callable[[CorrelationResult], Awaitable[bool]]


async def scan_node(
    classify: PairClassifier,
    search_client: Any,
    node_uuid: str,
    query: str,
    *,
    group_ids: list[str] | None,
    num_results: int,
    stop_on_drop: bool,
    exclude_uuids: set[str] | None = None,
    on_conflict: ConflictHandler | None = None,
    errors: list[str] | None = None,
) -> CorrelationBatchResult:
    """Search for ``node_uuid``'s neighbours and classify each hit in rank order (#871).

    Namespace rule: ``group_ids`` is the search filter as given. The caller resolves it from the
    node's namespace; ``None`` searches every group. Nothing here defaults or widens it.

    Stop rules, in the order they apply to a hit:

    - ``merged`` always ends the scan: ``classify_pair`` absorbs ``node_uuid`` into the hit, so the
      scanned node no longer exists and any later hit would be classified for a deleted node.
    - ``none`` with details is a drop (similarity floor, shared-episode veto, auto-merge off or
      proposed). It ends the scan when ``stop_on_drop``, as a caller that stops at a match must.
    - ``conflict`` ends the scan when ``on_conflict`` returns True. With no handler every conflict
      is collected, so the caller can fall back to a later pair.
    - ``related`` and plain ``none`` go on to the next hit.

    Errors: a failed search is logged and returns ``skipped=1``. With ``errors`` given, a failed
    search or ``classify_pair`` appends ``node_uuid`` and ends the scan, so the caller can leave the
    node for its next pass (#938, #1008); without it a ``classify_pair`` failure raises. Usage-limit
    and saga-pause signals always raise.
    """
    if not query.strip():
        return CorrelationBatchResult(checked=1, skipped=1)

    search_kwargs: dict[str, Any] = {"num_results": num_results}
    if group_ids is not None:
        search_kwargs["group_ids"] = group_ids
    try:
        similar = await search_client.search_scored(query, **search_kwargs)
    except Exception:  # noqa: BLE001 -- the check is best-effort; the caller decides what a miss means
        logger.warning("Correlation search failed for node=%s", node_uuid, exc_info=True)
        if errors is not None:
            errors.append(node_uuid)
        return CorrelationBatchResult(checked=1, skipped=1)

    exclude = (exclude_uuids or set()) | {node_uuid}
    result = CorrelationBatchResult(checked=1)
    for other_uuid, _other_name, score in similar:
        if other_uuid in exclude:
            continue

        try:
            final_action, details = await classify(node_uuid, other_uuid, score)
        except (LlmUsageControlSignal, SagaWritesPausedError):
            raise
        except Exception as exc:
            if errors is None:
                raise
            logger.warning(
                "classify_pair failed for %s <-> %s (%s); candidate left for the next pass",
                node_uuid, other_uuid, type(exc).__name__, exc_info=True,
            )
            errors.append(node_uuid)
            break

        if final_action == "none":
            if details is not None and stop_on_drop:
                break
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

        if final_action == "merged":
            break
        if final_action == "conflict" and on_conflict is not None and await on_conflict(cr):
            break

    return result


class CorrelationChecksMixin:
    """Namespace-scoped correlation searches; mixed into ``CorrelationService``."""

    _graphiti: "GraphitiClient"
    classify_pair: PairClassifier

    async def check_correlation(
        self,
        node_uuid: str,
        query: str,
        *,
        exclude_uuids: set[str] | None = None,
        num_results: int = 10,
        namespace: str | None = None,
    ) -> CorrelationBatchResult:
        """Check a single node for correlation with existing entities (enrichment).

        Every conflict is collected (the caller queues the first that writes); a merge ends the
        scan. ``namespace`` scopes the search; ``None`` or empty searches every group.

        Args:
            node_uuid: The UUID of the node being checked.
            query: The text to search for similar entities.
            exclude_uuids: Set of UUIDs to exclude from results.
            num_results: Maximum number of similar entities to return.
            namespace: Optional namespace to scope the search.
        """
        group_ids: list[str] | None = None
        if namespace:
            from menhir.domain.namespace import namespace_to_group_ids
            group_ids = namespace_to_group_ids(namespace)
        return await scan_node(
            self.classify_pair, self._graphiti, node_uuid, query,
            group_ids=group_ids, num_results=num_results, stop_on_drop=False,
            exclude_uuids=exclude_uuids,
        )
