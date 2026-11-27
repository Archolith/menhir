"""#923 / #922: recall must not silently degrade or cut what it reports.

- #923.1 stale labels come from rows for the returned memories, not a global top 200.
- #923.2 a failed observation/authority lane rolls back and says so.
- #923 c1 an adjacency failure carries a note.
- #923 c2 the stale-label name fallback applies only when an item has no uuid.
- #922 with the cross-encoder rerank on, injected candidates are scored and authority is not cut.
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services import cross_encoder_rerank as cer
from menhir.services.stale_labeling import label_stale_anchors
from tests.test_scalar_history_recall import (
    _build_svc,
    _postcard_history_view,
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import (
    QUERY,
    _other_slot_hit,
    _state_view,
    _svc,
    _wire,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #923 comment 2


def test_name_fallback_does_not_fire_for_an_unmatched_uuid():
    """A fresh memory sharing its name with a stale-anchored one stays fresh."""
    items = [{"uuid": "fresh-1", "name": "foo behavior"}]
    stale = [{"memory_uuid": "stale-1", "name": "foo behavior", "path": "src/foo.py",
              "dirty_at": "t1", "anchored_at": "t0"}]
    label_stale_anchors(items, stale)
    assert items[0]["stale_anchor"] is False
    assert items[0].get("path") is None


def test_name_fallback_still_applies_without_a_uuid():
    items = [{"name": "foo behavior"}]
    label_stale_anchors(items, [{"memory_uuid": "stale-1", "name": "foo behavior", "path": "p"}])
    assert items[0]["stale_anchor"] is True


# --------------------------------------------------------------------------- #923.1 repository


class _RecordingNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append((query, params or {}))
        return []


def test_stale_query_can_be_restricted_to_memory_uuids():
    from menhir.domain.namespace import tenant_scope_cypher
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    neo4j = _RecordingNeo4j()
    ToolEventRepository(neo4j).stale_anchored_memories(
        project="menhir", namespace="tenant-a", memory_uuids=["m1", "m2"])
    query, params = neo4j.calls[0]
    assert "sem.uuid IN $memory_uuids" in query
    assert params["memory_uuids"] == ["m1", "m2"]
    # Tenancy and project semantics are unchanged.
    assert tenant_scope_cypher("sem") in query
    assert params["tenant_namespaces"] == ["tenant-a"]
    assert params["project"] == "menhir"


def test_stale_query_without_memory_uuids_stays_unrestricted():
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    neo4j = _RecordingNeo4j()
    ToolEventRepository(neo4j).stale_anchored_memories()
    assert neo4j.calls[0][1]["memory_uuids"] is None


# --------------------------------------------------------------------------- #923.1 recall


@pytest.mark.asyncio
async def test_recall_labels_a_returned_memory_outside_the_global_top_200(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    """The adapter behaves like the real query: unrestricted, it returns the 200 most recently
    dirtied anchors (none of them ours); restricted, it returns the rows for those uuids."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    seen: list[dict] = []

    def _stale(**kwargs):
        seen.append(kwargs)
        uuids = kwargs.get("memory_uuids")
        if uuids is None:
            return [{"memory_uuid": f"other-{i}", "path": "x.py", "dirty_at": "t9",
                     "anchored_at": "t0"} for i in range(200)]
        return [{"memory_uuid": u, "path": "src/foo.py", "dirty_at": "t1", "anchored_at": "t0"}
                for u in uuids if u == "entity-1"]

    stub_memory_graph_adapter.stale_anchored_memories = _stale
    res = await _build_svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "test query", update_access=False)
    by_uuid = {r.uuid: r.stale_anchor_info for r in res.results}
    assert by_uuid["entity-1"]["stale_anchor"] is True
    assert by_uuid["entity-1"]["path"] == "src/foo.py"
    assert by_uuid["entity-2"]["stale_anchor"] is False
    assert set(seen[0]["memory_uuids"]) == {"entity-1", "entity-2"}


# --------------------------------------------------------------------------- #923 comment 1


def test_compute_adjacency_failure_appends_a_note():
    from menhir.services.recall_support import RecallSupportMixin

    class _Adapter:
        def fetch_adjacency_pairs(self, *_args):
            raise RuntimeError("adjacency unavailable")

    service = RecallSupportMixin()
    service.graph_adapter = _Adapter()
    notes: list[str] = []
    assert asyncio.run(service._compute_adjacency(["a", "b"], None, None,
                                                  degradation_notes=notes)) == ({}, {})
    assert notes and "adjacency" in notes[0].lower()


