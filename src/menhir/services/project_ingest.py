"""Application helpers for deterministic project-structure ingestion."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from menhir.infrastructure.project_scanner import ProjectScanResult


class ProjectIngestBackend(Protocol):
    """Backend capabilities required by the project-ingest application workflow."""

    async def scan_and_write_project(
        self,
        path: str,
        *,
        name: str | None,
        force: bool,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]: ...

    async def queue_episode(
        self,
        text: str,
        *,
        user_id: str,
        session_id: str,
        source: str,
    ) -> dict[str, Any]: ...


class ProjectEpisodeStatus(str, Enum):
    """Outcome of the best-effort semantic episode queue step."""

    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    FAILED = "failed"
    DEFERRED = "deferred"
    ERROR = "error"
    NO_NARRATIVE = "no_narrative"


@dataclass(frozen=True)
class ProjectEpisodeOutcome:
    status: ProjectEpisodeStatus
    episode_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProjectIngestOutcome:
    """Transport-neutral result of project structure scan and episode queueing."""

    project_name: str
    counts: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    background: bool = False
    skipped: bool = False
    error: str | None = None
    episode: ProjectEpisodeOutcome = field(
        default_factory=lambda: ProjectEpisodeOutcome(ProjectEpisodeStatus.NOT_REQUESTED)
    )


def build_project_narrative(scan: ProjectScanResult) -> str:
    """Build the bounded Graphiti narrative associated with a structure scan."""

    parts: list[str] = []
    parts.append(f'Project "{scan.name}" is a {scan.stack} project at {scan.root_path}.')

    if scan.description:
        parts.append(f"\nDescription: {scan.description}")

    entrypoints = [file for file in scan.files if file.role == "entrypoint"]
    if entrypoints:
        parts.append("\nEntrypoints:")
        for file in entrypoints[:10]:
            parts.append(f"- {file.rel_path} — {file.description or 'entry point'}")

    if scan.directories:
        parts.append("\nKey directories:")
        for directory in scan.directories[:20]:
            description = f" — {directory.purpose}" if directory.purpose else ""
            parts.append(f"- {directory.rel_path}/{description}")

    source_files = [file for file in scan.files if file.role == "file"]
    if source_files:
        parts.append("\nKey files:")
        for file in source_files[:30]:
            parts.append(f"- {file.rel_path}")

    if scan.endpoints:
        parts.append("\nExposed endpoints:")
        for endpoint in scan.endpoints[:20]:
            parts.append(
                f"- {endpoint.name} ({endpoint.kind}) in {endpoint.file_path}"
            )

    if scan.test_edges:
        parts.append("\nTest coverage:")
        for test_edge in scan.test_edges[:15]:
            parts.append(f"- {test_edge.test_path} tests {test_edge.source_path}")

    if scan.dependencies:
        dependency_list = ", ".join(scan.dependencies[:30])
        parts.append(f"\nDependencies: {dependency_list}")

    if scan.cross_project_refs:
        parts.append("\nCross-project references:")
        for reference in scan.cross_project_refs:
            parts.append(
                f"- {scan.name} → {reference.target_project} via "
                f"{reference.mechanism} ({reference.evidence})"
            )

    narrative = "\n".join(parts)
    if len(narrative) > 4000:
        narrative = narrative[:3950] + "\n... [truncated]"
    return narrative


async def execute_project_ingest(
    backend: ProjectIngestBackend,
    *,
    path: str,
    name: str | None,
    force: bool,
    session_id: str,
    user_id: str,
    queue_timeout_s: float = 10.0,
) -> ProjectIngestOutcome:
    """Run project scan/write and best-effort semantic queueing outside transport code."""

    project_name = name or os.path.basename(os.path.normpath(path))
    if not os.path.isdir(path):
        return ProjectIngestOutcome(
            project_name=project_name,
            error=f"not a directory: {path}",
        )

    result = await backend.scan_and_write_project(
        path,
        name=name,
        force=force,
        session_id=session_id,
        user_id=user_id,
    )
    if result.get("skipped"):
        return ProjectIngestOutcome(project_name=project_name, skipped=True)

    narrative = str(result.get("narrative") or "")
    episode = ProjectEpisodeOutcome(ProjectEpisodeStatus.NO_NARRATIVE)
    if narrative:
        try:
            queued = await asyncio.wait_for(
                backend.queue_episode(
                    narrative,
                    user_id=user_id,
                    session_id=session_id,
                    source="project-scan",
                ),
                timeout=queue_timeout_s,
            )
            episode_id = queued.get("episode_id")
            episode = ProjectEpisodeOutcome(
                ProjectEpisodeStatus.QUEUED if episode_id else ProjectEpisodeStatus.FAILED,
                episode_id=str(episode_id) if episode_id else None,
            )
        except asyncio.TimeoutError:
            episode = ProjectEpisodeOutcome(ProjectEpisodeStatus.DEFERRED)
        except Exception as exc:
            episode = ProjectEpisodeOutcome(
                ProjectEpisodeStatus.ERROR,
                error=str(exc),
            )

    return ProjectIngestOutcome(
        project_name=project_name,
        counts=dict(result.get("counts") or {}),
        meta=dict(result.get("meta") or {}),
        background=bool(result.get("background", False)),
        episode=episode,
    )


__all__ = [
    "ProjectEpisodeOutcome",
    "ProjectEpisodeStatus",
    "ProjectIngestBackend",
    "ProjectIngestOutcome",
    "build_project_narrative",
    "execute_project_ingest",
]
