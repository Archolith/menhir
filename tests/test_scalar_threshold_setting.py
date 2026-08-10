"""Settings and wiring contract for the typed-scalar agreement threshold."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from menhir.config import MemorySettings
from menhir.services import maintenance_scheduler as scheduler_module
from menhir.services.maintenance_scheduler import MaintenanceScheduler


@pytest.mark.unit
def test_scalar_threshold_defaults_to_unanimous() -> None:
    assert MemorySettings().personal_memory_scalar_threshold == 1.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.0", 1.0),
        ("0.5", 0.5),
        ("2/3", 2 / 3),
    ],
)
def test_scalar_threshold_parses_decimal_or_ratio(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: float,
) -> None:
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD", raw)
    assert MemorySettings.from_env().personal_memory_scalar_threshold == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["0", "1.1", "2/0", "not-a-number"])
def test_scalar_threshold_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD", raw)
    with pytest.raises(ValueError):
        MemorySettings.from_env()


PLUMBING = [
    ("src/menhir/core/runtime.py", "scalar_threshold=getattr("),
    ("src/menhir/api/routes_handlers.py", "scalar_threshold=getattr("),
    (
        "src/menhir/services/maintenance_scheduler.py",
        "scalar_threshold=self.scalar_threshold",
    ),
    (
        "src/menhir/services/scheduler_tasks.py",
        "threshold=(threshold if scalar_threshold is None else scalar_threshold)",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(("relative_path", "forwarding"), PLUMBING)
def test_every_runtime_hop_forwards_scalar_threshold(
    relative_path: str,
    forwarding: str,
) -> None:
    source = (Path(__file__).parent.parent / relative_path).read_text(encoding="utf-8")
    assert forwarding in source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_forwards_scalar_threshold_without_changing_counter_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_consolidate(*args, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(
        scheduler_module,
        "consolidate_personal_memory",
        fake_consolidate,
    )
    scheduler = MaintenanceScheduler(
        ingest_service=MagicMock(),
        graph_adapter=MagicMock(),
        personal_memory_llm=MagicMock(),
        scalar_threshold=2 / 3,
    )

    await scheduler._make_consolidate_personal_memory()

    assert seen["scalar_threshold"] == 2 / 3
    assert "threshold" not in seen
