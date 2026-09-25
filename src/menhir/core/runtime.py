"""Shared runtime lifecycle — canonical runtime owner for stdio and HTTP.

The startup passes, lifecycle/shutdown tasks, and the saga pieces no test pins to this module's
namespace live in sibling modules (``runtime_passes.py``, ``runtime_lifecycle.py``,
``runtime_saga.py``) and are re-imported below. Everything the test suite monkeypatches through
``menhir.core.runtime`` -- the admission latch, ``_recover_saga_backlog``, the observe/recovery
pair, ``_start_scheduler``, ``_initialize_services``, ``mcp_lifespan`` -- stays defined here on
purpose: a moved body would resolve names in its sibling's globals and the patches would stop
applying.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from time import perf_counter

from menhir.config import MemorySettings
from menhir.core import build_memory_services, prepare_memory_runtime
from menhir.core.runtime_preflight import collect_runtime_capabilities
from menhir.domain import new_session
from menhir.infrastructure.telemetry import enable_llm_usage_telemetry, record_lifecycle_event
from menhir.infrastructure.view_embedder import make_view_embedder, view_embedder_version
from menhir.services import MaintenanceScheduler

from .runtime_support import (
    RuntimeContext,
    RuntimeState,  # noqa: F401 - MCP lifecycle compatibility re-export
    _annotate_runtime_failures,
    _has_recent_flagged_bootstrap_read,  # noqa: F401 - lifecycle compatibility re-export
    _init_lock,
    _remember_flagged_bootstrap_read,  # noqa: F401 - lifecycle compatibility re-export
    _state,
)

# Facade re-imports from the sibling modules: every name below stays importable from
# menhir.core.runtime, and the functions in this module call them through these module globals,
# which is what keeps the test suite's monkeypatch seams on this module working.
from .runtime_lifecycle import (
    _clear_runtime_state,  # noqa: F401 - facade re-export
    _run_orphan_recovery_in_background,
    _schedule_shutdown_runtime,  # noqa: F401 - facade re-export
    _shutdown_runtime,
    _shutdown_runtime_sync,  # noqa: F401 - facade re-export
    _stop_scheduler,  # noqa: F401 - facade re-export
)
from .runtime_passes import (
    _run_initial_structure_scan,
    _run_startup_artifact_reconcile,
)
from .runtime_saga import (
    SAGA_GATE_WAIT_SECONDS,
    SagaRecoveryNotWriteReady,
    _SAGA_GATE_POLL_SECONDS,
    _build_saga_dispatcher,  # noqa: F401 - facade re-export
    _observe_saga_backlog,
    _saga_recovery_is_armed,
)

logger = logging.getLogger(__name__)

INIT_TIMEOUT = 30


#: Why this process refused saga write admission, or None. Set once, never cleared.
#:
#: Deliberately NOT a RuntimeState field: ``clear_all()`` runs on every shutdown, and a refusal a
#: shutdown can erase is not a process-lifetime fact. Raising was never sufficient on its own --
#: every caller between the raise and the process boundary may swallow or retry it, and
#: ``_bootstrap_runtime_on_startup`` did exactly that while ``_get_services`` cleared the failed
#: init task, so a second attempt in the same PID could reach a different verdict about the same
#: unresolved backlog.
_saga_admission_refusal: str | None = None


def _refuse_saga_write_admission(reason: str) -> None:
    """Latch this process out of saga writes for the rest of its life.

    Idempotent, and the FIRST reason is the one kept: it names the condition that actually broke
    readiness, while any later one is a consequence of already being latched.
    """
    global _saga_admission_refusal
    if _saga_admission_refusal is None:
        _saga_admission_refusal = reason


def _saga_write_admission_refused() -> str | None:
    """The standing refusal, if this process has one."""
    return _saga_admission_refusal


def _recover_saga_backlog(adapter: object) -> object | None:
    """Acquire the reconciliation gate, preflight, and drain the abandoned backlog. Live (CF-20c).

    **This process must establish readiness itself; a peer holding the gate is not a verdict.**
    An earlier version returned None as soon as the gate was held elsewhere and let startup
    continue, which is unsound: while the peer holds the gate this instance's PREPAREs are blocked,
    but if the peer then finds an unresolvable backlog, refuses its own startup and releases the
    gate in its ``finally``, THIS instance is already alive and begins admitting writes against a
    dirty backlog. Nothing ever told it recovery had failed.

    So a held gate means wait, not proceed: poll until the peer releases it, then run recovery here
    and reach a verdict of our own. If the gate never comes free within
    ``SAGA_GATE_WAIT_SECONDS``, that is itself a failure to establish readiness and startup is
    refused -- fail closed, like every other ambiguity in this subsystem.

    **On failure this releases the gate and lets the caller refuse to boot.** The rule being
    honoured is "keep the writer gate closed", and an instance that does not finish starting admits
    no writers at all -- which is what that rule protects. Holding the SQLite lease as well would
    add nothing for this process while blocking healthy peers and the very operator tooling needed
    to fix the problem, and it would outlive the process by its entire TTL.
    """
    from time import monotonic, sleep

    from menhir.services.saga_preflight import build_default_dispatcher, run_preflight
    from menhir.services.saga_reconcile_gate import ReconciliationGate

    dispatcher = build_default_dispatcher(adapter)
    gate = ReconciliationGate()

    deadline = monotonic() + SAGA_GATE_WAIT_SECONDS
    waited = False
    while not gate.acquire():
        if monotonic() >= deadline:
            raise SagaRecoveryNotWriteReady(
                "another instance held the saga reconciliation gate for "
                f"{SAGA_GATE_WAIT_SECONDS:.0f}s and this instance never established its own "
                "recovery verdict; refusing to admit saga writers"
            )
        if not waited:
            waited = True
            logger.info(
                "Saga recovery: reconciliation gate held elsewhere; waiting for the peer to "
                "finish before establishing readiness here"
            )
        sleep(_SAGA_GATE_POLL_SECONDS)

    try:
        report = run_preflight(dispatcher)
        if not report.clean:
            raise SagaRecoveryNotWriteReady(
                "saga recovery preflight is NOT clean; refusing to replay:\n" + report.render()
            )
        return dispatcher.run(dry_run=False, gate=gate)
    finally:
        gate.release()


async def _run_startup_saga_observe(built: object, settings: object) -> None:
    """The startup saga barrier: dispatches on ``saga_reconcile_startup_mode`` (CF-20b/CF-20c).

    NOT observation-only despite the name, which predates live mode and is kept because it is the
    documented startup hook:

    * ``off``     -- skip entirely.
    * ``observe`` -- classify the backlog and log one summary. Mutates nothing. The DEFAULT.
    * ``live``    -- take the reconciliation gate, preflight, and replay abandoned rows.

    Called before resume_pending_episodes, which starts the enrichment worker -- the earliest local
    saga writer, since enrichment correlation can reach MergeCoordinator.merge(). Running after it
    (as an earlier version did) meant the pass observed a backlog that a live writer could already
    be adding to.

    Awaited rather than backgrounded, unlike the artifact pass above. The point of this pass is the
    ORDERING -- it is the write-readiness barrier -- and a fire-and-forget task would race the
    writers it is supposed to precede, making the ordering meaningless. It is cheap enough to
    await: the backlog query is served by idx_graph_ops_state, and on the overwhelmingly common
    zero-PREPARED startup no handler runs at all, so the whole pass is one indexed read.

    **The two modes fail in opposite directions, deliberately.** An observation failure is
    swallowed: the pass exists to make a latent hazard visible and must never become an outage of
    its own, and its `write_ready` verdict is advisory because no writer consults it. A recovery
    failure is fatal: a deployment that asked for live recovery and silently did not get it is
    running with a backlog it believes was cleared.
    """
    mode = str(getattr(settings, "saga_reconcile_startup_mode", "observe") or "").lower()
    if mode == "off":
        return

    adapter = getattr(built, "graph_adapter", None)
    if adapter is None:
        return

    if mode == "live":
        await _run_startup_saga_recovery(adapter)
        return

    try:
        run = await asyncio.to_thread(_observe_saga_backlog, adapter)
    except Exception:
        # Never fail boot over an observation. The pass exists to make a latent hazard visible,
        # so a bug in it must not become a new outage of its own.
        logger.warning("Startup saga reconcile observation failed", exc_info=True)
        return

    scanned = getattr(run, "scanned", 0)
    if not scanned:
        logger.debug("Saga reconcile observation: no PREPARED operations")
        return

    logger.info(
        "Saga reconcile observation (run %s): scanned=%s counts=%s by_kind=%s oldest_age_s=%s",
        getattr(run, "run_id", "?"), scanned, getattr(run, "counts", {}),
        getattr(run, "counts_by_kind", {}), getattr(run, "oldest_prepared_age_seconds", None),
    )
    if not getattr(run, "write_ready", True):
        logger.warning(
            "Saga reconcile observation (run %s) would NOT be write-ready: %s. Nothing is blocked "
            "yet -- recovery is not active until CF-20c. Examples: %s",
            getattr(run, "run_id", "?"),
            "; ".join(getattr(run, "blocking_reasons", []) or []),
            getattr(run, "examples", {}),
        )


async def _run_startup_saga_recovery(adapter: object) -> None:
    """Drain the abandoned PREPARED backlog before local writers are admitted (CF-20c, live).

    Opt-in: reached only when ``saga_reconcile_startup_mode`` is "live". The default stays
    "observe", so no existing deployment changes behaviour merely by upgrading -- activating
    recovery remains a deliberate per-deployment act taken after a clean preflight.

    Unlike the observation pass, failures here are FATAL. Observation exists to make a latent
    hazard visible and must never become an outage of its own; recovery is the opposite, because a
    deployment that asked for live recovery and silently did not get it is running with a backlog
    it believes was cleared.

    Every refusal raised anywhere under this call latches the process (see
    ``_refuse_saga_write_admission``). Fatal here means fatal for the process, not fatal for this
    attempt: the caller chain may swallow the exception or retry initialisation, and a retry would
    re-examine the same unresolved backlog with this process's own fresh claims on it.
    """
    try:
        await _run_startup_saga_recovery_inner(adapter)
    except SagaRecoveryNotWriteReady as exc:
        _refuse_saga_write_admission(str(exc))
        raise


async def _run_startup_saga_recovery_inner(adapter: object) -> None:
    """The recovery pass itself. Separated so one place latches every refusal it can raise."""
    run = await asyncio.to_thread(_recover_saga_backlog, adapter)
    if run is None:
        # No longer reachable: a held gate is waited out rather than skipped, so every live start
        # produces a verdict of its own. Kept as a fail-closed guard -- a None here would mean some
        # future path returned without establishing readiness, and continuing would admit writers
        # on no evidence at all.
        raise SagaRecoveryNotWriteReady(
            "saga recovery returned no verdict; refusing to admit saga writers"
        )

    logger.info(
        "Saga recovery (run %s): scanned=%s counts=%s by_kind=%s aborted=%s write_ready=%s",
        getattr(run, "run_id", "?"), getattr(run, "scanned", 0), getattr(run, "counts", {}),
        getattr(run, "counts_by_kind", {}), getattr(run, "aborted", False),
        getattr(run, "write_ready", True),
    )
    if not getattr(run, "write_ready", True):
        raise SagaRecoveryNotWriteReady(
            "saga recovery finished NOT write-ready (run "
            f"{getattr(run, 'run_id', '?')}): "
            + "; ".join(getattr(run, "blocking_reasons", []) or [])
        )


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
            max_tokens=getattr(settings, "personal_memory_consolidation_max_tokens", 8192),
            disable_reasoning=bool(getattr(
                settings, "personal_memory_consolidation_disable_reasoning", False)))
        if personal_memory_llm is None:
            logger.warning(
                "personal-memory consolidation or event history enabled but no sync chat provider; job disabled")

    scheduler = MaintenanceScheduler(
        ingest_service=built.ingest_service,
        graph_adapter=built.graph_adapter,
        lifecycle_service=getattr(built, "lifecycle_service", None),
        revision_retention_days=getattr(settings, "revision_retention_days", 14),
        # CF-171: thread the windows through. The finding this fixes is a sibling of CF-166 --
        # a retention control that existed and was never called -- so leaving these parsed but
        # unread would reproduce the exact defect.
        telemetry_observability_retention_days=getattr(
            settings, "telemetry_observability_retention_days", 30
        ),
        telemetry_diagnostic_retention_days=getattr(
            settings, "telemetry_diagnostic_retention_days", 90
        ),
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


async def _initialize_services(
    settings: MemorySettings | None = None,
    *,
    candidate_readonly: bool = False,
) -> tuple[object, object]:
    # Before anything else. A process that already refused write admission may not rebuild the
    # services that admit writers -- not on a retry, not through a second entry point, not after a
    # shutdown. The refusal outlives every one of those.
    refusal = _saga_write_admission_refused()
    if refusal is not None:
        raise SagaRecoveryNotWriteReady(
            "this process already refused saga write admission and will not initialise again: "
            + refusal
        )

    settings = settings or MemorySettings.from_env()
    if not candidate_readonly:
        enable_llm_usage_telemetry()

    # The venv-path guard is auto-scoped by preflight (venv_guard_applies): enforced
    # only for a source checkout that carries its own .venv, skipped for pip/pipx/
    # container installs, opt-out via MENHIR_ALLOW_SYSTEM_PYTHON=1. `menhir check`
    # relies on the same default so both commands agree.
    capabilities = await asyncio.to_thread(
        collect_runtime_capabilities,
        settings,
        require_venv=None,
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
    built = build_memory_services(
        settings,
        capabilities=capabilities,
        read_only=candidate_readonly,
    )
    record_lifecycle_event(component="runtime_init", event="build_memory_services", state="completed")

    if candidate_readonly:
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        graph_adapter = MemoryGraphAdapter(neo4j=built.neo4j)
        schema_ready = await asyncio.to_thread(graph_adapter.phase_one_schema_ready)
        if not schema_ready:
            raise RuntimeError(
                "candidate-readonly requires the production graph schema to be present; "
                "schema creation is fenced"
            )
    else:
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

    # Observe the saga backlog BEFORE any local saga writer can start (CF-20b).
    #
    # This MUST precede resume_pending_episodes: that call starts the enrichment worker, enrichment
    # performs correlation, and a sufficiently similar pair reaches MergeCoordinator.merge() through
    # a worker thread. An earlier version of this ran after the scheduler instead and claimed to
    # precede "a local writer", which was simply false -- the enrichment path had already been
    # running for two init steps by then.
    #
    # For observation the consequence is only an inaccurate report. For CF-20c it is a hard ordering
    # requirement: the gate and any recovery must close before workers exist to race them.
    if not candidate_readonly:
        await _run_startup_saga_observe(built, settings)

    if not candidate_readonly:
        record_lifecycle_event(component="runtime_init", event="resume_pending_episodes", state="started")
        logger.info("[init 5/6] Resuming pending episodes...")
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
    if candidate_readonly:
        logger.info(
            "[init done] session=%s, candidate-readonly mutation fence active; "
            "schema, scheduler, enrichment resume, saga recovery, and orphan recovery disabled",
            session.session_id,
        )
        return built, session
    if settings.benchmark_mode:
        # Benchmark isolation: no scheduler (consolidation/decay/structure) and no
        # orphan recovery, so the store is never mutated mid-measurement.
        logger.info(
            "[init done] session=%s, MENHIR_BENCHMARK_MODE=1 — scheduler + orphan recovery disabled",
            session.session_id,
        )
        return built, session
    # Start the in-process maintenance scheduler whenever enrichment is ready. Periodic
    # maintenance — stale-lease recovery, failed-enrichment retry, conflict resolution,
    # structure refresh, counter sync — is required by every enrichment-ready deployment.
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


async def _get_services(
    settings: MemorySettings | None = None,
    *,
    candidate_readonly: bool = False,
) -> tuple[object, object]:
    if _state.built is not None and _state.session is not None:
        return _state.built, _state.session

    async with _init_lock:
        if _state.built is not None and _state.session is not None:
            return _state.built, _state.session
        init_task = _state.init_task
        if init_task is None or not isinstance(init_task, asyncio.Task) or init_task.done():
            init_task = asyncio.create_task(
                _initialize_services(settings, candidate_readonly=candidate_readonly)
            )
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
    except SagaRecoveryNotWriteReady as exc:
        # NOT swallowed like an ordinary bootstrap failure. An ordinary failure leaves a service
        # that cannot serve; this one leaves a backlog whose safety depends on no writer starting.
        # It must reach the lifespan so the process refuses to serve at all.
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_lifecycle_event(
            component="mcp_stdio_boot",
            event="runtime_bootstrap",
            state="refused",
            details={"elapsed_ms": elapsed_ms, "error": str(exc)},
        )
        logger.error(
            "Runtime bootstrap REFUSED saga write admission after %dms: %s", elapsed_ms, exc
        )
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
        # In live mode the bootstrap IS the write-readiness barrier, so it has to finish before
        # anything is served. Backgrounding it and yielding immediately -- correct for observe
        # mode, where the pass is advisory -- turned "refusing startup" into "logging that startup
        # failed while remaining available", which is not a refusal at all.
        if _saga_recovery_is_armed():
            await startup_task
        yield {}
    finally:
        if _state.startup_runtime_task is startup_task:
            _state.startup_runtime_task = None
        if not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await startup_task
        await _shutdown_runtime()


async def start_runtime(
    settings: MemorySettings | None = None,
    *,
    candidate_readonly: bool = False,
) -> RuntimeContext:
    built, session = await _get_services(
        settings, candidate_readonly=candidate_readonly
    )
    return RuntimeContext(
        built=built,
        session=session,
        scheduler=_state.scheduler,
        capabilities=_state.capabilities,
    )


async def stop_runtime() -> None:
    await _shutdown_runtime()