@pytest.mark.asyncio
async def test_recall_notes_an_adjacency_failure(stub_graphiti_client, stub_memory_graph_adapter):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)

    def _fail(*_a, **_k):
        raise RuntimeError("adjacency unavailable")

    stub_memory_graph_adapter.fetch_adjacency_pairs = _fail
    res = await _build_svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "test query", update_access=False)
    assert {r.uuid for r in res.results} == {"entity-1", "entity-2"}
    assert "Graph adjacency unavailable" in (res.note or "")


@pytest.mark.asyncio
async def test_recall_without_adjacency_failure_has_no_adjacency_note(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    res = await _build_svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "test query", update_access=False)
    assert "adjacency" not in (res.note or "").lower()


# --------------------------------------------------------------------------- #923.2


def _proj_pool(client, adapter):
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:
        row["namespace"] = "proj"


def _wire_second_slot_failure(adapter):
    """Two slots: postcard_count (best cosine, gets a View) then coin_count, whose View lookup
    raises -- after the first slot's candidate and verdict were already added."""
    other = _other_slot_hit()
    other["cosine"] = 0.90
    _wire(adapter, state_view=_state_view(), extra_hits=[other])

    def _view(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        if attribute == "coin_count":
            raise RuntimeError("view backend down")
        return _state_view()

    adapter.fetch_current_scalar_view_for_slot = _view


@pytest.mark.asyncio
async def test_failed_authority_lane_keeps_no_partial_changes_and_notes_it(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _proj_pool(stub_graphiti_client, stub_memory_graph_adapter)
    _wire_second_slot_failure(stub_memory_graph_adapter)
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        QUERY, namespace="proj", limit=10)
    ids = {r.uuid for r in res.results}
    # The authority stage rolled back whole: no View, no verdict for the slot that succeeded.
    assert "sv-1" not in ids
    assert not res.authority_layer
    # The observation stage had completed, so its candidates stay (committed stage).
    assert {"obs-postcards-1", "obs-other-1"} <= ids
    assert {"entity-1", "entity-2"} <= ids
    assert "Scalar authority lane failed" in (res.note or "")


@pytest.mark.asyncio
async def test_failed_observation_search_is_noted(stub_graphiti_client, stub_memory_graph_adapter):
    _proj_pool(stub_graphiti_client, stub_memory_graph_adapter)
    _wire(stub_memory_graph_adapter, state_view=_state_view())

    def _down(*_a, **_k):
        raise RuntimeError("assertion index down")

    stub_memory_graph_adapter.search_assertion_embeddings = _down
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        QUERY, namespace="proj", limit=10)
    ids = {r.uuid for r in res.results}
    assert {"entity-1", "entity-2"} <= ids
    assert "sv-1" not in ids and not res.authority_layer
    assert "Scalar observation lane failed" in (res.note or "")


@pytest.mark.asyncio
async def test_lane_that_completes_still_commits(stub_graphiti_client, stub_memory_graph_adapter):
    """Positive control: same wiring with no failure keeps the View and its verdict."""
    _proj_pool(stub_graphiti_client, stub_memory_graph_adapter)
    _wire_second_slot_failure(stub_memory_graph_adapter)
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            _state_view() if attribute == "postcard_count" else None)
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        QUERY, namespace="proj", limit=10)
    assert "sv-1" in {r.uuid for r in res.results}
    assert any(v.view_uuid == "sv-1" for v in (res.authority_layer or ()))
    assert "Scalar observation lane failed" not in (res.note or "")


# --------------------------------------------------------------------------- #923.2 history lane


