"""#925: never-retryable FAILED rows are parked so they cannot starve retryable ones.

The fake adapter applies the same exclusion rule as `fetch_failed_episode_retry_candidates`:
a row is skipped while it is parked for its CURRENT failure timestamp and the recheck window
has not passed. The Cypher itself is pinned below and exercised live in
`test_925_failed_retry_parking_live.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from menhir.infrastructure.episode_lifecycle import (
    RETRY_PARK_RECHECK_S,
    EpisodeLifecycleRepository,
)
from menhir.services.scheduler_tasks import retry_failed_enrichments

pytestmark = pytest.mark.unit

TERMINAL_ERROR = "Failed to extract JSON from LLM response"
RETRYABLE_ERROR = "Failed to load model"


class _Graph:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.park_calls: list[str] = []

    def add(self, uuid: str, *, error: str, failed_at: datetime, attempts: int = 1) -> None:
        self.rows[uuid] = {
            "uuid": uuid,
            "processing_state": "FAILED",
            "processing_attempts": attempts,
            "transient_retries": 0,
            "processing_error": error,
            "processing_completed_at": failed_at.isoformat(),
        }

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict]:
        now = datetime.now(timezone.utc)
        eligible = []
        for row in self.rows.values():
            if row["processing_state"] != "FAILED":
                continue
            parked_for = row.get("retry_parked_for")
            parked_at = row.get("retry_parked_at")
            if (
                parked_for is not None
                and parked_for == row["processing_completed_at"]
                and parked_at >= now - timedelta(seconds=RETRY_PARK_RECHECK_S)
            ):
                continue
            eligible.append(row)
        eligible.sort(key=lambda r: r["processing_completed_at"])
        return [
            {k: v for k, v in r.items() if not k.startswith("retry_parked")}
            for r in eligible[:limit]
        ]

    def park_failed_episode_retry(self, episode_uuid: str, *, failed_at: str, reason: str) -> bool:
        row = self.rows.get(episode_uuid)
        if row is None or row["processing_state"] != "FAILED":
            return False
        if row["processing_completed_at"] != failed_at:
            return False
        self.park_calls.append(episode_uuid)
        row["retry_parked_at"] = datetime.now(timezone.utc)
        row["retry_parked_for"] = failed_at
        row["retry_parked_reason"] = reason
        return True

    def linked_twin_artifact(self, episode_uuid: str) -> None:
        return None


class _Ingest:
    def __init__(self, graph: _Graph) -> None:
        self.graph = graph
        self.requeued: list[str] = []

    def get_queue_depth(self) -> int:
        return 0

    def get_max_enrichment_attempts(self) -> int:
        return 3

    def get_context_window_retry_attempts(self) -> int:
        return 6

    def get_llm_session_window_seconds(self) -> int:
        return 900

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        self.requeued.append(episode_uuid)
        # The real requeue moves the row out of FAILED once the worker claims it.
        self.graph.rows[episode_uuid]["processing_state"] = "ENRICHING"
        return True


def _seed(graph: _Graph, *, stuck: int) -> None:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(stuck):
        graph.add(f"stuck-{i:02d}", error=TERMINAL_ERROR, failed_at=base + timedelta(minutes=i))
    graph.add("retry-me", error=RETRYABLE_ERROR, failed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


async def _tick(graph: _Graph, ingest: _Ingest, events: list[dict]) -> dict:
    with patch(
        "menhir.services.scheduler_tasks.record_failure_event",
        side_effect=lambda **kw: events.append(kw),
    ):
        return await retry_failed_enrichments(ingest, graph, failed_retry_limit=50)


@pytest.mark.asyncio
async def test_retryable_row_behind_60_never_retryable_rows_is_requeued_within_two_ticks() -> None:
    graph = _Graph()
    _seed(graph, stuck=60)
    ingest = _Ingest(graph)
    events: list[dict] = []

    first = await _tick(graph, ingest, events)
    assert first["candidates"] == 50
    assert first["terminal"] == 50
    assert ingest.requeued == []

    second = await _tick(graph, ingest, events)
    assert second["candidates"] == 11
    assert ingest.requeued == ["retry-me"], "the retryable row was starved by never-retryable ones"


@pytest.mark.asyncio
async def test_parked_rows_get_one_failure_event_not_one_per_tick() -> None:
    graph = _Graph()
    _seed(graph, stuck=60)
    ingest = _Ingest(graph)
    events: list[dict] = []

    for _ in range(4):
        await _tick(graph, ingest, events)

    per_row: dict[str, int] = {}
    for event in events:
        per_row[event["episode_uuid"]] = per_row.get(event["episode_uuid"], 0) + 1
    stuck = {uuid: n for uuid, n in per_row.items() if uuid.startswith("stuck-")}
    assert len(stuck) == 60
    assert set(stuck.values()) == {1}, "a parked row kept writing a failure event every tick"
    assert len(graph.park_calls) == 60

    fifth = await _tick(graph, ingest, events)
    assert fifth["candidates"] == 0


@pytest.mark.asyncio
async def test_a_parked_row_that_is_requeued_and_fails_again_is_a_candidate_again() -> None:
    graph = _Graph()
    graph.add("ep-1", error=TERMINAL_ERROR, failed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    ingest = _Ingest(graph)
    events: list[dict] = []

    await _tick(graph, ingest, events)
    assert graph.rows["ep-1"]["retry_parked_for"] is not None
    assert (await _tick(graph, ingest, events))["candidates"] == 0

    # Operator requeue then a fresh, retryable failure. No path clears the marker: the new
    # failure timestamp alone must make the row eligible again.
    row = graph.rows["ep-1"]
    row["processing_state"] = "FAILED"
    row["processing_error"] = RETRYABLE_ERROR
    row["processing_completed_at"] = datetime(2026, 2, 1, tzinfo=timezone.utc).isoformat()

    third = await _tick(graph, ingest, events)
    assert third["candidates"] == 1
    assert ingest.requeued == ["ep-1"]


@pytest.mark.asyncio
async def test_a_park_is_rechecked_after_the_window() -> None:
    graph = _Graph()
    graph.add("ep-1", error=TERMINAL_ERROR, failed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    ingest = _Ingest(graph)
    events: list[dict] = []

    await _tick(graph, ingest, events)
    graph.rows["ep-1"]["retry_parked_at"] -= timedelta(seconds=RETRY_PARK_RECHECK_S + 60)

    assert (await _tick(graph, ingest, events))["candidates"] == 1
    assert len(events) == 2


@pytest.mark.asyncio
async def test_exhausted_rows_are_parked_and_backoff_waits_are_not() -> None:
    graph = _Graph()
    graph.add("exhausted", error=RETRYABLE_ERROR, failed_at=datetime(2025, 1, 1, tzinfo=timezone.utc), attempts=9)
    graph.add("transient", error=RETRYABLE_ERROR, failed_at=datetime(2025, 1, 2, tzinfo=timezone.utc))
    graph.rows["transient"]["transient_retries"] = 99
    graph.add("backoff", error=RETRYABLE_ERROR, failed_at=datetime.now(timezone.utc))
    ingest = _Ingest(graph)
    events: list[dict] = []

    result = await _tick(graph, ingest, events)

    assert result["exhausted"] == 2
    assert result["waiting"] == 1
    assert sorted(graph.park_calls) == ["exhausted", "transient"]
    assert "retry_parked_for" not in graph.rows["backoff"]


def test_candidate_query_excludes_only_rows_parked_for_the_current_failure() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = []

    repo.fetch_failed_episode_retry_candidates(limit=50)

    query = repo.neo4j.execute.call_args.args[0]
    params = repo.neo4j.execute.call_args.kwargs["params"]
    failed_at = "coalesce(n.processing_completed_at, n.processing_started_at, n.queued_at, n.created_at)"
    assert "n.processing_state = 'FAILED'" in query
    assert "properties(n)['retry_parked_for'] IS NULL" in query
    assert f"properties(n)['retry_parked_for'] <> toString({failed_at})" in query
    assert "duration({seconds: $park_recheck_s})" in query
    assert f"ORDER BY retry_tier ASC, {failed_at} ASC" in query
    assert params == {"limit": 50, "park_recheck_s": RETRY_PARK_RECHECK_S}


def test_park_write_is_fenced_on_state_and_the_failure_it_was_judged_for() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = [{"parked": 1}]

    assert repo.park_failed_episode_retry("ep-1", failed_at="2025-01-01T00:00:00Z", reason="terminal")

    query = repo.neo4j.execute.call_args.args[0]
    params = repo.neo4j.execute.call_args.kwargs["params"]
    assert "n.processing_state = 'FAILED'" in query
    assert "= $failed_at" in query
    assert "n.retry_parked_at = datetime()" in query
    assert "n.retry_parked_for = $failed_at" in query
    assert "n.retry_parked_reason = $reason" in query
    assert "processing_state =" not in query.split("SET", 1)[1], "parking must not change state"
    assert params == {"episode_uuid": "ep-1", "failed_at": "2025-01-01T00:00:00Z", "reason": "terminal"}


def test_park_reports_a_stale_failure_as_not_parked() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = [{"parked": 0}]

    assert not repo.park_failed_episode_retry("ep-1", failed_at="old", reason="terminal")
