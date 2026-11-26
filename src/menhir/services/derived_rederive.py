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

from menhir.infrastructure.circuit_breaker import provider_unavailable_reason, should_trip_circuit
from menhir.infrastructure.observability import LlmUsageControlSignal
from menhir.infrastructure.openai_rate_limit import find_provider_status_error, rate_limit_category
from menhir.infrastructure.telemetry import record_failure_event

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


def _is_provider_fault(exc: Exception) -> bool:
    """True when a failed rederive call says nothing about the item: timeout, connection, 429/5xx,
    open breaker, billing, rejected credentials, an unknown model, or an unimplemented backend."""
    if isinstance(exc, NotImplementedError):
        return True
    if should_trip_circuit(exc) or rate_limit_category(exc) is not None:
        return True
    if provider_unavailable_reason(exc) is not None:
        return True
    status = getattr(find_provider_status_error(exc), "status_code", None)
    return status in (401, 403, 404)


async def _defer(graph_adapter: Any, repair_key: str) -> None:
    """Best-effort: move a repair behind the queue (no attempt spent). Old adapters lack it."""
    defer = getattr(graph_adapter, "defer_derived_rederive", None)
    if defer is None:
        return
    try:
        await _call(defer, repair_key)
    except Exception:
        logger.warning("derived_rederive repair=%s: could not defer", repair_key)


async def _embed_or_none(embedder: Any, text: str) -> list[float] | None:
    """Embed rewritten fact text; any failure (class name only logged) yields None."""
    if embedder is None or not text:
        return None
    try:
        vector = await embedder.embed_query(text)
        return [float(x) for x in vector]
    except Exception as exc:
        logger.warning("derived_rederive embedding failed: %s", type(exc).__name__)
        return None


async def run_derived_rederive(
    graph_adapter: Any, llm: Any, *, limit: int = 25, embedder: Any = None
) -> dict:
    """Process up to ``limit`` pending DerivedRederiveRepair nodes, oldest first.

    ``llm`` is the scheduler's LLM adapter (``lifecycle_service.llm``). ``None``, or an adapter
    without a callable ``rederive_from_sources`` (e.g. ``UnavailableLLMAdapter``), leaves everything
    pending (flagged text stays hidden) and returns ``{"skipped": "no_llm"}``.

    A provider fault (timeout, connection, 429/5xx, billing, open breaker, rejected credentials) or
    an ``LlmUsageControlSignal`` stops the batch and leaves the item pending without spending an
    attempt (``counts["stopped"]``). A repair that reaches ``MAX_ATTEMPTS`` becomes ``'failed'``
    and records a failure event. A graph error on one item is counted in ``errors`` and the batch
    continues.
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

        try:
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
                # CAS compares the RAW stored list (facts); `episodes` is de-duplicated for sources.
                "episodes": list(
                    item["episodes_raw"] if item.get("episodes_raw") is not None
                    else (item.get("episodes") or [])
                ),
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
                    if _is_provider_fault(exc):
                        # Outage, throttling, billing or config: not the item's fault. Stop, leave
                        # pending, no attempt (#874). Defer the item so a call that always times out
                        # cannot stop every run at the head of the queue.
                        await _defer(graph_adapter, repair_key)
                        counts["stopped"] = "provider_unavailable"
                        logger.warning(
                            "derived_rederive stopped: provider unavailable (%s)", type(exc).__name__
                        )
                        break
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
                        await _call(
                            record_failure_event,
                            operation="scheduler_derived_rederive",
                            failure_stage="rederive_attempts_exhausted",
                            classification="exhausted",
                            retryable=False,
                            processing_attempt=MAX_ATTEMPTS,
                            error_type=error_class or "error",
                            error="derived rederive attempts exhausted",
                            details={"item_kind": kind, "decision": "failed"},
                        )
                    continue
                decision, text = verdict
                if decision == "unsupported" and kind == "fact":
                    applied = await _call(
                        graph_adapter.apply_derived_rederive, new_text=None, delete=True, **cas
                    )
                    outcome = "deleted"
                else:
                    extra: dict[str, Any] = {}
                    if kind == "fact":
                        vector = await _embed_or_none(embedder, text or "")
                        if vector is not None:
                            extra["embedding"] = vector
                    applied = await _call(
                        graph_adapter.apply_derived_rederive, new_text=text or "", **extra, **cas
                    )
                    outcome = "rewritten"

            if applied:
                counts[outcome] += 1
            else:
                # Re-flagged or stripped meanwhile (or gone): stays pending; next run re-reads it.
                counts["cas_failed"] += 1
        except LlmUsageControlSignal:
            counts["stopped"] = "llm_usage_control"
            break
        except Exception as exc:  # one item's graph fault must not end the batch; class name only
            counts["errors"] += 1
            logger.warning(
                "derived_rederive repair=%s errored: %s", repair_key, type(exc).__name__
            )
            # Spend an attempt so an item that errors every run reaches 'failed' instead of
            # holding a batch slot forever. Best-effort: the store may be what failed.
            try:
                await _call(
                    graph_adapter.record_derived_rederive_failure,
                    repair_key, type(exc).__name__, MAX_ATTEMPTS,
                )
            except Exception:
                logger.warning("derived_rederive repair=%s: could not record the failure", repair_key)

    logger.info("derived_rederive: %s", counts)
    return counts
