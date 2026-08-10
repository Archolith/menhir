"""Tests for the structure watcher scheduler job."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from menhir.services.maintenance_scheduler import MaintenanceScheduler, _JobState


# ---------------------------------------------------------------------------
# Minimal stubs for SchedulerGraphAdapter (structure watcher subset)
# ---------------------------------------------------------------------------

@dataclass
class _StubGraphAdapter:
    """Minimal graph adapter stub for structure watcher tests."""
    _projects: list[dict[str, str]] = field(default_factory=list)
    _fingerprints: dict[str, str] = field(default_factory=dict)
    _write_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def list_structure_projects(self) -> list[dict[str, str]]:
        return self._projects

    def get_scan_fingerprint(self, project_name: str) -> str | None:
        return self._fingerprints.get(project_name)

    def write_project_structure(self, scan: object, session_id: str, user_id: str) -> dict[str, int]:
        self._write_calls.append((getattr(scan, "name", ""), session_id, user_id))
        return {"entities": 5, "edges": 3}

    # Required by SchedulerGraphAdapter protocol but unused by watcher
    def fetch_memory_overview(self) -> dict[str, object]:
        return {}

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        return []

    def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
        return None

    def stamp_ingest_metadata(self, **kwargs: object) -> object:
        return None

    def mark_episode_ready(self, *args: object, **kwargs: object) -> bool:
        return False


@dataclass
class _StubIngestService:
    """Minimal ingest service stub."""
    async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
        return (0, 0)

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        return False

    def get_queue_depth(self) -> int:
        return 0

    def get_failed_enrichment_count(self) -> int:
        return 0

    def get_max_enrichment_attempts(self) -> int:
        return 3

    def get_context_window_retry_attempts(self) -> int:
        return 6


@dataclass
class _FakeScan:
    name: str = "test-project"
    scan_fingerprint: str = "new-fp-abc"


# ---------------------------------------------------------------------------
# Tests: refresh_structure_graphs task function
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_project_index(tmp_path):
    """Prevent tests from overwriting the real project-index.json."""
    fake_path = tmp_path / "project-index.json"
    with patch("menhir.services.scheduler_tasks._PROJECT_INDEX_PATH", fake_path):
        yield fake_path


class TestRefreshStructureGraphs:
    """Tests for the refresh_structure_graphs scheduler task."""

    async def test_no_known_projects_returns_zeros(self):
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        adapter = _StubGraphAdapter(_projects=[])
        result = await refresh_structure_graphs(adapter)

        assert result["projects_known"] == 0
        assert result["scanned"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0

    async def test_unchanged_fingerprint_skips(self, tmp_path):
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        project_dir = tmp_path / "my-proj"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("print('hello')")

        adapter = _StubGraphAdapter(
            _projects=[{"name": "my-proj", "root_path": str(project_dir)}],
            _fingerprints={"my-proj": "will-match"},
        )

        fake_scan = _FakeScan(name="my-proj", scan_fingerprint="will-match")
        with patch(
            "menhir.infrastructure.project_scanner.ProjectScanner"
        ) as MockScanner:
            MockScanner.return_value.scan.return_value = fake_scan
            result = await refresh_structure_graphs(adapter)

        assert result["projects_known"] == 1
        assert result["skipped"] == 1
        assert result["scanned"] == 0
        assert len(adapter._write_calls) == 0

    async def test_changed_fingerprint_rescans(self, tmp_path):
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        project_dir = tmp_path / "my-proj"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("print('hello')")

        adapter = _StubGraphAdapter(
            _projects=[{"name": "my-proj", "root_path": str(project_dir)}],
            _fingerprints={"my-proj": "old-fp"},
        )

        fake_scan = _FakeScan(name="my-proj", scan_fingerprint="new-fp")
        with patch(
            "menhir.infrastructure.project_scanner.ProjectScanner"
        ) as MockScanner:
            MockScanner.return_value.scan.return_value = fake_scan
            result = await refresh_structure_graphs(adapter)

        assert result["projects_known"] == 1
        assert result["scanned"] == 1
        assert result["skipped"] == 0
        assert len(adapter._write_calls) == 1
        assert adapter._write_calls[0] == ("my-proj", "watcher", "system")

    async def test_missing_path_counts_as_error(self):
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        adapter = _StubGraphAdapter(
            _projects=[{"name": "gone", "root_path": "/nonexistent/path/xyz"}],
        )
        result = await refresh_structure_graphs(adapter)

        assert result["projects_known"] == 1
        assert result["errors"] == 1
        assert result["scanned"] == 0
        assert result["details"][0]["status"] == "path_missing"

    async def test_scan_exception_counts_as_error(self, tmp_path):
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        project_dir = tmp_path / "bad-proj"
        project_dir.mkdir()

        adapter = _StubGraphAdapter(
            _projects=[{"name": "bad-proj", "root_path": str(project_dir)}],
        )

        with patch(
            "menhir.infrastructure.project_scanner.ProjectScanner"
        ) as MockScanner:
            MockScanner.return_value.scan.side_effect = RuntimeError("scan boom")
            result = await refresh_structure_graphs(adapter)

        assert result["errors"] == 1
        assert result["scanned"] == 0
        assert "scan_error" in result["details"][0]["status"]

    async def test_first_ingest_no_stored_fingerprint(self, tmp_path):
        """When no fingerprint is stored (first scan), project should be scanned."""
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        project_dir = tmp_path / "new-proj"
        project_dir.mkdir()
        (project_dir / "app.py").write_text("pass")

        adapter = _StubGraphAdapter(
            _projects=[{"name": "new-proj", "root_path": str(project_dir)}],
            _fingerprints={},  # no stored fingerprint
        )

        fake_scan = _FakeScan(name="new-proj", scan_fingerprint="fresh-fp")
        with patch(
            "menhir.infrastructure.project_scanner.ProjectScanner"
        ) as MockScanner:
            MockScanner.return_value.scan.return_value = fake_scan
            result = await refresh_structure_graphs(adapter)

        assert result["scanned"] == 1
        assert len(adapter._write_calls) == 1

    async def test_project_index_written_for_hooks(self, tmp_path, _redirect_project_index):
        """Watcher writes project-index.json for hook integration."""
        from menhir.services.scheduler_tasks import refresh_structure_graphs

        adapter = _StubGraphAdapter(
            _projects=[
                {"name": "proj-a", "root_path": "/path/to/a"},
                {"name": "proj-b", "root_path": "/path/to/b"},
                {"name": "no-path", "root_path": ""},  # should be excluded
            ],
        )
        await refresh_structure_graphs(adapter)

        index_path: Path = _redirect_project_index
        assert index_path.exists()
        index = json.loads(index_path.read_text("utf-8"))
        assert len(index) == 2
        assert index[0]["name"] == "proj-a"
        assert index[1]["root_path"] == "/path/to/b"


# ---------------------------------------------------------------------------
# Tests: MaintenanceScheduler registration
# ---------------------------------------------------------------------------

class TestSchedulerWatcherRegistration:
    """Tests for watcher job registration in MaintenanceScheduler."""

    def test_watcher_job_registered_when_enabled(self):
        scheduler = MaintenanceScheduler(
            ingest_service=_StubIngestService(),
            graph_adapter=_StubGraphAdapter(),
            structure_watcher_enabled=True,
            structure_watcher_interval_s=600.0,
        )
        assert "refresh_structure_graphs" in scheduler._jobs
        assert scheduler._jobs["refresh_structure_graphs"].interval_s == 600.0

    def test_watcher_job_not_registered_when_disabled(self):
        scheduler = MaintenanceScheduler(
            ingest_service=_StubIngestService(),
            graph_adapter=_StubGraphAdapter(),
            structure_watcher_enabled=False,
        )
        assert "refresh_structure_graphs" not in scheduler._jobs

    def test_watcher_default_interval_is_1800(self):
        scheduler = MaintenanceScheduler(
            ingest_service=_StubIngestService(),
            graph_adapter=_StubGraphAdapter(),
        )
        assert "refresh_structure_graphs" in scheduler._jobs
        assert scheduler._jobs["refresh_structure_graphs"].interval_s == 1800.0

    def test_status_snapshot_includes_watcher_job(self):
        scheduler = MaintenanceScheduler(
            ingest_service=_StubIngestService(),
            graph_adapter=_StubGraphAdapter(),
            structure_watcher_enabled=True,
        )
        snapshot = scheduler.status_snapshot()
        assert "refresh_structure_graphs" in snapshot["jobs"]
