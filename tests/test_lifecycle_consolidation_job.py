"""Scheduler lifecycle jobs (P7 + D2): the maintenance scheduler runs consolidation AND decay on a
daily cadence, so demote-with-TTL / promotion (consolidation) and sharpness-recompute / compress
(decay) fire without depending on a process restart."""

from __future__ import annotations

import pytest

from menhir.services.lifecycle_service import ConsolidationResult, DecayResult
from menhir.services.maintenance_scheduler import MaintenanceScheduler


class _Ingest:
    def get_queue_depth(self) -> int:
        return 0


class _FakeLifecycle:
    """Minimal SchedulerLifecycleService stand-in that records recover_orphans / apply_decay calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.decay_calls = 0

    async def recover_orphans(self, max_age_hours: float = 4.0) -> ConsolidationResult:
        self.calls += 1
        return ConsolidationResult(
            promoted=2,
            deleted=1,
            conflicts_detected=0,
            skipped_pending=0,
            orphan_episodes_cleaned=3,
            demoted=4,
        )

    async def apply_decay(self) -> DecayResult:
        self.decay_calls += 1
        return DecayResult(
            edge_counts_synced=5,
            sharpness_recalculated=7,
            compressed=2,
            deleted=0,
            edges_bridged=1,
            orphan_subgraphs_cleaned=3,
        )


_COMMON = dict(ingest_service=_Ingest(), graph_adapter=object())


@pytest.mark.unit
def test_job_registered_only_when_lifecycle_present_and_enabled() -> None:
    # lifecycle_service present + enabled (default) -> job registered
    s_on = MaintenanceScheduler(**_COMMON, lifecycle_service=_FakeLifecycle())
    assert "consolidate_lifecycle" in s_on._jobs
    assert s_on._jobs["consolidate_lifecycle"].interval_s == 86400.0  # daily

    # explicitly disabled -> no job
    s_off = MaintenanceScheduler(
        **_COMMON, lifecycle_service=_FakeLifecycle(), lifecycle_consolidation_enabled=False
    )
    assert "consolidate_lifecycle" not in s_off._jobs

    # no lifecycle_service -> no job (nothing to run)
    s_none = MaintenanceScheduler(**_COMMON, lifecycle_service=None)
    assert "consolidate_lifecycle" not in s_none._jobs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runner_invokes_recover_orphans_and_maps_result() -> None:
    lifecycle = _FakeLifecycle()
    scheduler = MaintenanceScheduler(**_COMMON, lifecycle_service=lifecycle)

    result = await scheduler._make_consolidate_lifecycle()

    assert lifecycle.calls == 1
    assert result["promoted"] == 2
    assert result["deleted"] == 1
    assert result["demoted"] == 4
    assert result["orphan_episodes_cleaned"] == 3


@pytest.mark.unit
def test_decay_job_registered_only_when_lifecycle_present_and_enabled() -> None:
    # present + enabled (default) -> job registered daily
    s_on = MaintenanceScheduler(**_COMMON, lifecycle_service=_FakeLifecycle())
    assert "decay_lifecycle" in s_on._jobs
    assert s_on._jobs["decay_lifecycle"].interval_s == 86400.0

    # explicitly disabled -> no job
    s_off = MaintenanceScheduler(
        **_COMMON, lifecycle_service=_FakeLifecycle(), lifecycle_decay_enabled=False
    )
    assert "decay_lifecycle" not in s_off._jobs

    # no lifecycle_service -> no job
    s_none = MaintenanceScheduler(**_COMMON, lifecycle_service=None)
    assert "decay_lifecycle" not in s_none._jobs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decay_runner_invokes_apply_decay_and_maps_result() -> None:
    lifecycle = _FakeLifecycle()
    scheduler = MaintenanceScheduler(**_COMMON, lifecycle_service=lifecycle)

    result = await scheduler._make_decay_lifecycle()

    assert lifecycle.decay_calls == 1
    assert result["compressed"] == 2
    assert result["sharpness_recalculated"] == 7
    assert result["edges_bridged"] == 1
    assert result["orphan_subgraphs_cleaned"] == 3
