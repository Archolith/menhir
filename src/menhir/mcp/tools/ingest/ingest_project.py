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
    namespace: str = "",
    force_identity: bool = False,
    identity_action: str | None = None,
    adopt_project_id: str | None = None,
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
        namespace: Optional silo for the QUEUED EPISODE -- the recallable memory this
            produces. Empty = default/global behavior. The structural graph itself is shared
            and is keyed by project, not by namespace.
        force_identity: Operator-tier override for the CF-257 identity guard -- scan a worktree,
            a submodule, or a directory whose basename is already claimed by another project.
            Deliberately writes across an identity boundary; the second scan prunes the first
            project's files.

    Returns:
        Summary of entities and edges written plus episode queue status.
    """
    return await IngestProjectTool().execute(
        path=path, name=name, force=force, namespace=namespace,
        **({"force_identity": True} if force_identity else {}),
        **({"identity_action": identity_action} if identity_action else {}),
        **({"adopt_project_id": adopt_project_id} if adopt_project_id else {}),
    )


class IngestProjectTool(BaseTextTool):
    name = "ingest_project"
    # NAMESPACED: see `execute_project_ingest` -- the structure write is shared by design, the
    # queued episode is tenant memory and was landing in the default group regardless of the
    # caller's pin (CF-220's escape in a fourth tool).
    scope = ToolScope.NAMESPACED
    description = "Scan a project directory and ingest its structure into the memory graph."

    def timeout_for(
        self, path: str = "", name: str | None = None,
        force: bool = False, **_unused: object,
    ) -> int:
        # timeout_for is dispatched with the caller's raw kwargs, so it must
        # accept everything endpoint accepts (including namespace).
        return 120

    async def endpoint(
        self,
        path: str,
        name: str | None = None,
        force: bool = False,
        namespace: str = "",
        force_identity: bool = False,
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
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
            # Forwarded only when set: byte-identical call when none is used.
            **({"force_identity": True} if force_identity else {}),
            **({"identity_action": identity_action} if identity_action else {}),
            **({"adopt_project_id": adopt_project_id} if adopt_project_id else {}),
            **({"namespace": namespace} if namespace else {}),
        )
        return _format_project_ingest_outcome(outcome)


def _format_project_ingest_outcome(outcome: ProjectIngestOutcome) -> str:
    """Format a transport-neutral project-ingest outcome for the MCP text surface."""

    if outcome.needs_decision:
        nd = outcome.needs_decision
        lines = [
            f"NOT SCANNED -- {outcome.project_name} needs an identity decision "
            f"({nd.get('reason')}).",
            f"  directory: {nd.get('directory')}",
        ]
        for c in nd.get("candidates") or []:
            lines.append(
                f"  candidate: project_id={c.get('project_id')} "
                f"name={c.get('display_name')} entities={c.get('entity_count')} "
                f"last_scan={c.get('last_scan')} recorded_root={c.get('recorded_root_path')}"
            )
        if not (nd.get("candidates") or []):
            lines.append("  no candidate: nothing recorded at this directory.")
        lines.append(
            "  Retry with identity_action='adopt' and adopt_project_id=<id> to continue an "
            "existing project, or identity_action='new' to mint a fresh identity."
        )
        return chr(10).join(lines)
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
