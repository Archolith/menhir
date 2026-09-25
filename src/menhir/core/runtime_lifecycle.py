"""Runtime lifecycle tasks moved verbatim from ``runtime.py``.

The scheduler stop, the post-init orphan recovery task, and the shutdown family (async, loop
scheduled, and sync entry points) live here. ``menhir.core.runtime`` re-imports every name so
existing callers -- including ``menhir.mcp.lifecycle``, which resolves ``_shutdown_runtime_sync``
as an attribute of the facade module -- keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import perf_counter

from menhir.infrastructure.telemetry import record_lifecycle_event
from menhir.services import MaintenanceScheduler

from .runtime_support import _state

logger = logging.getLogger(__name__)


async def _stop_scheduler() -> None:
    scheduler = _state.scheduler
    if isinstance(scheduler, MaintenanceScheduler):
        try:
            await scheduler.stop()
        except asyncio.CancelledError:
            logger.debug("Maintenance scheduler task was already cancelled during runtime shutdown")
        finally:
            _state.scheduler = None


async def _run_orphan_recovery_in_background(built: object, session_id: str) -> None:
    started_at = perf_counter()
    record_lifecycle_event(component="runtime_init", event="recover_orphans", state="started")
    logger.info("[post-init] Recovering orphans in background...")
    try:
        orphan_result = await built.lifecycle_service.recover_orphans()
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_lifecycle_event(
            component="runtime_init",
            event="recover_orphans",
            state="completed",
            details={
                "elapsed_ms": elapsed_ms,
                "promoted": getattr(orphan_result, "promoted", None),
                "deleted": getattr(orphan_result, "deleted", None),
                "session_id": session_id,
            },
        )
        logger.info(
            "[post-init done] session=%s, orphans_promoted=%d orphans_deleted=%d",
            session_id,
            getattr(orphan_result, "promoted", -1),
            getattr(orphan_result, "deleted", -1),
        )
    except asyncio.CancelledError:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_lifecycle_event(
            component="runtime_shutdown",
            event="recover_orphans",
            state="cancelled",
            details={"elapsed_ms": elapsed_ms, "session_id": session_id},
        )
        raise
    except Exception:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.warning("recover_orphans failed in background — skipping", exc_info=True)
        record_lifecycle_event(
            component="runtime_init",
            event="recover_orphans",
            state="error_skipped",
            details={"elapsed_ms": elapsed_ms, "session_id": session_id},
        )
    finally:
        current = asyncio.current_task()
        if _state.orphan_recovery_task is current:
            _state.orphan_recovery_task = None


def _clear_runtime_state() -> None:
    _state.clear_all()


async def _shutdown_runtime() -> None:
    built = _state.built
    try:
        orphan_task = _state.orphan_recovery_task
        if isinstance(orphan_task, asyncio.Task) and not orphan_task.done():
            orphan_task.cancel()
            with suppress(asyncio.CancelledError):
                await orphan_task
            _state.orphan_recovery_task = None
        if built is not None and hasattr(built, "ingest_service"):
            try:
                released = await built.ingest_service.shutdown()
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="ingest_service_shutdown",
                    state="completed",
                    details={"released": released},
                )
            except Exception:
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="ingest_service_shutdown",
                    state="failed",
                )
                logger.exception("ingest_service shutdown failed")
        if built is not None and hasattr(built, "recall_service"):
            try:
                await built.recall_service.shutdown()
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="recall_service_shutdown",
                    state="completed",
                )
            except Exception:
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="recall_service_shutdown",
                    state="failed",
                )
                logger.exception("recall_service shutdown failed")
        if built is not None and hasattr(built, "graphiti_client"):
            try:
                await built.graphiti_client.close()
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="graphiti_client_close",
                    state="completed",
                )
            except Exception:
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="graphiti_client_close",
                    state="failed",
                )
                logger.exception("Graphiti client close failed")
        if built is not None and hasattr(built, "neo4j"):
            try:
                built.neo4j.close()
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="neo4j_driver_close",
                    state="completed",
                )
            except Exception:
                record_lifecycle_event(
                    component="runtime_shutdown",
                    event="neo4j_driver_close",
                    state="failed",
                )
                logger.exception("Neo4j driver close failed")
        await _stop_scheduler()
    finally:
        _clear_runtime_state()


def _schedule_shutdown_runtime(loop: asyncio.AbstractEventLoop) -> None:
    existing = _state.shutdown_task
    if isinstance(existing, asyncio.Task) and not existing.done():
        return
    task = loop.create_task(_shutdown_runtime())
    _state.shutdown_task = task


def _shutdown_runtime_sync() -> None:
    existing_shutdown = _state.shutdown_task
    if isinstance(existing_shutdown, asyncio.Task) and not existing_shutdown.done():
        return
    built = _state.built
    scheduler = _state.scheduler
    if built is None and scheduler is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(_shutdown_runtime())
        except RuntimeError:
            logger.warning("Cannot run async shutdown — no accessible event loop")
            _clear_runtime_state()
    else:
        _schedule_shutdown_runtime(loop)
        logger.debug("Scheduled async shutdown hook on the active event loop")
