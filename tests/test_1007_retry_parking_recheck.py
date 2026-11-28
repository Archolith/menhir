"""#1007: due-for-recheck parked rows cannot crowd out unparked ones, and a parked row whose twin
link appears later is re-admitted at once so finalize-from-link closes it.

The fake adapter applies the same tier rule as `fetch_failed_episode_retry_candidates`: tier 0
not parked for this failure, tier 1 parked with a twin link, tier 2 parked and due for recheck,
tier 3 excluded; ordered by tier, then failure time. The Cypher is pinned below and exercised
live in `test_925_failed_retry_parking_live.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
FAILED_AT = "coalesce(n.processing_completed_at, n.processing_started_at, n.queued_at, n.created_at)"


class _Graph:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.twins: set[str] = set()
        self.ready: list[str] = []

    def add(self, uuid: str, *, error: str, failed_at: datetime, attempts: int = 1) -> None:
        self.rows[uuid] = {
            "uuid": uuid,
            "processing_state": "FAILED",
            "processing_attempts": attempts,
            "transient_retries": 0,
            "processing_error": error,
            "processing_completed_at": failed_at.isoformat(),
        }

    def park(self, uuid: str, *, age_s: int) -> None:
        row = self.rows[uuid]
        row["retry_parked_at"] = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        row["retry_parked_for"] = row["processing_completed_at"]
        row["retry_parked_reason"] = "terminal"

    def _has_link(self, row: dict) -> bool:
        return row.get("resolved_episode_uuid") is not None and row["resolved_episode_uuid"] in self.twins

    def _tier(self, row: dict) -> int:
        parked_for = row.get("retry_parked_for")
        if parked_for is None or parked_for != row["processing_completed_at"]:
            return 0
        if self._has_link(row):
            return 1
        if row["retry_parked_at"] < datetime.now(timezone.utc) - timedelta(seconds=RETRY_PARK_RECHECK_S):
            return 2
        return 3

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict]:
        eligible = [
            (self._tier(r), r["processing_completed_at"], r)
            for r in self.rows.values()
            if r["processing_state"] == "FAILED"
        ]
        eligible = sorted((e for e in eligible if e[0] < 3), key=lambda e: (e[0], e[1]))
        return [
            {k: v for k, v in r.items() if not k.startswith("retry_parked") and k != "resolved_episode_uuid"}
            for _tier, _at, r in eligible[:limit]
        ]

    def park_failed_episode_retry(self, episode_uuid: str, *, failed_at: str, reason: str) -> bool:
        row = self.rows.get(episode_uuid)
        if row is None or row["processing_state"] != "FAILED" or row["processing_completed_at"] != failed_at:
            return False
        row["retry_parked_at"] = datetime.now(timezone.utc)
        row["retry_parked_for"] = failed_at
        row["retry_parked_reason"] = reason
        return True

    def linked_twin_artifact(self, episode_uuid: str) -> dict | None:
        row = self.rows.get(episode_uuid)
        if row is None or not self._has_link(row):
            return None
        return {
            "resolved_episode_uuid": row["resolved_episode_uuid"],
            "entity_uuids": [],
            "edge_uuids": [],
            "namespace": "default",
            "fence_namespace_key": "default",
            # A finished publish (#905): the outcome recorded with the link makes it a completion.
            "extraction_outcome": "empty",
            "completed": True,
        }

    def stamp_ingest_metadata(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(nodes_touched=1, edges_touched=0)

    def record_retention_sources(self, **_: object) -> int:
        return 0

    def mark_episode_ready(self, episode_uuid: str, *, required_state: str | None = None, **_: object) -> bool:
        row = self.rows[episode_uuid]
        if required_state is not None and row["processing_state"] != required_state:
            return False
        row["processing_state"] = "READY"
        self.ready.append(episode_uuid)
        return True


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
        self.graph.rows[episode_uuid]["processing_state"] = "ENRICHING"
        return True


async def _tick(graph: _Graph, ingest: _Ingest) -> dict:
    with patch("menhir.services.scheduler_tasks.record_failure_event"):
        return await retry_failed_enrichments(ingest, graph, failed_retry_limit=50)


def _seed_due_herd(graph: _Graph, n: int) -> None:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        uuid = f"parked-{i:02d}"
        graph.add(uuid, error=TERMINAL_ERROR, failed_at=base + timedelta(minutes=i))
        graph.park(uuid, age_s=RETRY_PARK_RECHECK_S + 60)


@pytest.mark.asyncio
async def test_unparked_row_is_fetched_on_the_first_tick_ahead_of_a_due_recheck_herd() -> None:
    graph = _Graph()
    _seed_due_herd(graph, 60)
    graph.add("retry-me", error=RETRYABLE_ERROR, failed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ingest = _Ingest(graph)

    first = await _tick(graph, ingest)

    assert first["candidates"] == 50
    assert ingest.requeued == ["retry-me"], "a due recheck herd crowded out an unparked row"


@pytest.mark.asyncio
async def test_parked_row_that_gains_a_twin_link_is_a_candidate_again_and_is_finalized() -> None:
    graph = _Graph()
    graph.add("ep-1", error=TERMINAL_ERROR, failed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    ingest = _Ingest(graph)

    await _tick(graph, ingest)
    assert graph.rows["ep-1"]["retry_parked_for"] is not None
    assert (await _tick(graph, ingest))["candidates"] == 0

    # The twin link lands after the park, well inside the recheck window.
    graph.twins.add("twin-1")
    graph.rows["ep-1"]["resolved_episode_uuid"] = "twin-1"

    result = await _tick(graph, ingest)
    assert result["candidates"] == 1
    # #906: the scheduler hands the row to the worker, whose finalize-from-link marks it READY.
    assert result["requeued"] == 1
    assert ingest.requeued == ["ep-1"]
    assert graph.ready == []


@pytest.mark.asyncio
async def test_a_link_to_a_missing_twin_does_not_readmit_a_parked_row() -> None:
    graph = _Graph()
    graph.add("ep-1", error=TERMINAL_ERROR, failed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    graph.park("ep-1", age_s=60)
    graph.rows["ep-1"]["resolved_episode_uuid"] = "gone"

    assert (await _tick(graph, _Ingest(graph)))["candidates"] == 0


@pytest.mark.asyncio
async def test_linked_parked_rows_sort_ahead_of_the_due_recheck_herd() -> None:
    graph = _Graph()
    _seed_due_herd(graph, 60)
    graph.add("linked", error=TERMINAL_ERROR, failed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    graph.park("linked", age_s=60)
    graph.twins.add("twin-1")
    graph.rows["linked"]["resolved_episode_uuid"] = "twin-1"

    ingest = _Ingest(graph)
    result = await _tick(graph, ingest)

    assert result["requeued"] == 1
    assert ingest.requeued[0] == "linked"


def _candidate_query() -> tuple[str, dict]:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = []
    repo.fetch_failed_episode_retry_candidates(limit=50)
    return repo.neo4j.execute.call_args.args[0], repo.neo4j.execute.call_args.kwargs["params"]


def test_candidate_query_orders_by_tier_before_failure_time() -> None:
    query, params = _candidate_query()

    assert "AS retry_tier" in query
    assert "WHERE retry_tier < 3" in query
    assert f"ORDER BY retry_tier ASC, {FAILED_AT} ASC" in query
    tier = query.split("CASE WHEN", 1)[1].split("AS retry_tier", 1)[0]
    # Tier 0 (not parked for this failure) < tier 1 (twin link) < tier 2 (recheck due).
    assert tier.index("THEN 0") < tier.index("EXISTS { MATCH (g:Episodic") < tier.index("THEN 1")
    assert tier.index("THEN 1") < tier.index("duration({seconds: $park_recheck_s})") < tier.index("THEN 2")
    assert "ELSE 3 END" in tier
    assert params == {"limit": 50, "park_recheck_s": RETRY_PARK_RECHECK_S}


def test_candidate_query_uses_the_same_twin_link_predicate_as_finalize_from_link() -> None:
    query, _params = _candidate_query()
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = []
    repo.linked_twin_artifact("ep-1")
    link_query = repo.neo4j.execute.call_args.args[0]

    for fragment in ("n.resolved_episode_uuid IS NOT NULL", "(g:Episodic {uuid: n.resolved_episode_uuid})"):
        assert fragment in link_query
        assert fragment in query
    # linked_twin_artifact also requires no ErasureMarker; the candidate query already filters that.
    assert "NOT EXISTS { MATCH (:ErasureMarker {subject_uuid: n.uuid}) }" in query
