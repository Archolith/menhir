"""Unit tests for pause_scheduler and resume_scheduler MCP tools."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# PauseSchedulerTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pause_scheduler_when_running() -> None:
    from menhir.mcp.tools.ops.pause_scheduler import PauseSchedulerTool

    tool = PauseSchedulerTool()
    backend = MagicMock()
    backend.scheduler_pause = AsyncMock(return_value=True)
    backend.scheduler_status_snapshot = AsyncMock(
        return_value={"running": False, "lease": {"acquired": True}},
    )
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint())

    backend.scheduler_pause.assert_awaited_once()
    assert "was_running: True" in result
    assert "scheduler_running: False" in result
    assert "lease_acquired: True" in result


@pytest.mark.unit
def test_pause_scheduler_when_already_stopped() -> None:
    from menhir.mcp.tools.ops.pause_scheduler import PauseSchedulerTool

    tool = PauseSchedulerTool()
    backend = MagicMock()
    backend.scheduler_pause = AsyncMock(return_value=False)
    backend.scheduler_status_snapshot = AsyncMock(
        return_value={"running": False, "lease": {"acquired": False}},
    )
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint())

    assert "was_running: False" in result
    assert "scheduler_running: False" in result


@pytest.mark.unit
def test_pause_scheduler_snapshot_none() -> None:
    """When scheduler is not initialized, snapshot returns None."""
    from menhir.mcp.tools.ops.pause_scheduler import PauseSchedulerTool

    tool = PauseSchedulerTool()
    backend = MagicMock()
    backend.scheduler_pause = AsyncMock(return_value=False)
    backend.scheduler_status_snapshot = AsyncMock(return_value=None)
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint())

    assert "was_running: False" in result
    assert "scheduler_running: None" in result


# ---------------------------------------------------------------------------
# ResumeSchedulerTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_scheduler_success() -> None:
    from menhir.mcp.tools.ops.resume_scheduler import ResumeSchedulerTool

    tool = ResumeSchedulerTool()
    backend = MagicMock()
    backend.scheduler_resume = AsyncMock(return_value=True)
    backend.scheduler_status_snapshot = AsyncMock(
        return_value={
            "running": True,
            "lease": {"acquired": True, "blocked_reason": None},
        },
    )
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint())

    backend.scheduler_resume.assert_awaited_once()
    assert "start_succeeded: True" in result
    assert "scheduler_running: True" in result
    assert "lease_acquired: True" in result
    assert "lease_blocked_reason: None" in result


@pytest.mark.unit
def test_resume_scheduler_blocked() -> None:
    from menhir.mcp.tools.ops.resume_scheduler import ResumeSchedulerTool

    tool = ResumeSchedulerTool()
    backend = MagicMock()
    backend.scheduler_resume = AsyncMock(return_value=False)
    backend.scheduler_status_snapshot = AsyncMock(
        return_value={
            "running": False,
            "lease": {"acquired": False, "blocked_reason": "another instance holds lease"},
        },
    )
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint())

    assert "start_succeeded: False" in result
    assert "scheduler_running: False" in result
    assert "another instance holds lease" in result


@pytest.mark.unit
def test_resume_scheduler_snapshot_none() -> None:
    from menhir.mcp.tools.ops.resume_scheduler import ResumeSchedulerTool

    tool = ResumeSchedulerTool()
    backend = MagicMock()
    backend.scheduler_resume = AsyncMock(return_value=False)
    backend.scheduler_status_snapshot = AsyncMock(return_value=None)
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint())

    assert "start_succeeded: False" in result
    assert "scheduler_running: None" in result


# ---------------------------------------------------------------------------
# RuntimeProvider scheduler methods
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runtime_scheduler_pause_no_scheduler() -> None:
    from menhir.core.backend_impl import RuntimeProvider

    built = MagicMock(spec=[])  # no scheduler attr
    provider = RuntimeProvider(built, MagicMock())

    result = asyncio.run(provider.scheduler_pause())
    assert result is False


@pytest.mark.unit
def test_runtime_scheduler_pause_stops_running() -> None:
    from menhir.core.backend_impl import RuntimeProvider

    scheduler = MagicMock()
    scheduler.is_running = MagicMock(return_value=True)
    scheduler.stop = AsyncMock()
    built = MagicMock()
    built.scheduler = scheduler
    provider = RuntimeProvider(built, MagicMock())

    result = asyncio.run(provider.scheduler_pause())

    assert result is True
    scheduler.stop.assert_awaited_once()


@pytest.mark.unit
def test_runtime_scheduler_resume_no_scheduler() -> None:
    from menhir.core.backend_impl import RuntimeProvider

    built = MagicMock(spec=[])
    provider = RuntimeProvider(built, MagicMock())

    result = asyncio.run(provider.scheduler_resume())
    assert result is False


@pytest.mark.unit
def test_runtime_scheduler_resume_starts() -> None:
    from menhir.core.backend_impl import RuntimeProvider

    scheduler = MagicMock()
    scheduler.start = AsyncMock(return_value=True)
    built = MagicMock()
    built.scheduler = scheduler
    provider = RuntimeProvider(built, MagicMock())

    result = asyncio.run(provider.scheduler_resume())

    assert result is True
    scheduler.start.assert_awaited_once()


@pytest.mark.unit
def test_runtime_scheduler_snapshot_no_scheduler() -> None:
    from menhir.core.backend_impl import RuntimeProvider

    built = MagicMock(spec=[])
    provider = RuntimeProvider(built, MagicMock())

    result = asyncio.run(provider.scheduler_status_snapshot())
    assert result is None


@pytest.mark.unit
def test_runtime_scheduler_snapshot_returns_data() -> None:
    from menhir.core.backend_impl import RuntimeProvider

    scheduler = MagicMock()
    scheduler.status_snapshot = MagicMock(return_value={"running": True, "lease": {}})
    built = MagicMock()
    built.scheduler = scheduler
    provider = RuntimeProvider(built, MagicMock())

    result = asyncio.run(provider.scheduler_status_snapshot())

    assert result["running"] is True
