"""Re-derive items flagged ``rederive_pending`` by an erasure (fail-closed recall repair).

Erasing a memory strips its episode uuid from shared fact edges and flags those edges and shared
entities. Until repaired, recall hides the flagged text. This job rewrites each flagged item from
its REMAINING source episodes (or deletes an unsupported fact edge) and clears the flag.

Privacy: this module never logs item text, source text, or erased content; failures record only
the exception class name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from menhir.infrastructure.observability import LlmUsageControlSignal

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


async def run_derived_rederive(graph_adapter: Any, llm: Any, *, limit: int = 25) -> dict:
    """Process up to ``limit`` pending DerivedRederiveRepair nodes, oldest first.

    ``llm`` is the scheduler's LLM adapter (``lifecycle_service.llm``); ``None`` leaves everything
    pending (flagged text stays hidden) and returns ``{"skipped": "no_llm"}``.
    """
    # UnavailableLLMAdapter (degraded startup) has no rederive method; skipping keeps repairs
    # pending instead of burning their attempts into 'failed'.
    if llm is None or not callable(getattr(llm, "rederive_from_sources", None)):
        return {"skipped": "no_llm"}

    counts = {
        "processed": 0, "rewritten": 0, "deleted": 0, "obsolete": 0,
        "cas_failed": 0, "errors": 0, "failed": 0,
    }
    repairs = await _call(graph_adapter.list_pending_derived_rederive, limit)
    for repair in repairs:
        repair_key = str(repair.get("repair_key") or "")
        kind = str(repair.get("item_kind") or "")
        item_uuid = str(repair.get("item_uuid") or "")
        if not repair_key or kind not in ("fact", "entity") or not item_uuid:
            continue
        counts["processed"] += 1

        item = await _call(graph_adapter.load_derived_rederive_item, kind, item_uuid)
        if item is None or not item.get("pending"):
            # Item deleted, or already repaired/cleared by another path.
            await _call(graph_adapter.mark_derived_rederive_status, repair_key, "obsolete")
            counts["obsolete"] += 1
            continue

        sources = list(item.get("sources") or [])
        cas = {
            "repair_key": repair_key,
            "item_kind": kind,
            "item_uuid": item_uuid,
            "flagged_at": item.get("flagged_at"),
            "episodes": list(item.get("episodes") or []),
        }

        if not sources:
            # Nothing remains to support the item. A fact edge is deleted; an entity keeps its
            # node (deletion is the erasure's own fenced decision) with an empty summary.
            if kind == "fact":
                applied = await _call(
                    graph_adapter.apply_derived_rederive, new_text=None, delete=True, **cas
                )
                outcome = "deleted"
            else:
                applied = await _call(
                    graph_adapter.apply_derived_rederive, new_text="", **cas
                )
                outcome = "rewritten"
        else:
            try:
                verdict = await llm.rederive_from_sources(
                    kind=kind,
                    name=str(item.get("name") or ""),
                    current_text=str(item.get("text") or ""),
                    sources=sources,
                )
                error_class = "no_verdict" if verdict is None else None
            except LlmUsageControlSignal:
                # A budget refusal is a decision, not a fault: stop, leave pending, no attempt.
                counts["stopped"] = "llm_usage_control"
                break
            except Exception as exc:  # error class only; never the message (may echo content)
                verdict = None
                error_class = type(exc).__name__
            if verdict is None:
                status = await _call(
                    graph_adapter.record_derived_rederive_failure,
                    repair_key, error_class or "error", MAX_ATTEMPTS,
                )
                counts["errors"] += 1
                if status == "failed":
                    counts["failed"] += 1
                    logger.warning(
                        "derived_rederive repair=%s failed after %d attempts", repair_key, MAX_ATTEMPTS
                    )
                continue
            decision, text = verdict
            if decision == "unsupported" and kind == "fact":
                applied = await _call(
                    graph_adapter.apply_derived_rederive, new_text=None, delete=True, **cas
                )
                outcome = "deleted"
            else:
                applied = await _call(
                    graph_adapter.apply_derived_rederive, new_text=text or "", **cas
                )
                outcome = "rewritten"

        if applied:
            counts[outcome] += 1
        else:
            # Re-flagged or stripped meanwhile (or gone): stays pending; next run re-reads it.
            counts["cas_failed"] += 1

    logger.info("derived_rederive: %s", counts)
    return counts
