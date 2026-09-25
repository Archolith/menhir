"""Helpers for the memory-write REST endpoints in ``routes.py``."""

from __future__ import annotations

import asyncio


async def _count_linked_entities(
    runtime_ctx: object, row: dict[str, object] | None, episode_id: str
) -> int | None:
    """How many entities the write is linked to, or None when it cannot be determined.

    The API's ``episode_id`` is Menhir's anchor node; Graphiti stores the extracted entities
    against its own ``:Episodic`` node, which the anchor records as ``resolved_episode_uuid``.
    Counting against the anchor always read 0 -- verified against a graph whose entities were
    recallable -- so resolve first and fall back to the anchor only when unresolved.
    """

    adapter = getattr(getattr(runtime_ctx, "built", None), "graph_adapter", None)
    fetch = getattr(adapter, "fetch_linked_entity_uuids_for_episode", None)
    target = str((row or {}).get("resolved_episode_uuid") or episode_id or "")
    if fetch is None or not target:
        return None
    try:
        return len(await asyncio.to_thread(fetch, target))
    except Exception:  # noqa: BLE001 - advisory count; the write is already committed
        return None


def _terminal_status_from_row(
    row: dict[str, object] | None, *, fallback: str
) -> tuple[str, str | None, str | None, bool]:
    """Turn a processing row into (status, error, retry, timed_out) for a waited write.

    ``wait_for_episode_processing`` returns the row once it is READY or FAILED, or the last row
    it saw when the timeout elapsed. A missing row means the anchor was never written; keep the
    queue status rather than invent one.
    """

    if row is None:
        return fallback, None, None, False
    state = row.get("processing_state")
    state_text = str(getattr(state, "value", state) or "").lower()
    if state_text == "failed":
        from menhir.services.enrichment_failures import classify_enrichment_failure

        error_text = str(row.get("processing_error") or "") or None
        return "failed", error_text, classify_enrichment_failure(error_text), False
    if state_text == "ready":
        return "ready", None, None, False
    return (state_text or fallback), None, None, True
