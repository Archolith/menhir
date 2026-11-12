"""Compensating erase for Graphiti twins orphaned by an erase racing enrichment."""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _default_coordinator(graph_adapter: Any) -> Any:
    from menhir.services.erasure_coordinator import ErasureCoordinator

    return ErasureCoordinator(graph_adapter=graph_adapter)


def _erase_and_clear(graph_adapter: Any, episode_uuid: str, factory: Callable[[Any], Any] | None) -> None:
    coordinator = (factory or _default_coordinator)(graph_adapter)
    coordinator.erase_memory(episode_uuid)
    getattr(graph_adapter, "clear_erasure_compensation")(episode_uuid)


def compensate_erased_enrichment(
    graph_adapter: Any,
    episode_uuid: str,
    resolved_episode_uuid: str,
    *,
    coordinator_factory: Callable[[Any], Any] | None = None,
) -> dict:
    """Link the erased queue node to its twin, then erase it. Never raises."""
    try:
        linked = bool(
            getattr(graph_adapter, "link_erased_twin")(episode_uuid, resolved_episode_uuid)
        )
        if not linked:
            # A prior attempt may have linked it already; only proceed for the same twin.
            fetch = getattr(graph_adapter, "fetch_episode_processing", None)
            row = fetch(episode_uuid) if fetch is not None else None
            if not row or str(row.get("resolved_episode_uuid") or "") != resolved_episode_uuid:
                return {"linked": False, "erased": False}
        _erase_and_clear(graph_adapter, episode_uuid, coordinator_factory)
        return {"linked": True, "erased": True}
    except Exception as exc:
        logger.warning(
            "Erasure compensation failed episode_id=%s; sweep will retry",
            episode_uuid,
            exc_info=True,
        )
        return {"error": str(exc)}


def sweep_erased_twin_compensations(
    graph_adapter: Any,
    *,
    limit: int = 50,
    coordinator_factory: Callable[[Any], Any] | None = None,
) -> dict:
    """Retry pending compensations and backstop crashed, never-linked twins."""
    counts = {"retried": 0, "compensated": 0, "ambiguous": 0, "none": 0, "errors": 0}
    try:
        rows = getattr(graph_adapter, "list_erased_twin_compensations")(limit)
    except Exception:
        logger.warning("Listing erasure compensations failed", exc_info=True)
        rows = []
        counts["errors"] += 1
    for row in rows:
        try:
            _erase_and_clear(graph_adapter, str(row["uuid"]), coordinator_factory)
            counts["retried"] += 1
        except Exception:
            counts["errors"] += 1
            logger.warning("Erasure compensation retry failed episode_id=%s", row.get("uuid"), exc_info=True)

    try:
        unlinked = getattr(graph_adapter, "list_unlinked_erased_queue_nodes")(limit)
    except Exception:
        logger.warning("Listing unlinked erased queue nodes failed", exc_info=True)
        unlinked = []
        counts["errors"] += 1
    for uuid in unlinked:
        try:
            found = getattr(graph_adapter, "find_unlinked_erased_twin")(uuid)
            if found.get("match"):
                # Linked meanwhile to another twin: leave it to the pending-compensation retry.
                result = compensate_erased_enrichment(
                    graph_adapter, uuid, found["match"], coordinator_factory=coordinator_factory
                )
                if result.get("error"):
                    counts["errors"] += 1
                elif result.get("erased"):
                    getattr(graph_adapter, "mark_compensation_checked")(uuid, "compensated")
                    counts["compensated"] += 1
                else:
                    getattr(graph_adapter, "mark_compensation_checked")(uuid, "link_conflict")
                    counts["ambiguous"] += 1
            elif found.get("ambiguous"):
                logger.warning(
                    "Ambiguous unlinked twin for erased episode_id=%s candidates=%s",
                    uuid,
                    found["ambiguous"],
                )
                getattr(graph_adapter, "mark_compensation_checked")(uuid, "ambiguous")
                counts["ambiguous"] += 1
            else:
                getattr(graph_adapter, "mark_compensation_checked")(uuid, "none")
                counts["none"] += 1
        except Exception:
            counts["errors"] += 1
            logger.warning("Unlinked twin backstop failed episode_id=%s", uuid, exc_info=True)
    return counts
