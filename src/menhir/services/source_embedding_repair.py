"""Maintenance job: embed settled source-memory episodes whose ingest-time embed never landed.

The ingest step (``enrichment_steps.embed_episode_content``) is best-effort: a timeout or embedder
error is logged and swallowed so enrichment never stalls, and nothing retried it. On the o500 build
91 episodes (34 of them answer evidence) lost their embedding to 10s timeouts under load and could
never surface in the source-memory section. This job is the retry.

Scope, deliberately narrow:
- READY and FAILED episodes only (#662). PENDING/ENRICHING ones still have the ingest step ahead
  of them. A FAILED episode is recallable through the source-memory lane, and a terminal or
  manual-review failure is never re-run, so nothing else would ever embed it. Erase tombstones
  are excluded by the listing.
- Missing embeddings only, never model re-embedding. Two processes with different embed models
  would otherwise overwrite each other's vectors forever; model migration stays with
  ``scripts/backfill_episode_embeddings.py``.
- Each write is bound to the content it was computed from (``expected_content``): if the episode's
  content changed or the node was erased between listing and write, nothing is stamped.

Writes are idempotent, so a second owner (after a lease handover) repeating a row is harmless.
"""

from __future__ import annotations

import asyncio
import uuid
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from menhir.infrastructure.openai_rate_limit import await_with_backoff_aware_timeout
from menhir.infrastructure.telemetry import record_failure_event

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
#: Recorded failures after which an episode is no longer listed by this job (#921), so rows that
#: keep failing stop heading every run's listing. A successful embed clears the count.
MAX_EPISODE_FAILURES = 3
_PROBE_TEXT = "embedder health probe"


async def _embedder_is_up(embed: Callable[[str], Awaitable[list[float] | None]]) -> bool:
    """One probe after a run with errors: up means the failures were item-specific.

    The text is unique per call so the shared embedding cache cannot answer it during an outage.
    """
    try:
        return bool(await await_with_backoff_aware_timeout(
            embed(f"{_PROBE_TEXT} {uuid.uuid4().hex}"), timeout_s=EMBED_TIMEOUT_S))
    except Exception:
        return False


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
    written = failed = skipped = calls = excluded = 0
    error_class: dict[str, str] = {}
    consecutive_failures = 0
    errored: list[str] = []  # embed raised: item-specific only if the embedder is up (see below)
    empty: list[str] = []  # embed returned nothing: item-specific by construction
    stopped = None
    after_uuid: str | None = None
    while stopped is None:
        rows = await asyncio.to_thread(
            graph_adapter.list_episodes_missing_content_embedding,
            None,
            PAGE_SIZE,
            after_uuid=after_uuid,
            settled_only=True,
            max_failures=MAX_EPISODE_FAILURES,
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
                embedding = await await_with_backoff_aware_timeout(
                    embed(content[:EMBED_MAX_CHARS]), timeout_s=EMBED_TIMEOUT_S
                )
            except Exception as exc:  # timeout or embedder error: count it, keep going
                failed += 1
                errored.append(str(row["uuid"]))
                error_class[str(row["uuid"])] = exc.__class__.__name__
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
                empty.append(str(row["uuid"]))
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
    to_record = empty
    # Probe after any errors, not only an embedder_failing stop: a short listing or a budget stop
    # during an outage must not count the outage against each episode.
    if errored and await _embedder_is_up(embed):
        to_record = errored + empty
    if to_record:
        newly_excluded: list[str] = []
        try:
            newly_excluded = list(await asyncio.to_thread(
                graph_adapter.record_source_embedding_failures, to_record, MAX_EPISODE_FAILURES
            ) or [])
        except Exception:  # bookkeeping only: never fail the repair run over it
            logger.warning("Could not record source-embedding failures", exc_info=True)
        for episode_uuid in newly_excluded:
            excluded += 1
            try:
                await asyncio.to_thread(
                    record_failure_event,
                    operation="scheduler_source_embedding_repair",
                    episode_uuid=episode_uuid,
                    failure_stage="source_embedding_excluded",
                    classification="exhausted",
                    retryable=False,
                    processing_attempt=MAX_EPISODE_FAILURES,
                    error_type=error_class.get(episode_uuid, "EmptyEmbedding"),
                    error="source embedding repair excluded the episode after repeated failures",
                    details={
                        "failure_limit": MAX_EPISODE_FAILURES,
                        "decision": "excluded_until_cooldown",
                        "note": "listed again after the 7-day cooldown",
                    },
                )
            except Exception:  # best-effort: never fail the repair run over it
                logger.warning("Could not record source-embedding exclusion event", exc_info=True)
    if written or failed:
        logger.info(
            "Source-memory embedding repair: wrote %d, failed %d, skipped %d (stopped=%s)",
            written, failed, skipped, stopped,
        )
    return {"written": written, "failed": failed, "skipped": skipped, "embed_calls": calls,
            "excluded": excluded, "stopped": stopped}
