"""Recall pipeline: post-rank provenance fill (best-effort, one batched fetch)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


def _meta(uuid: str, name: str, **extra) -> dict:
    base = {
        "uuid": uuid,
        "name": name,
        "scope": "PERSISTENT",
        "type": "SEMANTIC",
        "content": f"{name} content",
        "summary": None,
        "last_accessed": datetime.now(timezone.utc) - timedelta(hours=1),
        "edge_count": 3,
        "sharpness": 0.5,
        "freshness": "ACTIVE",
        "user_flagged": False,
    }
    base.update(extra)
    return base


def _service(client, adapter) -> RecallService:
    return RecallService(
        graphiti_client=client, graph_adapter=adapter, scoring_service=ScoringService()
    )


def _seed(client, adapter, metas: list[dict]) -> None:
    client.search_scored_results = [
        (m["uuid"], m["name"], 0.9 - 0.05 * i) for i, m in enumerate(metas)
    ]
    adapter.candidate_metadata = metas


def _record_fetch(adapter, monkeypatch, rows_by_uuid: dict[str, dict]) -> list[dict]:
    """Replace fetch_result_provenance with a recorder that answers only the asked uuids."""
    calls: list[dict] = []

    def fake(uuids, namespace=None):
        calls.append({"uuids": list(uuids), "namespace": namespace})
        return [rows_by_uuid[u] for u in uuids if u in rows_by_uuid]

    monkeypatch.setattr(adapter, "fetch_result_provenance", fake)
    return calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_row_fills_agent_provenance_and_no_row_stays_none(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    _seed(stub_graphiti_client, stub_memory_graph_adapter,
          [_meta("e-agent", "Agent thing"), _meta("e-norow", "No row thing")])
    _record_fetch(stub_memory_graph_adapter, monkeypatch, {
        "e-agent": {"uuid": "e-agent", "sources": ["claude-code"],
                    "first_seen_at": "2026-01-01T00:00:00Z", "turn_evidence_ids": ["t2", "t1"]},
    })

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", update_access=False)

    by_uuid = {r.uuid: r for r in result.results}
    prov = by_uuid["e-agent"].provenance
    assert prov["said_by"] == "agent"
    assert prov["sources"] == ["claude-code"]
    assert prov["first_seen_at"] == "2026-01-01T00:00:00Z"
    assert prov["turn_evidence_ids"] == ["t1", "t2"]
    assert by_uuid["e-norow"].provenance is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_row_is_carried_as_user_on_the_scored_memory(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    _seed(stub_graphiti_client, stub_memory_graph_adapter, [_meta("e-user", "User thing")])
    _record_fetch(stub_memory_graph_adapter, monkeypatch, {
        "e-user": {"uuid": "e-user", "sources": ["user"], "first_seen_at": None,
                   "turn_evidence_ids": []},
    })

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", update_access=False)

    assert result.results[0].provenance["said_by"] == "user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mixed_and_unknown_are_derived_not_copied(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    _seed(stub_graphiti_client, stub_memory_graph_adapter,
          [_meta("e-mix", "Mixed"), _meta("e-unk", "Unknown")])
    _record_fetch(stub_memory_graph_adapter, monkeypatch, {
        "e-mix": {"uuid": "e-mix", "sources": ["user", "agent_inference"]},
        "e-unk": {"uuid": "e-unk", "sources": [None]},
    })

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", update_access=False)

    by_uuid = {r.uuid: r for r in result.results}
    assert by_uuid["e-mix"].provenance["said_by"] == "mixed"
    assert by_uuid["e-unk"].provenance["said_by"] == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_and_observation_types_are_not_sent_to_the_fetch(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    metas = [
        _meta("e-plain", "Plain"),
        _meta("e-state", "Scalar state", type="SCALAR_STATE"),
        _meta("e-hist", "Scalar history", type="SCALAR_HISTORY"),
        _meta("e-obs", "Observation", type="OBSERVATION"),
    ]
    _seed(stub_graphiti_client, stub_memory_graph_adapter, metas)
    # Offer a row for EVERY uuid, so an excluded item would be filled if it were sent/applied.
    calls = _record_fetch(stub_memory_graph_adapter, monkeypatch, {
        m["uuid"]: {"uuid": m["uuid"], "sources": ["claude-code"]} for m in metas
    })

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", update_access=False)

    returned = {r.uuid: r for r in result.results}
    assert "e-plain" in returned  # the included item survived ranking
    assert len(calls) == 1
    sent = set(calls[0]["uuids"])
    assert "e-plain" in sent
    assert not (sent & {"e-state", "e-hist", "e-obs"}), sent
    for uuid in ("e-state", "e-hist", "e-obs"):
        if uuid in returned:
            assert returned[uuid].provenance is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_view_kind_items_are_not_sent_to_the_fetch(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    metas = [
        _meta("e-plain", "Plain"),
        _meta("v-view", "A view", view_kind="scalar_state", view_current=True),
    ]
    _seed(stub_graphiti_client, stub_memory_graph_adapter, metas)
    calls = _record_fetch(stub_memory_graph_adapter, monkeypatch, {
        m["uuid"]: {"uuid": m["uuid"], "sources": ["claude-code"]} for m in metas
    })

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", update_access=False)

    returned = {r.uuid: r for r in result.results}
    assert "e-plain" in returned
    for call in calls:
        assert "v-view" not in call["uuids"]
    if "v-view" in returned:
        assert returned["v-view"].provenance is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_failure_leaves_results_with_no_provenance(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    _seed(stub_graphiti_client, stub_memory_graph_adapter,
          [_meta("e-1", "One"), _meta("e-2", "Two")])
    attempts: list[object] = []

    def boom(uuids, namespace=None):
        attempts.append(uuids)
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(stub_memory_graph_adapter, "fetch_result_provenance", boom)

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", update_access=False)

    assert attempts, "the fetch must have been attempted"
    assert {r.uuid for r in result.results} == {"e-1", "e-2"}
    assert all(r.provenance is None for r in result.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exactly_one_batched_fetch_per_recall_carrying_every_eligible_uuid(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    metas = [_meta(f"e-{i}", f"Thing {i}", namespace="ns-prov") for i in range(4)]
    _seed(stub_graphiti_client, stub_memory_graph_adapter, metas)
    calls = _record_fetch(stub_memory_graph_adapter, monkeypatch, {})

    result = await _service(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", namespace="ns-prov", update_access=False)

    assert len(result.results) == 4
    assert len(calls) == 1
    assert set(calls[0]["uuids"]) == {r.uuid for r in result.results}
    assert len(calls[0]["uuids"]) == len(set(calls[0]["uuids"]))
    assert calls[0]["namespace"] == "ns-prov"
