"""Shared runtime lifecycle — canonical runtime owner for stdio and HTTP."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from time import perf_counter

from menhir.config import MemorySettings
from menhir.core import build_memory_services, prepare_memory_runtime
from menhir.core.runtime_preflight import collect_runtime_capabilities
from menhir.domain import new_session
from menhir.infrastructure.llama_endpoint import ensure_scheduler_running
from menhir.infrastructure.scheduler_trace import register_scheduler_task_source
from menhir.infrastructure.telemetry import enable_llm_usage_telemetry, record_lifecycle_event
from menhir.infrastructure.view_embedder import make_view_embedder, view_embedder_version
from menhir.services import MaintenanceScheduler

from .runtime_support import (
    RuntimeContext,
    RuntimeState,
    _annotate_runtime_failures,
    _has_recent_flagged_bootstrap_read,
    _init_lock,
    _remember_flagged_bootstrap_read,
    _state,
    _uses_scheduler_managed_graphiti,
)

logger = logging.getLogger(__name__)

INIT_TIMEOUT = 30


async def _run_initial_structure_scan(scheduler: MaintenanceScheduler) -> None:
    try:
        job = scheduler._jobs.get("refresh_structure_graphs")
        if job is not None:
            await scheduler._run_job(
                job,
                "scheduler_refresh_structure_graphs_initial",
                scheduler._make_refresh_structure_graphs(),
            )
    except Exception:
        logger.warning("Initial structure scan failed", exc_info=True)


async def _run_startup_artifact_reconcile(built: object, settings: object) -> None:
    """Recover artifact source drift the file-event hook could not see.

    Hook coverage will never be complete: `apply_patch`, a shell `mv`, an IDE
    refactor, a branch switch, and an external editor all move files without
    emitting an event menhir can recognize. This pass is the backstop, and it
    reports by default -- `safe_apply` is an explicit operator choice, because a
    process that mutates the graph on boot is a process nobody watched do it.
    """
    mode = getattr(settings, "artifact_reconcile_mode", "audit")
    if mode == "off":
        return

    repo_path = getattr(settings, "artifact_reconcile_repo", "") or ""
    if not repo_path:
        logger.debug(
            "Artifact reconcile mode is %s but MENHIR_ARTIFACT_RECONCILE_REPO is unset; skipping",
            mode,
        )
        return

    repository = getattr(settings, "artifact_reconcile_repository", "") or ""
    if not repository.strip():
        logger.warning(
            "Artifact reconcile mode is %s but "
            "MENHIR_ARTIFACT_RECONCILE_REPOSITORY is unset; skipping",
            mode,
        )
        return

    adapter = getattr(built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "fetch_artifact_corpus_audit"):
        return

    try:
        report = await asyncio.to_thread(
            adapter.fetch_artifact_corpus_audit,
            repo_path=repo_path,
            repository=repository,
        )
    except Exception:
        logger.warning("Startup artifact corpus audit failed", exc_info=True)
        return

    counts = report.get("counts") or {}
    by_kind = counts.get("by_kind") or {}
    logger.info(
        "Artifact corpus audit (%s): %s entries, %s sources, actions=%s, digest=%s",
        mode, counts.get("entries"), counts.get("sources"), by_kind,
        report.get("plan_digest"),
    )
    if report.get("evidence_base_valid") is False:
        logger.warning(
            "Artifact corpus audit selected a Git evidence base that cannot be "
            "compared with HEAD; apply will refuse until --from-commit selects a valid base"
        )
    if mode != "safe_apply":
        return

    # safe_apply re-derives the plan inside apply() and gates on the digest we
    # just computed, so the window between audit and apply cannot be exploited.
    try:
        from menhir.services.artifact_reconciliation_service import (
            ArtifactReconciliationService,
        )

        service = ArtifactReconciliationService(adapter._work_artifacts)  # noqa: SLF001
        result = await asyncio.to_thread(
            service.apply,
            repo_path,
            expected_digest=report.get("plan_digest") or "",
            repository=repository,
            allow_new_repository=False,
        )
        logger.info(
            "Artifact corpus safe_apply: applied=%s skipped=%s conflicted=%s refused=%s",
            len(result.applied), len(result.skipped), len(result.conflicted),
            result.refused_reason,
        )
    except Exception:
        logger.warning("Startup artifact corpus safe_apply failed", exc_info=True)


async def _start_scheduler(built: object) -> MaintenanceScheduler:
    existing = _state.scheduler
    if isinstance(existing, MaintenanceScheduler):
        await existing.start()
        setattr(built, "scheduler", existing)
        return existing

    settings = getattr(built, "settings", None)
    verifier_sync_enabled = getattr(settings, "verifier_sync_enabled", False)
    verifier_repo = None
    verifier_context = None
    if verifier_sync_enabled and getattr(built.graph_adapter, "neo4j", None) is not None:
        from menhir.infrastructure.verifier_repository import VerifierRepository
        from menhir.services.verifier_sync import VerifierContext, seed_default_verifiers

        verifier_repo = VerifierRepository(built.graph_adapter.neo4j)
        verifier_context = VerifierContext(settings=settings)
        try:
            seed_default_verifiers(verifier_repo)
        except Exception:
            logger.exception("Verifier seeding failed; sync job will run against existing verifiers")

    # Toggle the (behavior-neutral) audit trails from settings. Global switches, so they are set once
    # here rather than threaded through the scheduler / recall service.
    from menhir.infrastructure import consolidation_audit
    from menhir.infrastructure.audit_trail import RECALL as _recall_audit

    consolidation_audit.set_enabled(
        getattr(settings, "personal_memory_consolidation_audit_enabled", False)
    )
    _recall_audit.set_enabled(
        getattr(settings, "personal_memory_recall_audit_enabled", False)
    )

    personal_memory_enabled = getattr(settings, "personal_memory_consolidation_enabled", False)
    event_history_enabled = getattr(settings, "personal_memory_event_history_enabled", False)
    personal_memory_llm = None
    if (personal_memory_enabled or event_history_enabled) and settings is not None:
        from menhir.infrastructure.sync_llm import make_sync_chat

        pm_model = getattr(settings, "personal_memory_consolidation_chat_model", "") or None
        personal_memory_llm = make_sync_chat(
            settings, model=pm_model,
            max_tokens=getattr(settings, "personal_memory_consolidation_max_tokens", 2048))
        if personal_memory_llm is None:
            logger.warning(
                "personal-memory consolidation or event history enabled but no sync chat provider; job disabled")

    scheduler = MaintenanceScheduler(
        ingest_service=built.ingest_service,
        graph_adapter=built.graph_adapter,
        lifecycle_service=getattr(built, "lifecycle_service", None),
        structure_watcher_interval_s=getattr(settings, "structure_watcher_interval_s", 1800.0),
        structure_watcher_enabled=getattr(settings, "structure_watcher_enabled", True),
        experience_counter_enabled=getattr(settings, "experience_counter_enabled", True),
        experience_embed=make_view_embedder(settings) if settings is not None else None,
        experience_embed_version=view_embedder_version(settings) if settings is not None else None,
        verifier_sync_enabled=verifier_sync_enabled,
        verifier_sync_interval_s=getattr(settings, "verifier_sync_interval_s", 300.0),
        verifier_repo=verifier_repo,
        verifier_context=verifier_context,
        personal_memory_enabled=personal_memory_enabled,
        event_history_enabled=event_history_enabled,
        event_history_perceiver_version=getattr(
            settings, "personal_memory_event_history_perceiver_version", "v1"),
        personal_memory_interval_s=getattr(settings, "personal_memory_consolidation_interval_s", 300.0),
        personal_memory_llm=personal_memory_llm,
        personal_memory_k=getattr(settings, "personal_memory_consolidation_k", 3),
        personal_memory_call_budget=getattr(settings, "personal_memory_consolidation_call_budget", 300),
        personal_memory_verify_retries=getattr(settings, "personal_memory_consolidation_verify_retries", 0),
        personal_memory_sum_grounding=getattr(settings, "personal_memory_consolidation_sum_grounding", False),
        scalar_state_enabled=getattr(settings, "personal_memory_scalar_state_enabled", False),
        scalar_state_perceiver_version=getattr(
            settings, "personal_memory_scalar_state_perceiver_version", "v2"),
        scalar_reconcile_attribute=getattr(
            settings, "personal_memory_scalar_reconcile_attribute", False),
        scalar_reconcile_scope=getattr(
            settings, "personal_memory_scalar_reconcile_scope", False),
        scalar_reconcile_subject=getattr(
            settings, "personal_memory_scalar_reconcile_subject", False),
        scalar_canonical_self=getattr(
            settings, "personal_memory_scalar_canonical_self", False),
        scalar_threshold=getattr(settings, "personal_memory_scalar_threshold", 1.0),
        scalar_history_enabled=getattr(settings, "personal_memory_scalar_history_enabled", False),
        scalar_deterministic_shadow_enabled=getattr(
            settings, "personal_memory_scalar_deterministic_shadow", False),
        scalar_deterministic_router_enabled=getattr(
            settings, "personal_memory_scalar_deterministic_router", False),
        scalar_deterministic_router_promoted_classes=getattr(
            settings, "personal_memory_scalar_deterministic_classes", ()),
    )
    await scheduler.start()
    snapshot = scheduler.status_snapshot()
    if not snapshot.get("running"):
        lease = snapshot.get("lease") or {}
        logger.warning(
            "Maintenance scheduler not started in this process; blocked_reason=%s active_owner_pid=%s",
            lease.get("blocked_reason"),
            (lease.get("active_owner") or {}).get("owner_pid"),
        )
    elif getattr(settings, "structure_watcher_enabled", True):
        asyncio.create_task(
            _run_initial_structure_scan(scheduler),
            name="menhir-initial-structure-scan",
        )
    if getattr(settings, "artifact_reconcile_mode", "audit") != "off":
        asyncio.create_task(
            _run_startup_artifact_reconcile(built, settings),
            name="menhir-startup-artifact-reconcile",
        )
    _state.scheduler = scheduler
    setattr(built, "scheduler", scheduler)
    return scheduler


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


async def _initialize_services(
    settings: MemorySettings | None = None,
) -> tuple[object, object]:
    settings = settings or MemorySettings.from_env()
    enable_llm_usage_telemetry()

    # Start the scheduler BEFORE preflight so it can bring up the LLM/embedder
    # endpoints that preflight will check. Without this, preflight sees the LLM
    # as down → enrichment_ready=False → scheduler never starts → deadlock.
    uses_scheduler = _uses_scheduler_managed_graphiti(settings)
    if uses_scheduler:
        record_lifecycle_event(component="runtime_init", event="ensure_scheduler_running", state="started")
        logger.info("[init 0/6] Ensuring scheduler process is running...")
        await asyncio.to_thread(ensure_scheduler_running)
        record_lifecycle_event(component="runtime_init", event="ensure_scheduler_running", state="completed")

    # Don't acquire scheduler task slots at startup — they block when the LLM
    # is busy with enrichment.  Slots are acquired lazily on first use.
    #
    # The venv-path check guards the dev-workspace case (a stray global
    # interpreter instead of the project .venv). In a container/pip install the
    # package is properly installed into the system interpreter — graphiti's own
    # importability check (graphiti_dependency_ready) covers the real concern —
    # so allow opting out via MENHIR_ALLOW_SYSTEM_PYTHON=1. Default preserves the
    # existing dev behavior.
    require_venv = os.getenv("MENHIR_ALLOW_SYSTEM_PYTHON", "").strip().lower() not in (
        "1", "true", "yes", "on",
    )
    capabilities = await asyncio.to_thread(
        collect_runtime_capabilities,
        settings,
        require_venv=require_venv,
        acquire_scheduler_endpoints=False,
    )
    _state.capabilities = capabilities
    blocking_failures = (
        (not capabilities.venv_ready)
        or (not capabilities.neo4j_ready)
    )
    if blocking_failures:
        annotated_failures = _annotate_runtime_failures(list(capabilities.failures), settings)
        record_lifecycle_event(
            component="runtime_init",
            event="preflight",
            state="failed",
            details={"failures": annotated_failures, "startup_mode": capabilities.startup_mode},
        )
        raise RuntimeError(" | ".join(annotated_failures))
    if capabilities.failures:
        record_lifecycle_event(
            component="runtime_init",
            event="preflight",
            state="degraded",
            details={"failures": list(capabilities.failures), "startup_mode": capabilities.startup_mode},
        )

    record_lifecycle_event(component="runtime_init", event="build_memory_services", state="started")
    logger.info("[init 1/5] Building memory services...")
    built = build_memory_services(settings, capabilities=capabilities)
    record_lifecycle_event(component="runtime_init", event="build_memory_services", state="completed")

    if uses_scheduler and capabilities.enrichment_ready:
        record_lifecycle_event(component="runtime_init", event="register_scheduler_task_source", state="started")
        await register_scheduler_task_source()
        record_lifecycle_event(component="runtime_init", event="register_scheduler_task_source", state="completed")

    record_lifecycle_event(component="runtime_init", event="prepare_memory_runtime", state="started")
    logger.info("[init 3/6] Preparing memory runtime (Graphiti indices + schema)...")
    await prepare_memory_runtime(built)
    record_lifecycle_event(component="runtime_init", event="prepare_memory_runtime", state="completed")

    record_lifecycle_event(component="runtime_init", event="new_session", state="started")
    logger.info("[init 4/6] Creating session...")
    session = new_session("claude-code")
    record_lifecycle_event(
        component="runtime_init",
        event="new_session",
        state="completed",
        details={"session_id": session.session_id},
    )

    record_lifecycle_event(component="runtime_init", event="resume_pending_episodes", state="started")
    logger.info("[init 5/6] Resuming pending episodes...")
    orphan_result = None
    try:
        await asyncio.wait_for(built.ingest_service.resume_pending_episodes(), timeout=INIT_TIMEOUT)
        record_lifecycle_event(component="runtime_init", event="resume_pending_episodes", state="completed")
    except asyncio.TimeoutError:
        logger.warning("resume_pending_episodes timed out after %ds — continuing (scheduler will retry)", INIT_TIMEOUT)
        record_lifecycle_event(
            component="runtime_init",
            event="resume_pending_episodes",
            state="timeout_skipped",
            details={"timeout_s": INIT_TIMEOUT},
        )
    except Exception:
        logger.warning("resume_pending_episodes failed during init — continuing", exc_info=True)
        record_lifecycle_event(
            component="runtime_init",
            event="resume_pending_episodes",
            state="error_skipped",
        )

    _state.built = built
    _state.session = session
    if settings.benchmark_mode:
        # Benchmark isolation: no scheduler (consolidation/decay/structure) and no
        # orphan recovery, so the store is never mutated mid-measurement.
        logger.info(
            "[init done] session=%s, MENHIR_BENCHMARK_MODE=1 — scheduler + orphan recovery disabled",
            session.session_id,
        )
        return built, session
    # Start the in-process maintenance scheduler whenever enrichment is ready, regardless of
    # whether the *model endpoints* are managed by the external scheduler process
    # (uses_scheduler). Periodic maintenance — stale-lease recovery, failed-enrichment retry,
    # conflict resolution, structure refresh, counter sync — is required by every enrichment-ready
    # deployment, including direct OpenAI/Gemini Graphiti configs. Coupling it to model-process
    # ownership left direct-provider deployments with no maintenance owner (bug AR-01).
    if capabilities.enrichment_ready:
        await _start_scheduler(built)
    _state.orphan_recovery_task = asyncio.create_task(
        _run_orphan_recovery_in_background(built, session.session_id),
        name="menhir-orphan-recovery",
    )
    logger.info(
        "[init done] session=%s, orphan_recovery=background",
        session.session_id,
    )
    return built, session


async def _get_services(settings: MemorySettings | None = None) -> tuple[object, object]:
    if _state.built is not None and _state.session is not None:
        return _state.built, _state.session

    async with _init_lock:
        if _state.built is not None and _state.session is not None:
            return _state.built, _state.session
        init_task = _state.init_task
        if init_task is None or not isinstance(init_task, asyncio.Task) or init_task.done():
            init_task = asyncio.create_task(_initialize_services(settings))
            _state.init_task = init_task

    try:
        await asyncio.shield(init_task)
    except Exception:
        async with _init_lock:
            if _state.init_task is init_task:
                _state.init_task = None
        raise

    async with _init_lock:
        if _state.init_task is init_task:
            _state.init_task = None
    if _state.built is None or _state.session is None:
        raise RuntimeError("Service initialization completed but built/session is None")
    return _state.built, _state.session


async def _bootstrap_runtime_on_startup() -> None:
    started_at = perf_counter()
    record_lifecycle_event(component="mcp_stdio_boot", event="runtime_bootstrap", state="started")
    logger.info("Starting runtime bootstrap during MCP stdio startup...")
    try:
        await _get_services()
    except asyncio.CancelledError:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_lifecycle_event(
            component="mcp_stdio_boot",
            event="runtime_bootstrap",
            state="cancelled",
            details={"elapsed_ms": elapsed_ms},
        )
        logger.info("Runtime bootstrap during MCP stdio startup was cancelled after %dms", elapsed_ms)
        raise
    except Exception as exc:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_lifecycle_event(
            component="mcp_stdio_boot",
            event="runtime_bootstrap",
            state="failed",
            details={"elapsed_ms": elapsed_ms, "error": str(exc)},
        )
        logger.exception("Runtime bootstrap failed during MCP stdio startup after %dms", elapsed_ms)
    else:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_lifecycle_event(
            component="mcp_stdio_boot",
            event="runtime_bootstrap",
            state="completed",
            details={"elapsed_ms": elapsed_ms},
        )
        logger.info("Runtime bootstrap completed during MCP stdio startup in %dms", elapsed_ms)
    finally:
        current = asyncio.current_task()
        if _state.startup_runtime_task is current:
            _state.startup_runtime_task = None


@asynccontextmanager
async def mcp_lifespan(_app: object) -> AsyncIterator[dict[str, object]]:
    startup_task = asyncio.create_task(
        _bootstrap_runtime_on_startup(),
        name="menhir-runtime-startup-bootstrap",
    )
    _state.startup_runtime_task = startup_task
    try:
        yield {}
    finally:
        if _state.startup_runtime_task is startup_task:
            _state.startup_runtime_task = None
        if not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await startup_task
        await _shutdown_runtime()


async def start_runtime(settings: MemorySettings | None = None) -> RuntimeContext:
    built, session = await _get_services(settings)
    return RuntimeContext(
        built=built,
        session=session,
        scheduler=_state.scheduler,
        capabilities=_state.capabilities,
    )


async def stop_runtime() -> None:
    await _shutdown_runtime()
