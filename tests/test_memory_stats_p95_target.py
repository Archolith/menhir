from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.mcp.tools.ops.get_memory_stats import (
    DEFAULT_P95_TARGET_S,
    P95_TARGET_ENV,
    GetMemoryStatsTool,
    enrichment_p95_target_seconds,
    format_p95,
    get_memory_stats,
)

pytestmark = pytest.mark.unit


def test_target_absent_uses_default(monkeypatch):
    monkeypatch.delenv(P95_TARGET_ENV, raising=False)
    assert enrichment_p95_target_seconds() == DEFAULT_P95_TARGET_S == 120.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 120.0),
        ("   ", 120.0),
        ("90", 90.0),
        ("90.5", 90.5),
        ("0", 0.0),
        ("abc", 120.0),
        ("-5", 120.0),
        ("nan", 120.0),
        ("inf", 120.0),
    ],
)
def test_target_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(P95_TARGET_ENV, raw)
    assert enrichment_p95_target_seconds() == expected


def test_format_under_target():
    assert format_p95(60_000, 120.0) == "60.0s  (target 120s: ok)"


def test_format_at_target_is_ok():
    assert format_p95(120_000, 120.0) == "120.0s  (target 120s: ok)"


def test_format_over_target():
    assert format_p95(130_000, 120.0) == "130.0s  (target 120s: MISS)"


def test_format_fractional_target_label():
    assert format_p95(100_000, 90.5) == "100.0s  (target 90.5s: MISS)"


def test_format_target_zero_has_no_grade():
    assert format_p95(500_000, 0.0) == "500.0s"


def test_format_none_is_na():
    assert format_p95(None, 120.0) == "n/a"
    assert format_p95(None, 0.0) == "n/a"


async def _tool_output(tmp_path, p95_ms: int) -> str:
    store = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    enrichment = {
        **store.fetch_enrichment_rate(since_hours=24),
        "p95_duration_ms": p95_ms,
    }
    backend = MagicMock()
    backend.fetch_memory_overview = AsyncMock(
        return_value={"entity_count": 0, "episode_count": 0}
    )
    backend.fetch_operation_stats = AsyncMock(
        return_value=store.fetch_operation_stats(since_hours=24)
    )
    backend.fetch_failure_summary = AsyncMock(
        return_value=store.fetch_failure_summary(since_hours=24)
    )
    backend.fetch_enrichment_rate = AsyncMock(return_value=enrichment)
    backend.fetch_lifecycle_summary = AsyncMock(
        return_value=store.fetch_lifecycle_summary(since_hours=24)
    )
    backend.get_queue_depth = AsyncMock(return_value=0)
    backend.circuit_breaker_snapshots = AsyncMock(return_value={})
    with patch.object(GetMemoryStatsTool, "get_backend", return_value=backend):
        return str(await get_memory_stats(since_hours=24))


@pytest.mark.asyncio
async def test_tool_output_uses_configured_target(tmp_path, monkeypatch):
    monkeypatch.setenv(P95_TARGET_ENV, "300")
    output = await _tool_output(tmp_path, 215_000)
    assert "  p95 time:    215.0s  (target 300s: ok)" in output


@pytest.mark.asyncio
async def test_tool_output_without_grade(tmp_path, monkeypatch):
    monkeypatch.setenv(P95_TARGET_ENV, "0")
    output = await _tool_output(tmp_path, 215_000)
    assert "  p95 time:    215.0s" in output
    assert "(target" not in output
