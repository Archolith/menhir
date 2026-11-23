"""#651 / #652: pending and auto-resolved conflict members are flagged in recall, never refused."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

import pytest

from menhir.domain.belief import QueryIntent
from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
from menhir.domain.oracles import CandidateMemory, QueryContext
from menhir.domain.truth.labels import WardenLabel
from menhir.domain.warden import ContradictionWarden, WardenContext, WardenDecision
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.mcp.tools.ops.get_memory_stats import GetMemoryStatsTool, get_memory_stats
from menhir.services.assertion_pipeline import AssertionPipeline

pytestmark = pytest.mark.unit

INTENTS = [QueryIntent.CURRENT, QueryIntent.HISTORICAL]


# --- warden unit tests -------------------------------------------------------

@pytest.mark.parametrize("intent", INTENTS)
def test_pending_is_flagged_possible_conflict_never_refused(intent) -> None:
    v = ContradictionWarden().evaluate(WardenContext("c", intent=intent, pending_conflict=True))
    assert v.decision is WardenDecision.FLAG
    assert v.label is WardenLabel.POSSIBLE_CONFLICT
    assert v.reason == "possible conflict, not yet reviewed"


@pytest.mark.parametrize("intent", INTENTS)
def test_auto_resolved_is_flagged_unsettled_never_refused(intent) -> None:
    v = ContradictionWarden().evaluate(WardenContext("c", intent=intent, unsettled_conflict=True))
    assert v.decision is WardenDecision.FLAG
    assert v.label is WardenLabel.UNSETTLED_CONFLICT
    assert v.reason == "contradiction confirmed earlier, auto-resolved without a decision"


@pytest.mark.parametrize("intent", INTENTS)
def test_clean_context_is_admitted(intent) -> None:
    v = ContradictionWarden().evaluate(WardenContext("c", intent=intent))
    assert v.decision is WardenDecision.ADMIT and v.label is None


def test_unresolved_unchanged() -> None:
    cur = ContradictionWarden().evaluate(WardenContext("c", recorded_conflict=True))
    assert cur.decision is WardenDecision.REFUSE
    hist = ContradictionWarden().evaluate(
        WardenContext("c", intent=QueryIntent.HISTORICAL, recorded_conflict=True, pending_conflict=True)
    )
    assert hist.decision is WardenDecision.FLAG and hist.label is WardenLabel.CONFLICT


# --- pipeline-level ------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["current", "historical"])
@pytest.mark.parametrize("group,status,expected_label", [
    ("g", "pending_llm_review", "possible_conflict"),
    (None, "pending_llm_review", None),
    (None, "auto-resolved", "unsettled_conflict"),
    ("g", "auto-resolved", "unsettled_conflict"),
    (None, "resolved", None),
    (None, "false_positive", None),
])
async def test_pipeline_labels_pending_and_auto_resolved(intent, group, status, expected_label) -> None:
    pipe = AssertionPipeline(LogSpaceOracleCombiner(), auto_intent=False, contradiction_interrupt=True)
    candidate = CandidateMemory(id="c", content="configuration fact", metadata={
        "similarity": .85, "evidence_kinds": ("git",),
        "conflict_group_id": group, "conflict_status": status,
    })
    outcome = await pipe.run(QueryContext(text="configuration", intent=intent), [candidate])
    result = outcome.ranked[0]
    assert not outcome.refused
    assert result.label == expected_label
    assert result.decision is (WardenDecision.FLAG if expected_label else WardenDecision.ADMIT)


# --- stats ---------------------------------------------------------------------

class _Neo4j:
    def __init__(self, row: dict) -> None:
        self._row = row
        self.queries: list[str] = []

    def execute(self, query: str, *args, **kwargs):
        self.queries.append(query)
        if "ADMITTED_ON" in query:
            return [{"turn_evidence_count": 0, "admission_edge_count": 0}]
        return [self._row]


def test_overview_returns_pending_conflict_keys() -> None:
    when = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    neo = _Neo4j({"total_memories": 1, "pending_conflict_groups": 2, "oldest_pending_conflict_at": when})
    overview = MemoryQueryRepository(neo).fetch_memory_overview()
    assert overview["pending_conflict_groups"] == 2
    assert overview["oldest_pending_conflict_at"] == when.isoformat(timespec="microseconds")
    assert "pending_llm_review" in neo.queries[0]


def test_overview_pending_conflict_defaults_when_absent() -> None:
    overview = MemoryQueryRepository(_Neo4j({"total_memories": 0})).fetch_memory_overview()
    assert overview["pending_conflict_groups"] == 0
    assert overview["oldest_pending_conflict_at"] is None


async def _stats_output(overview: dict) -> str:
    backend = MagicMock()
    backend.fetch_memory_overview = AsyncMock(return_value=overview)
    backend.fetch_operation_stats = AsyncMock(return_value=[])
    backend.fetch_failure_summary = AsyncMock(return_value={})
    backend.fetch_enrichment_rate = AsyncMock(return_value={
        "total": 0, "successes": 0, "avg_duration_ms": 0, "success_rate_pct": 100,
    })
    backend.fetch_lifecycle_summary = AsyncMock(return_value={})
    backend.get_queue_depth = AsyncMock(return_value=0)
    backend.circuit_breaker_snapshots = AsyncMock(return_value={})
    with patch.object(GetMemoryStatsTool, "get_backend", return_value=backend):
        return str(await get_memory_stats(since_hours=24))


@pytest.mark.asyncio
async def test_get_memory_stats_renders_pending_conflicts() -> None:
    out = await _stats_output({
        "entity_count": 1, "episode_count": 1,
        "pending_conflict_groups": 3, "oldest_pending_conflict_at": "2026-11-01T12:00:00+00:00",
    })
    assert "Pending conflicts:      3" in out
    assert "Oldest pending conflict: 2026-11-01T12:00:00+00:00" in out


@pytest.mark.asyncio
async def test_get_memory_stats_renders_zero_pending_without_oldest() -> None:
    out = await _stats_output({"entity_count": 1, "episode_count": 1})
    assert "Pending conflicts:      0" in out
    assert "Oldest pending conflict" not in out


# --- queue health --------------------------------------------------------------

class _Ingest:
    def get_queue_depth(self) -> int:
        return 0

    def get_failed_enrichment_count(self) -> int:
        return 0


class _Adapter:
    def __init__(self, overview: dict) -> None:
        self._overview = overview

    def fetch_memory_overview(self) -> dict:
        return self._overview

    def fetch_failed_error_signatures(self, limit: int = 25) -> list:
        return []


@pytest.mark.asyncio
async def test_queue_health_warns_on_stale_pending_conflict(caplog) -> None:
    from menhir.services.scheduler_tasks import observe_queue_health

    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    with caplog.at_level(logging.WARNING, logger="menhir.services.scheduler_tasks"):
        result = await observe_queue_health(
            _Ingest(), _Adapter({"pending_conflict_groups": 2, "oldest_pending_conflict_at": old})
        )
    assert result["pending_conflict_groups"] == 2
    assert result["oldest_pending_conflict_at"] == old
    assert any("STALE PENDING CONFLICTS" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_queue_health_quiet_on_fresh_or_absent_pending_conflict(caplog) -> None:
    from menhir.services.scheduler_tasks import observe_queue_health

    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with caplog.at_level(logging.WARNING, logger="menhir.services.scheduler_tasks"):
        await observe_queue_health(
            _Ingest(), _Adapter({"pending_conflict_groups": 1, "oldest_pending_conflict_at": fresh})
        )
        await observe_queue_health(_Ingest(), _Adapter({}))
    assert not any("STALE PENDING CONFLICTS" in r.getMessage() for r in caplog.records)
