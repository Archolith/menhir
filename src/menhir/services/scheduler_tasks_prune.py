"""Scheduler telemetry-retention task functions.

Extracted verbatim from ``scheduler_tasks.py``; the facade module re-exports everything
here so existing ``menhir.services.scheduler_tasks`` import sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging

#: Same logger object/name as the facade module: every record these tasks emit must keep
#: the ``menhir.services.scheduler_tasks`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.scheduler_tasks")


async def prune_telemetry_tables(
    *, observability_days: int, diagnostic_days: int
) -> dict[str, object]:
    """Delete sidecar telemetry rows past their retention window (CF-171).

    Nothing deleted from any of the nine high-volume tables. The one pruner that existed targeted
    `memory_revisions` -- by write volume the LEAST affected table -- and was itself never wired
    until CF-166. Measured growth is ~664 bytes/row against ~30 rows per ingest, and this is the
    file six writers contend on, so the size is not merely disk: a larger file means longer WAL
    checkpoints and deeper B-trees, which lengthens exactly the CF-170 writes that block the loop.

    A tier set to 0 is skipped entirely, so an operator can disable one window without disabling
    the other. Threaded off the event loop for the same reason as its sibling.
    """
    from menhir.infrastructure.telemetry import telemetry_store

    if observability_days <= 0 and diagnostic_days <= 0:
        return {"pruned": {}, "skipped": "both tiers disabled"}

    deleted = await asyncio.to_thread(
        telemetry_store.prune_telemetry_tables,
        observability_days=observability_days if observability_days > 0 else 10**6,
        diagnostic_days=diagnostic_days if diagnostic_days > 0 else 10**6,
    )
    total = sum(deleted.values())
    if total:
        logger.info(
            "Pruned %d telemetry row(s) across %d table(s): %s",
            total,
            len(deleted),
            ", ".join(f"{t}={n}" for t, n in sorted(deleted.items())),
        )
    return {"pruned": deleted, "total": total}


async def prune_telemetry_revisions(*, retention_days: int) -> dict[str, object]:
    """Delete `memory_revisions` rows past the retention window.

    This job exists because the control it drives did not. `prune_old_revisions` was written,
    fully unit-tested, and never called from production: `grep` found its definition and six
    test references, and nothing else. The setting that is supposed to configure it,
    `MENHIR_REVISION_RETENTION_DAYS`, was parsed into `revision_retention_days` and then read
    nowhere. Meanwhile the operator runbook stated, as fact, that the window was enforced and
    configurable. Three independent failures of one control, and a document asserting it worked.

    Threaded off the event loop: the store does synchronous SQLite with a commit, and the
    sidecar is shared by seven writers.
    """
    from menhir.infrastructure.telemetry import telemetry_store

    days = max(1, int(retention_days))
    deleted = await asyncio.to_thread(
        telemetry_store.prune_old_revisions, retention_days=days
    )
    if deleted:
        logger.info(
            "Pruned %d memory_revisions row(s) older than %d day(s)", deleted, days
        )
    return {"retention_days": days, "rows_deleted": int(deleted)}