def _wire_history(adapter, second_slot_error):
    """History-only mode, two slots: postcard_count (best cosine) yields a history View, then
    coin_count's history read raises -- after the first slot's candidate and verdict were added."""
    other = _other_slot_hit()
    other["cosine"] = 0.90
    _setup_observation_lane(adapter, obs_hit=_postcard_observation_hit() + [other])

    def _history(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        if attribute == "coin_count" and second_slot_error is not None:
            raise second_slot_error
        return _postcard_history_view() if attribute == "postcard_count" else None

    adapter.fetch_scalar_history = _history


async def _history_recall(client, adapter, second_slot_error):
    _proj_pool(client, adapter)
    _wire_history(adapter, second_slot_error)
    svc = _build_svc(client, adapter, scalar_view_authority_enabled=False,
                     scalar_history_enabled=True)
    return await svc.recall(QUERY, namespace="proj", limit=10)


def _history_additions(res):
    ids = {r.uuid for r in res.results}
    verdicts = [v for v in (res.authority_layer or ()) if v.kind == "history"]
    return ids, verdicts


@pytest.mark.asyncio
async def test_history_lane_that_completes_commits(stub_graphiti_client, stub_memory_graph_adapter):
    """Positive control: without the failure the first slot's history View surfaces."""
    res = await _history_recall(stub_graphiti_client, stub_memory_graph_adapter, None)
    ids, verdicts = _history_additions(res)
    assert "history-view-postcards" in ids
    assert [v.view_uuid for v in verdicts] == ["history-view-postcards"]
    assert "Scalar history lane failed" not in (res.note or "")


@pytest.mark.asyncio
async def test_failed_history_lane_keeps_no_partial_changes_and_notes_it(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    res = await _history_recall(stub_graphiti_client, stub_memory_graph_adapter,
                                RuntimeError("history backend down"))
    ids, verdicts = _history_additions(res)
    assert "history-view-postcards" not in ids
    assert verdicts == []
    assert {"entity-1", "entity-2"} <= ids
    assert "Scalar history lane failed" in (res.note or "")


@pytest.mark.asyncio
async def test_history_lane_breaker_refusal_rolls_back_without_a_note(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    """#642: a breaker refusal is expected and carries its own breaker note; no lane note."""
    from menhir.infrastructure.circuit_breaker import CircuitOpenError

    res = await _history_recall(stub_graphiti_client, stub_memory_graph_adapter,
                                CircuitOpenError("embed circuit open", snapshot={}))
    ids, verdicts = _history_additions(res)
    assert "history-view-postcards" not in ids
    assert verdicts == []
    assert "Scalar history lane failed" not in (res.note or "")


# --------------------------------------------------------------------------- #922


class _PrefixScorer:
    label = "fake"
    device = "cpu"

    def __init__(self, by_prefix: dict[str, float]):
        self.by_prefix = by_prefix
        self.calls: list[list[str]] = []

    def score(self, query, passages):
        self.calls.append(list(passages))
        return [next(v for k, v in self.by_prefix.items() if p.startswith(k)) for p in passages]


def _rerank_scorer(monkeypatch, by_prefix):
    scorer = _PrefixScorer(by_prefix)
    monkeypatch.setattr(cer, "load_reranker", lambda *_: scorer)
    return scorer


_SCORES = {"Test Entity": 5.0, "Another Entity": 4.0, "17 postcards": 3.0, "12 coins": 2.5,
           "postcards: 37": -5.0}


async def _rerank_recall(client, adapter, *, top_n, max_results):
    _proj_pool(client, adapter)
    _wire(adapter, state_view=_state_view())
    return await _svc(client, adapter, rerank_model_dir="m").recall(
        QUERY, namespace="proj", tuning=RetrievalTuningConfig(
            enable_cross_encoder_rerank=True, rerank_top_n=top_n,
            rerank_max_results=max_results))


@pytest.mark.asyncio
async def test_injected_candidates_are_in_the_rerank_head(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """rerank_top_n = the number of search hits: before #922 only the hits were scored and the
    injected View was floored behind them."""
    scorer = _rerank_scorer(monkeypatch, _SCORES)
    await _rerank_recall(stub_graphiti_client, stub_memory_graph_adapter, top_n=2, max_results=7)
    scored = scorer.calls[0]
    assert len(scored) == 2
    assert any(p.startswith("postcards: 37") for p in scored)
    assert any(p.startswith("17 postcards") for p in scored)


@pytest.mark.asyncio
async def test_rerank_cap_does_not_cut_the_injected_authority_view(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """The cross-encoder ranks the View last; the default cap still keeps it, displacing the
    lowest-ranked other result, so authority_layer never reports a View that is not returned."""
    _rerank_scorer(monkeypatch, _SCORES)
    res = await _rerank_recall(stub_graphiti_client, stub_memory_graph_adapter,
                               top_n=50, max_results=2)
    ids = [r.uuid for r in res.results]
    assert len(ids) == 2
    assert "sv-1" in ids
    assert ids[0] == "entity-1"
    reported = {v.view_uuid for v in (res.authority_layer or ()) if v.view_uuid}
    assert reported == {"sv-1"}
    assert reported <= set(ids)
    assert "capped the default result limit at 2" in (res.note or "")


def test_cut_keeping_is_a_plain_cut_when_nothing_is_past_it():
    from types import SimpleNamespace

    from menhir.services.recall_pipeline import _cut_keeping

    rows = [SimpleNamespace(uuid=u) for u in ("a", "b", "c", "d")]
    assert [r.uuid for r in _cut_keeping(rows, 2, {"a"})] == ["a", "b"]
    assert [r.uuid for r in _cut_keeping(rows, 2, {"d"})] == ["a", "d"]
    assert [r.uuid for r in _cut_keeping(rows, 0, {"d"})] == []
    assert [r.uuid for r in _cut_keeping(rows, 9, set())] == ["a", "b", "c", "d"]
