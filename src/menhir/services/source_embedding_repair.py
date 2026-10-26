"""Maintenance job: embed READY source-memory episodes whose ingest-time embed never landed.

The ingest step (``enrichment_steps.embed_episode_content``) is best-effort: a timeout or embedder
error is logged and swallowed so enrichment never stalls, and nothing retried it. On the o500 build
91 episodes (34 of them answer evidence) lost their embedding to 10s timeouts under load and could
never surface in the source-memory section. This job is the retry.

Scope, deliberately narrow:
- READY episodes only. PENDING/ENRICHING ones still have the ingest step ahead of them, FAILED ones
  are retried by ``retry_failed_enrichments`` (whose re-run embeds), so READY + missing is exactly
  the set the ingest step gave up on.
- Missing embeddings only, never model re-embedding. Two processes with different embed models
  would otherwise overwrite each other's vectors forever; model migration stays with
  ``scripts/backfill_episode_embeddings.py``.
- Each write is bound to the content it was computed from (``expected_content``): if the episode's
  content changed or the node was erased between listing and write, nothing is stamped.

Writes are idempotent, so a second owner (after a lease handover) repeating a row is harmless.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Same truncation bound as the ingest step and the backfill script.
EMBED_MAX_CHARS = 8000
#: Per-call bound. Longer than the ingest step's 10s: nothing waits on this job, and the failures
#: it repairs were timeouts under load.
EMBED_TIMEOUT_S = 30.0
#: Rows listed per page.
PAGE_SIZE = 100
#: Consecutive embed failures that end a run early (embedder down); the next run tries again.
MAX_CONSECUTIVE_FAILURES = 5


async def repair_source_embeddings(
    graph_adapter: Any,
    *,
    embed: Callable[[str], Awaitable[list[float] | None]],
    model_name: Callable[[], str],
    call_budget: int = 200,
) -> dict[str, object]:
    """Embed up to ``call_budget`` READY episodes that have no content embedding.

    Pages the listing with an exclusive uuid cursor, so a row that fails or is skipped is not
    re-listed ahead of the rest within a run; the next run starts from the beginning again.
    """
    written = failed = skipped = calls = 0
    consecutive_failures = 0
    stopped = None
    after_uuid: str | None = None
    while stopped is None:
        rows = await asyncio.to_thread(
            graph_adapter.list_episodes_missing_content_embedding,
            None,
            PAGE_SIZE,
            after_uuid=after_uuid,
            ready_only=True,
        )
        if not rows:
            break
        after_uuid = str(rows[-1]["uuid"])
        for row in rows:
            if calls >= call_budget:
                stopped = "call_budget"
                break
            content = str(row.get("content") or "")
            if not content.strip():
                skipped += 1
                continue
            calls += 1
            try:
                embedding = await asyncio.wait_for(
                    embed(content[:EMBED_MAX_CHARS]), timeout=EMBED_TIMEOUT_S
                )
            except Exception as exc:  # timeout or embedder error: count it, keep going
                failed += 1
                consecutive_failures += 1
                logger.warning(
                    "Source-memory embedding repair failed episode_id=%s: %s: %s",
                    row.get("uuid"), exc.__class__.__name__, exc,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stopped = "embedder_failing"
                    break
                continue
            consecutive_failures = 0
            if not embedding:
                skipped += 1
                continue
            ok = await asyncio.to_thread(
                graph_adapter.set_episode_content_embedding,
                str(row["uuid"]),
                embedding,
                model_name(),
                expected_content=content,
            )
            if ok:
                written += 1
            else:  # content changed, node erased, or an evidence projection
                skipped += 1
    if written or failed:
        logger.info(
            "Source-memory embedding repair: wrote %d, failed %d, skipped %d (stopped=%s)",
            written, failed, skipped, stopped,
        )
    return {"written": written, "failed": failed, "skipped": skipped, "embed_calls": calls,
            "stopped": stopped}
