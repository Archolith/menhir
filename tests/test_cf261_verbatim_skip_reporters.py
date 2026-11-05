"""CF-261: reporters of episode status follow effective_processing_row, not the raw state.

A verbatim-skipped client is READY before the projection that extracts it is. Anything that
tells a client or operator an episode's status must report the projection's state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from menhir.domain.models import ProcessingState
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.mcp.tools.ops import list_enrichment_queue as queue_module
from menhir.services.ingest_intake import IngestIntakeMixin


def _skipped_client(projection_state: str | None) -> dict[str, Any]:
    return {
        "uuid": "client-1",
        "processing_state": "READY",
        "processing_substage": "verbatim_skipped",
        "extraction_skipped_for": "proj-1",
        "ingest_request_sha256": "digest",
        "skip_projection": (
            None if projection_state is None
            else {"uuid": "proj-1", "processing_state": projection_state}
        ),
    }


def test_replay_of_skipped_episode_reports_projection_state() -> None:
    result = IngestIntakeMixin._idempotent_replay_result(_skipped_client("ENRICHING"), "digest")
    assert result.idempotent_replay is True
    assert result.processing_state == "ENRICHING"


def test_replay_of_skipped_episode_with_lost_projection_reports_pending() -> None:
    result = IngestIntakeMixin._idempotent_replay_result(_skipped_client(None), "digest")
    assert result.processing_state == "PENDING"


def test_replay_of_skipped_episode_with_ready_projection_stays_ready() -> None:
    result = IngestIntakeMixin._idempotent_replay_result(_skipped_client("READY"), "digest")
    assert result.processing_state == "READY"


def test_replay_of_ordinary_episode_is_unchanged() -> None:
    row = {"uuid": "e", "processing_state": ProcessingState.PENDING, "ingest_request_sha256": "d"}
    assert IngestIntakeMixin._idempotent_replay_result(row, "d").processing_state == "PENDING"


class _FakeNeo4j:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def execute(self, query: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self.rows


def test_ingest_key_lookup_carries_skip_projection() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _FakeNeo4j([{
        "uuid": "client-1", "processing_state": "READY", "processing_substage": "verbatim_skipped",
        "extraction_skipped_for": "proj-1", "skip_projection": {"processing_state": "PENDING"},
        "namespace": "default", "ingest_request_sha256": "d",
    }])
    found = repo.find_episode_by_ingest_key("key")
    assert found is not None
    assert found["skip_projection"] == {"processing_state": "PENDING"}
    assert found["extraction_skipped_for"] == "proj-1"
    assert "skip_projection" in repo.neo4j.queries[0]


def test_queue_listing_query_carries_skip_projection() -> None:
    repo = EpisodeMaintenanceRepository()
    repo.neo4j = _FakeNeo4j([])
    repo.list_episode_processing(processing_states=["READY"])
    assert "skip_projection" in repo.neo4j.queries[0]
    assert "extraction_skipped_for" in repo.neo4j.queries[0]


class _QueueBackend:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_episode_processing(self, **_: Any) -> list[dict[str, Any]]:
        return self.rows

    async def get_queue_depth(self) -> int:
        return 0


def test_list_enrichment_queue_reports_projected_state(monkeypatch: Any) -> None:
    row = _skipped_client("ENRICHING")
    row["processing_stage"] = "verbatim_skipped"
    row["skip_projection"].update(processing_stage="extracting", processing_progress=40.0)
    tool = queue_module.ListEnrichmentQueueTool()
    monkeypatch.setattr(tool, "get_backend", lambda: _QueueBackend([row]), raising=False)
    text = asyncio.run(tool.endpoint(state="all"))
    assert " state=ENRICHING" in text
    assert " state=READY" not in text
    assert "queue_state=READY" in text


def test_memory_overview_counts_follow_projection_rule() -> None:
    repo = MemoryQueryRepository(_FakeNeo4j([{}]))
    try:
        repo.fetch_memory_overview()
    except Exception:  # noqa: BLE001 - only the first (counting) statement is asserted
        pass
    query = repo.neo4j.queries[0]
    assert "verbatim_skipped" in query
    assert "effective_state = 'READY'" in query
    assert "n.processing_state = 'READY' THEN 1" not in query


def test_explorer_ready_lists_follow_projection_rule() -> None:
    from menhir.explorer import app as explorer

    for fn in (explorer._queued_episodes, explorer._successful_episodes, explorer._recovered_episodes):
        repo = _FakeNeo4j([])
        fn(repo)
        assert "effective_state" in repo.queries[0], fn.__name__


# --- F11: the Cypher rule is the Python rule -------------------------------------------------

import pytest  # noqa: E402

from menhir.domain.verbatim_skip_status import (  # noqa: E402
    effective_processing_row,
    effective_state_cypher,
)

_PROJECTION_STATES = ["READY", "PENDING", "ENRICHING", "FAILED", None, "WEIRD", "GONE"]
_SKIP_FOR = ["proj-1", "", " ", None]


def _eval_effective_state_in_python(client: dict[str, Any], projection: dict[str, Any] | None) -> Any:
    row = dict(client, skip_projection=projection)
    return effective_processing_row(row)["processing_state"]


@pytest.mark.online
@pytest.mark.parametrize("skip_for", _SKIP_FOR)
@pytest.mark.parametrize("projection_state", _PROJECTION_STATES)
def test_effective_state_cypher_matches_effective_processing_row(
    test_neo4j_repo: Any, projection_state: Any, skip_for: Any
) -> None:
    projection = None if projection_state == "GONE" else {"processing_state": projection_state}
    client = {
        "processing_state": "READY",
        "processing_substage": "verbatim_skipped",
        "extraction_skipped_for": skip_for,
    }
    rows = test_neo4j_repo.execute(
        "OPTIONAL MATCH (z:NoSuchLabelCf261) "
        "WITH {processing_state: $cs, processing_substage: $sub, extraction_skipped_for: $skip} AS n, "
        "CASE WHEN $has_p THEN {processing_state: $ps} ELSE null END AS p "
        f"RETURN {effective_state_cypher('n', 'p')} AS s",
        params={
            "cs": "READY", "sub": "verbatim_skipped", "skip": skip_for,
            "has_p": projection is not None, "ps": projection_state,
        },
    )
    expected = _eval_effective_state_in_python(client, projection)
    assert rows[0]["s"] == str(getattr(expected, "value", expected))
