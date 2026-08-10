"""Project-ingest application workflow and thin MCP adapter tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.services.project_ingest import (
    ProjectEpisodeOutcome,
    ProjectEpisodeStatus,
    ProjectIngestOutcome,
    execute_project_ingest,
)


def _execute(backend: object, path: str, **overrides: object) -> ProjectIngestOutcome:
    kwargs = {
        "path": path,
        "name": None,
        "force": False,
        "session_id": "session-1",
        "user_id": "user-1",
    }
    kwargs.update(overrides)
    return asyncio.run(execute_project_ingest(backend, **kwargs))


@pytest.mark.unit
def test_project_ingest_rejects_a_non_directory_before_backend_work() -> None:
    backend = SimpleNamespace(
        scan_and_write_project=AsyncMock(),
        queue_episode=AsyncMock(),
    )

    outcome = _execute(backend, "/definitely/not/a/project")

    assert outcome.error == "not a directory: /definitely/not/a/project"
    backend.scan_and_write_project.assert_not_awaited()
    backend.queue_episode.assert_not_awaited()


@pytest.mark.unit
def test_project_ingest_scans_and_queues_the_narrative(tmp_path) -> None:
    backend = SimpleNamespace(
        scan_and_write_project=AsyncMock(
            return_value={
                "counts": {"entities": 12, "edges": 20},
                "meta": {"files": 7, "dirs": 2},
                "narrative": "Project narrative",
                "background": False,
            }
        ),
        queue_episode=AsyncMock(return_value={"episode_id": "episode-1"}),
    )

    outcome = _execute(backend, str(tmp_path), name="sample", force=True)

    assert outcome.project_name == "sample"
    assert outcome.counts == {"entities": 12, "edges": 20}
    assert outcome.meta == {"files": 7, "dirs": 2}
    assert outcome.episode == ProjectEpisodeOutcome(
        ProjectEpisodeStatus.QUEUED,
        episode_id="episode-1",
    )
    backend.scan_and_write_project.assert_awaited_once_with(
        str(tmp_path),
        name="sample",
        force=True,
        session_id="session-1",
        user_id="user-1",
    )
    backend.queue_episode.assert_awaited_once_with(
        "Project narrative",
        user_id="user-1",
        session_id="session-1",
        source="project-scan",
    )


@pytest.mark.unit
def test_project_ingest_skips_queueing_for_unchanged_fingerprint(tmp_path) -> None:
    backend = SimpleNamespace(
        scan_and_write_project=AsyncMock(return_value={"skipped": True}),
        queue_episode=AsyncMock(),
    )

    outcome = _execute(backend, str(tmp_path))

    assert outcome.skipped is True
    assert outcome.episode.status is ProjectEpisodeStatus.NOT_REQUESTED
    backend.queue_episode.assert_not_awaited()


@pytest.mark.unit
def test_project_ingest_reports_queue_timeout_without_failing_scan(tmp_path) -> None:
    async def _slow_queue(*args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"episode_id": "too-late"}

    backend = SimpleNamespace(
        scan_and_write_project=AsyncMock(
            return_value={"counts": {}, "meta": {}, "narrative": "Narrative"}
        ),
        queue_episode=AsyncMock(side_effect=_slow_queue),
    )

    outcome = _execute(backend, str(tmp_path), queue_timeout_s=0.001)

    assert outcome.error is None
    assert outcome.episode.status is ProjectEpisodeStatus.DEFERRED


@pytest.mark.unit
def test_mcp_project_ingest_tool_only_adapts_and_formats() -> None:
    from menhir.mcp.tools.ingest import ingest_project as tool_module

    backend = MagicMock()
    session = SimpleNamespace(session_id="session-1", user_id="user-1")
    outcome = ProjectIngestOutcome(
        project_name="sample",
        counts={"entities": 12, "edges": 20},
        meta={"dirs": 2, "files": 7},
        episode=ProjectEpisodeOutcome(
            ProjectEpisodeStatus.QUEUED,
            episode_id="episode-1",
        ),
    )
    tool = tool_module.IngestProjectTool()
    tool.get_backend = MagicMock(return_value=backend)

    with (
        patch.object(tool_module, "get_mcp_session", return_value=session),
        patch.object(
            tool_module,
            "execute_project_ingest",
            AsyncMock(return_value=outcome),
        ) as execute,
    ):
        rendered = asyncio.run(
            tool.endpoint(path="C:/workspace/sample", name="sample", force=True)
        )

    execute.assert_awaited_once_with(
        backend,
        path="C:/workspace/sample",
        name="sample",
        force=True,
        session_id="session-1",
        user_id="user-1",
    )
    assert rendered.startswith("Scanned sample: 12 entities, 20 edges written.")
    assert "Semantic episode: episode_id=episode-1" in rendered
