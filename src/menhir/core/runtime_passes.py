"""Startup background passes spawned by the runtime, moved verbatim from ``runtime.py``.

The initial structure scan is scheduled by ``_start_scheduler`` and the artifact reconcile pass
is the file-hook backstop that recovers source drift on boot. Neither body reads a name the test
suite monkeypatches on ``menhir.core.runtime``, so both are safe to define here; the facade
module re-imports them so ``from menhir.core.runtime import _run_startup_artifact_reconcile``
keeps working.
"""

from __future__ import annotations

import asyncio
import logging

from menhir.services import MaintenanceScheduler

logger = logging.getLogger(__name__)


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
