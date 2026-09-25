"""Scheduler structure-refresh task function and hook project-index writer.

Extracted verbatim from ``scheduler_tasks.py``; the facade module re-exports everything
here so existing ``menhir.services.scheduler_tasks`` import sites keep working unchanged.
``_write_project_index`` and ``_PROJECT_INDEX_PATH`` are resolved through the facade module
at call time so monkeypatching ``menhir.services.scheduler_tasks._write_project_index`` or
``._PROJECT_INDEX_PATH`` keeps redirecting the write, exactly as when everything lived in
one module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from menhir.services.scheduler_protocols import SchedulerGraphAdapter

#: Same logger object/name as the facade module: every record this task emits must keep
#: the ``menhir.services.scheduler_tasks`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.scheduler_tasks")


# ---------------------------------------------------------------------------
# Job: refresh structure graphs
# ---------------------------------------------------------------------------

async def refresh_structure_graphs(
    graph_adapter: SchedulerGraphAdapter,
) -> dict[str, object]:
    """Re-scan all known projects whose file fingerprint has changed."""
    from menhir.services import scheduler_tasks as _facade
    from menhir.infrastructure.project_scanner import ProjectScanner
    from menhir.infrastructure.repo_topology import classify_root
    from menhir.infrastructure.structure_write_fence import StructureWritesFrozen
    from menhir.services.project_identity_service import settle_project_identity

    projects = await asyncio.to_thread(graph_adapter.list_structure_projects)
    if not projects:
        return {"projects_known": 0, "scanned": 0, "skipped": 0, "errors": 0}

    scanner = ProjectScanner()
    scanned = 0
    skipped = 0
    errors = 0
    details: list[dict[str, str]] = []

    for proj in projects:
        name = proj.get("name", "")
        root_path = proj.get("root_path", "")
        if not root_path or not await asyncio.to_thread(os.path.isdir, root_path):
            errors += 1
            details.append({"project": name, "status": "path_missing"})
            continue

        # CF-257 phase 0. The watcher is unattended and re-scans every known project on a timer,
        # so if a recorded root_path ever became a worktree -- a checkout moved, a directory
        # replaced -- it would refresh the canonical project from the wrong copy on every cycle
        # with nobody watching. The ingest guard cannot cover this: it runs at claim time, and
        # this path re-scans an already-claimed project. Reported like `path_missing`, never
        # raised: one unscannable project must not stop the sweep.
        topology = await asyncio.to_thread(classify_root, root_path)
        if not topology.may_scan:
            errors += 1
            details.append({
                "project": name,
                "status": f"identity_refused: {topology.kind.value}",
                "detail": topology.detail,
            })
            logger.warning(
                "Structure watcher refused %s at %s: %s", name, root_path, topology.detail
            )
            continue

        try:
            scan = await asyncio.to_thread(scanner.scan, root_path, name)
        except Exception as exc:
            errors += 1
            details.append({"project": name, "status": f"scan_error: {exc}"})
            logger.warning("Structure watcher scan failed for %s: %s", name, exc)
            continue

        stored_fp = await asyncio.to_thread(graph_adapter.get_scan_fingerprint, name)
        if stored_fp and stored_fp == scan.scan_fingerprint:
            skipped += 1
            continue

        # CF-257. The watcher writes through the same adapter method as everything else, so it
        # must settle identity too -- it re-scans every known project on a timer, which is exactly
        # why leaving it out produced 1,816 id-less nodes rather than a handful. It never mints:
        # an unattended job inventing an identity is the silent-mint failure this design refuses.
        try:
            claim, resolution = await asyncio.to_thread(
                settle_project_identity,
                graph_adapter, root_path=root_path, display_name=name,
            )
        except Exception as exc:  # noqa: BLE001 - one project must not stop the sweep
            errors += 1
            details.append({"project": name, "status": f"identity_error: {exc}"})
            continue
        if claim is None:
            skipped += 1
            details.append({
                "project": name,
                "status": "identity_needs_decision",
                "reason": resolution.reason,
            })
            continue
        scan.project_id = claim.project_id
        scan.identity_generation = claim.generation

        try:
            # write_project_structure MERGEs thousands of nodes/edges — a heavy synchronous Neo4j
            # write. Offload it (like scanner.scan above) so the maintenance loop never freezes the
            # asyncio event loop while re-writing a large project's structure.
            counts = await asyncio.to_thread(graph_adapter.write_project_structure, scan, "watcher", "system")
            scanned += 1
            details.append({
                "project": name,
                "status": "refreshed",
                "entities": str(counts.get("entities", 0)),
                "edges": str(counts.get("edges", 0)),
            })
        except StructureWritesFrozen:
            # CF-257 phase 2. A migration holds the fence. Reported like `path_missing` and
            # `identity_refused` rather than raised: the sweep must not die because a migration is
            # in progress, and the next cycle picks the project up once the fence lifts.
            skipped += 1
            details.append({"project": name, "status": "frozen_for_migration"})
            continue
        except Exception as exc:
            errors += 1
            details.append({"project": name, "status": f"write_error: {exc}"})
            logger.warning("Structure watcher write failed for %s: %s", name, exc)

    # Write project index for hooks integration. A synchronous JSON write to the
    # user's home directory -- offload it like every other I/O in this job so the
    # maintenance loop never freezes the event loop for its duration.
    await asyncio.to_thread(_facade._write_project_index, projects)

    return {
        "projects_known": len(projects),
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Hook integration: project index file
# ---------------------------------------------------------------------------

def _write_project_index(
    projects: list[dict[str, str]],
    *,
    index_path: Path | None = None,
) -> None:
    """Write a JSON index of ingested projects for hook scripts to read.

    Best-effort — failure is logged but never propagated.
    """
    from menhir.services import scheduler_tasks as _facade

    target = index_path or _facade._PROJECT_INDEX_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        index = [
            {"name": p.get("name", ""), "root_path": p.get("root_path", "")}
            for p in projects
            if p.get("name") and p.get("root_path")
        ]
        target.write_text(
            json.dumps(index, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("Failed to write project index for hooks", exc_info=True)
