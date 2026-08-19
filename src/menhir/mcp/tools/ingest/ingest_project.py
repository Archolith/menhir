"""MCP tool: ingest_project — scan a project directory into the memory graph."""

from __future__ import annotations

from menhir.mcp.service_access import get_mcp_session
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope
from menhir.services.project_ingest import (
    ProjectEpisodeStatus,
    ProjectIngestOutcome,
    execute_project_ingest,
)


async def ingest_project(
    path: str,
    name: str | None = None,
    force: bool = False,
) -> str:
    """Scan a project directory and ingest its structure into the memory graph.

    Creates entities for the project, directories, files, endpoints, and
    dependencies, plus edges for CONTAINS, DEPENDS_ON, TESTS, IMPORTS,
    EXPOSES, and CALLS relationships. Also queues a narrative episode for
    Graphiti-based semantic extraction.

    Args:
        path: Absolute path to the project root directory.
        name: Project name override (default: directory basename).
        force: Re-scan even if the project fingerprint has not changed.

    Returns:
        Summary of entities and edges written plus episode queue status.
    """
    return await IngestProjectTool().execute(path=path, name=name, force=force)


class IngestProjectTool(BaseTextTool):
    name = "ingest_project"
    scope = ToolScope.OBJECT
    description = "Scan a project directory and ingest its structure into the memory graph."

    def timeout_for(self, path: str = "", name: str | None = None, force: bool = False) -> int:
        return 120

    async def endpoint(
        self,
        path: str,
        name: str | None = None,
        force: bool = False,
    ) -> str:
        """Scan a project directory and ingest its structure into the memory graph.

        Args:
            path: Absolute path to the project root directory.
            name: Project name override (default: directory basename).
            force: Re-scan even if the project fingerprint has not changed.

        Returns:
            Summary of entities and edges written plus episode queue status.
        """
        backend = self.get_backend()
        session = get_mcp_session()
        outcome = await execute_project_ingest(
            backend,
            path=path,
            name=name,
            force=force,
            session_id=session.session_id,
            user_id=session.user_id,
        )
        return _format_project_ingest_outcome(outcome)


def _format_project_ingest_outcome(outcome: ProjectIngestOutcome) -> str:
    """Format a transport-neutral project-ingest outcome for the MCP text surface."""

    if outcome.error:
        return f"Error: {outcome.error}"
    if outcome.skipped:
        return (
            f"Skipped {outcome.project_name}: fingerprint unchanged. "
            f"Use force=True to re-scan."
        )

    episode_status = {
        ProjectEpisodeStatus.QUEUED: f"episode_id={outcome.episode.episode_id}",
        ProjectEpisodeStatus.FAILED: "episode queue failed",
        ProjectEpisodeStatus.DEFERRED: "episode queue deferred (enrichment pipeline busy)",
        ProjectEpisodeStatus.ERROR: f"episode queue error: {outcome.episode.error}",
        ProjectEpisodeStatus.NO_NARRATIVE: "no narrative (skipped or empty)",
        ProjectEpisodeStatus.NOT_REQUESTED: "episode queue pending",
    }[outcome.episode.status]

    counts = outcome.counts
    meta = outcome.meta
    dirs = meta.get("dirs", "?")
    files = meta.get("files", "?")
    deps = meta.get("deps", "?")
    endpoints = meta.get("endpoints", "?")
    imports = meta.get("imports", "?")
    test_edges = meta.get("test_edges", "?")
    cross_refs = meta.get("cross_refs", "?")
    symbols = meta.get("symbols", "?")
    call_edges = meta.get("call_edges", "?")
    status_line = (
        "Graph write running in background — check server log for completion."
        if outcome.background
        else f"{counts.get('entities', 0)} entities, {counts.get('edges', 0)} edges written."
    )

    return (
        f"Scanned {outcome.project_name}: {status_line} "
        f"Semantic episode: {episode_status}\n"
        f"  dirs={dirs}, files={files}, "
        f"deps={deps}, endpoints={endpoints}, "
        f"imports={imports}, test_edges={test_edges}, "
        f"cross_refs={cross_refs}, symbols={symbols}, call_edges={call_edges}"
    )
