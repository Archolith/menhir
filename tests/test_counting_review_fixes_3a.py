"""Counting lane unit 3a review fixes, offline.

Pins the review-round corrections: occurrence turn identity (grounding_id first), the per-write
fence (write_guard), the observation / history lanes and rebuild respecting the counting kill
switch, the adapter's include_occurrences default, and the stated-after-counted audit clear.
The live Cypher side is in test_counting_review_fixes_live.py.
"""

from __future__ import annotations

import json

import pytest

from menhir.domain.scalar_state_fold import BASIS_COUNTED, fold_assertions
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import CountingFenceLost
from menhir.services.recall_policies import _scalar_history_has_occurrences
from menhir.services.recall_service import RecallService
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.scoring_service import ScoringService
from menhir.services.typed_scalar_perception import TypedScalarPerceptionService
from tests.test_counting_correctness_3a import (
    _FenceGraph,
    _occ,
    _row,
    _run,
    fake_perception,  # noqa: F401 - fixture
)
from tests.test_scalar_history_recall import (
    _postcard_history_view,
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_history_service import FakeAssertionSource, FakeViewSink, _assertion
from tests.test_typed_scalar_self_binding import _Ep, _llm, _SelfFakeAdapter, _SelfFakeService


def _only_state(result):
    assert len(result.states) == 1, result
    return result.states[0]


# --------------------------------------------------------------------------- 1. turn key

@pytest.mark.unit
def test_empty_episode_uuid_rows_with_different_grounding_ids_are_two_turns():
    # Fails if the turn is identified by episode_uuid only: both rows would have an empty turn,
    # skip the dedup (fine) -- but the point is they must COUNT as two. A regression that treats
    # "" episodes as one turn (or drops them) changes the total.
    rows = [_occ("o1", "", grounding_id="turn-1"), _occ("o2", "", grounding_id="turn-2")]
    assert _only_state(fold_assertions(rows, include_occurrences=True)).value == 2


@pytest.mark.unit
def test_same_grounding_id_rows_collapse_to_one_turn_even_with_empty_episode():
    # Fails if the dedup keys on episode_uuid alone: an empty episode would never collapse and the
    # re-read turn would count 1 + 3 = 4 instead of the larger value 3.
    rows = [
        _occ("o1", "", scope="", value=1, grounding_id="turn-1"),
        _occ("o2", "", scope="gym", value=3, grounding_id="turn-1"),
    ]
    s = _only_state(fold_assertions(rows, include_occurrences=True))
    assert s.value == 3
    assert s.contributed_occurrence_ids == ["o2"]


@pytest.mark.unit
def test_episode_grounded_delta_absorbs_turn_grounded_occurrence_of_the_same_admitted_turn():
    # (c) Fails if a delta claims only one key: the occurrence (grounding turn-9, episode ep-1)
    # would then survive and be double counted next to the delta's episode-grounded change.
    rows = [
        _occ("d1", "ep-1", operation="delta", value=3, grounding_id="ep-1"),
        _occ("o-dup", "ep-1", grounding_id="turn-9"),
        _occ("o-other", "ep-2", grounding_id="turn-2"),
    ]
    s = _only_state(fold_assertions(rows, include_occurrences=True))
    assert s.value == 1 and s.contributed_occurrence_ids == ["o-other"]
    assert s.absorbed_occurrence_ids == ["o-dup"]


@pytest.mark.unit
def test_turn_grounded_delta_with_no_episode_absorbs_same_turn_occurrence():
    # (d) Fails if absorption relies on episode_uuid (empty here) instead of grounding_id.
    rows = [
        _occ("d1", "", operation="delta", value=3, grounding_id="turn-9"),
        _occ("o-dup", "", grounding_id="turn-9"),
        _occ("o-other", "", grounding_id="turn-2"),
    ]
    s = _only_state(fold_assertions(rows, include_occurrences=True))
    assert s.value == 1 and s.absorbed_occurrence_ids == ["o-dup"]


@pytest.mark.unit
def test_delta_from_an_unrelated_turn_absorbs_nothing():
    # (e) Fails if absorption is over-broad (e.g. matches on empty keys or any delta).
    rows = [
        _occ("d1", "ep-7", operation="delta", value=3, grounding_id="turn-7"),
        _occ("o1", "ep-1", grounding_id="turn-1"),
        _occ("o2", "", grounding_id="turn-2"),
    ]
    s = _only_state(fold_assertions(rows, include_occurrences=True))
    assert s.value == 2 and s.absorbed_occurrence_ids == []


# --------------------------------------------------------------------------- 2. write fence

@pytest.mark.unit
def test_write_guard_raising_stops_all_later_assertion_writes_and_propagates():
    # Fails if perceive_and_persist ignores write_guard (all 3 rows recorded) or swallows the
    # error (no exception reaches the caller).
    adapter, svc = _SelfFakeAdapter(), _SelfFakeService()
    coord = TypedScalarPerceptionService(adapter, svc)
    eps = [_Ep(f"ep-{i}", f"I woke at 0{i}:30 once.") for i in (1, 2, 3)]
    rows = [dict(episode=i, subject="user", attribute=f"wake{i}", scope="", value_kind="clock_time",
                 unit="", operation="absolute", value=f"0{i + 1}:30", when="2026-07-01",
                 stated_span=f"0{i + 1}:30") for i in range(3)]
    for i, r in enumerate(rows):
        eps[i].content = f"I woke at {r['value']} once."
    calls = {"n": 0}

    def guard():
        calls["n"] += 1
        if calls["n"] == 2:
            raise CountingFenceLost("lost mid-run")

    with pytest.raises(CountingFenceLost):
        coord.perceive_and_persist(eps, _llm(rows), k=1, threshold=1.0, namespace="ns",
                                   write_guard=guard)
    assert calls["n"] == 2                 # guard consulted before each write, stopped at the raise
    assert len(adapter.recorded) == 1      # only the write before the lost fence landed


@pytest.mark.unit
def test_runner_passes_a_write_guard_that_reports_fenced_and_does_not_advance(
    fake_perception, monkeypatch,  # noqa: F811
):
    # Fails if the runner does not pass write_guard, if the guard does not re-check the claim
    # (a lost lease mid-LLM would store), or if the fence loss is counted as failure / advances.
    seen = {}

    def perceive(self, episodes, llm, **kwargs):
        guard = kwargs.get("write_guard")
        seen["guard"] = guard
        guard()                              # second renew of the run -> scripted False -> raises
        seen["after_guard"] = True
        return {"bound": 1, "advisory": 0}

    monkeypatch.setattr(fake_perception, "perceive_and_persist", perceive)
    graph = _FenceGraph(pages={"a": [[_row(1)]]})
    graph.renew_script = [True, False]       # pre-perception fence ok, per-write fence lost
    out = _run(graph, ["a"])
    assert callable(seen["guard"]) and "after_guard" not in seen
    assert out["counting_namespaces_fenced"] == 1
    assert out["counting_namespaces_failed"] == 0 and out["counting_namespaces_processed"] == 0
    assert graph.names("advance") == []
    assert len(graph.names("renew")) == 2
    assert len(graph.names("release")) == 1


# --------------------------------------------------------------------------- 3. observation lane

def _occurrence_hit():
    hit = dict(_postcard_observation_hit()[0])
    hit["operation"] = "occurrence"
    return hit


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("counting_enabled", [False, True])
async def test_occurrence_hit_is_an_observation_and_drives_history_only_when_counting_on(
    stub_graphiti_client, stub_memory_graph_adapter, counting_enabled,
):
    # Fails if the occurrence filter is removed (off: the occurrence leaks as an OBSERVATION and
    # its slot pulls the history View in) or applied unconditionally (on: it never surfaces).
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter, obs_hit=[_occurrence_hit()],
                            history_view=_postcard_history_view(), state_view=None)
    svc = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(), scalar_view_authority_enabled=True,
        scalar_history_enabled=True, counting_enabled=counting_enabled)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    ids = {r.uuid for r in res.results}
    assert ("obs-postcards-1" in ids) is counting_enabled
    assert ("history-view-postcards" in ids) is counting_enabled


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_occurrence_hit_still_drives_the_lanes_with_counting_off(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Positive control: the counting-off filter is specific to occurrence rows.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter, obs_hit=_postcard_observation_hit(),
                            history_view=_postcard_history_view(), state_view=None)
    svc = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(), scalar_view_authority_enabled=True,
        scalar_history_enabled=True, counting_enabled=False)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    ids = {r.uuid for r in res.results}
    assert "obs-postcards-1" in ids and "history-view-postcards" in ids


# --------------------------------------------------------------------------- 4. rebuild history

def _history_service(counting_enabled):
    rows = [
        _assertion(assertion_id="a1", operation="delta", value=17, episode_uuid="ep-1"),
        _assertion(assertion_id="o1", operation="occurrence", value=1, episode_uuid="ep-2",
                   valid_at="2023-09-01T00:00:00+00:00", importance="tracked"),
    ]
    sink = FakeViewSink()
    svc = ScalarStateService(FakeAssertionSource(rows), sink, counting_enabled=counting_enabled,
                             scalar_history_enabled=True)
    return svc, sink


@pytest.mark.unit
@pytest.mark.parametrize("counting_enabled,expected", [(False, ["a1"]), (True, ["a1", "o1"])])
def test_rebuild_scalar_history_lists_occurrences_only_when_counting_on(counting_enabled, expected):
    # Fails if rebuild_scalar_history stops filtering occurrences with counting off (history View
    # lists them) or filters them with counting on.
    svc, sink = _history_service(counting_enabled)
    svc.rebuild_scalar_history("ent-postcards")
    assert len(sink.scalar_histories) == 1
    ids = [e["assertion_id"] if isinstance(e, dict) else e.assertion_id
           for e in sink.scalar_histories[0]["entries"]]
    assert ids == expected
    ops = sink.scalar_histories[0]["operation_counts"]
    assert ("occurrence" in ops) is counting_enabled


# --------------------------------------------------------------------------- 5. history kill switch

@pytest.mark.unit
@pytest.mark.parametrize("view,expected", [
    ({"operation_counts": {"delta": 1, "occurrence": 2}}, True),
    ({"operation_counts": {"delta": 1}}, False),
    ({"operation_counts": {"occurrence": 0}}, False),
    ({"sh_op_counts": json.dumps({"occurrence": 2}, sort_keys=True)}, True),
    ({"sh_op_counts": json.dumps({"delta": 2})}, False),
    ({"sh_op_counts": None}, False),
    ({}, False),
])
def test_scalar_history_has_occurrences_helper(view, expected):
    # Fails if the helper stops reading either source (parsed dict or raw JSON) or misreads absence.
    assert _scalar_history_has_occurrences(view) is expected


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("counting_enabled", [False, True])
async def test_generic_candidate_history_view_with_occurrences_hidden_when_counting_off(
    stub_graphiti_client, stub_memory_graph_adapter, counting_enabled,
):
    # Fails if the generic metadata filter ignores sh_op_counts (stale occurrence history leaks
    # with counting off) or hides it with counting on.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.search_scored_results.append(("history-view-1", "history of postcards", 0.9))
    stub_memory_graph_adapter.candidate_metadata.append({
        "uuid": "history-view-1", "name": "history of postcards", "scope": "PERSISTENT",
        "type": "SEMANTIC", "content": "history content", "summary": None,
        "last_accessed": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        "edge_count": 1, "freshness": "ACTIVE", "user_flagged": False,
        "view_kind": "scalar_history", "view_current": True,
        "sh_op_counts": json.dumps({"delta": 1, "occurrence": 2}, sort_keys=True),
    })
    svc = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(), scalar_history_enabled=True,
        counting_enabled=counting_enabled)
    res = await svc.recall("postcards")
    assert ("history-view-1" in [r.uuid for r in res.results]) is counting_enabled


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("counting_enabled", [False, True])
async def test_history_lane_view_with_occurrences_hidden_when_counting_off(
    stub_graphiti_client, stub_memory_graph_adapter, counting_enabled,
):
    # Fails if the history lane skips the `_scalar_history_has_occurrences(hv)` check.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    hv = _postcard_history_view()
    hv["operation_counts"] = {"delta": 2, "occurrence": 1}
    _setup_observation_lane(stub_memory_graph_adapter, obs_hit=_postcard_observation_hit(),
                            history_view=hv, state_view=None)
    svc = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(), scalar_view_authority_enabled=True,
        scalar_history_enabled=True, counting_enabled=counting_enabled)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    assert ("history-view-postcards" in {r.uuid for r in res.results}) is counting_enabled


# --------------------------------------------------------------------------- 6. adapter kill switch

class _RecordingViews:
    def __init__(self):
        self.calls = []

    def fetch_scalar_authority_contributors(self, **kw):
        self.calls.append(("contrib", kw))
        return {}

    def list_scalar_history_entries(self, **kw):
        self.calls.append(("history", kw))
        return {}


def _adapter(counting_enabled):
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._views = _RecordingViews()
    adapter.counting_enabled = counting_enabled
    return adapter


@pytest.mark.unit
@pytest.mark.parametrize("counting_enabled", [False, True])
def test_adapter_defaults_include_occurrences_to_counting_enabled(counting_enabled):
    # Fails if either adapter method stops injecting the kill-switch default.
    adapter = _adapter(counting_enabled)
    adapter.fetch_scalar_authority_contributors(view_uuid="v")
    adapter.list_scalar_history_entries(view_uuid="v")
    assert [c[1]["include_occurrences"] for c in adapter._views.calls] == [counting_enabled] * 2


@pytest.mark.unit
def test_adapter_respects_an_explicit_include_occurrences():
    # Fails if the adapter overwrites the caller's explicit choice instead of using setdefault.
    adapter = _adapter(False)
    adapter.fetch_scalar_authority_contributors(view_uuid="v", include_occurrences=True)
    adapter.list_scalar_history_entries(view_uuid="v", include_occurrences=True)
    assert [c[1]["include_occurrences"] for c in adapter._views.calls] == [True, True]


# --------------------------------------------------------------------------- 9. P7 basis clear

@pytest.mark.unit
def test_counted_to_stated_rebuild_sends_a_none_basis_not_the_stale_counted_mark():
    # Fails if a stated state omits the key again: the refresh (`SET n += $refresh`) would then
    # leave scalar_basis='counted' on an unchanged-value View.
    occ = [_assertion(assertion_id="o1", operation="occurrence", value=1, episode_uuid="ep-1",
                      importance="tracked", attribute="yoga_count", scope="", unit=""),
           _assertion(assertion_id="o2", operation="occurrence", value=1, episode_uuid="ep-2",
                      importance="tracked", attribute="yoga_count", scope="", unit="",
                      valid_at="2023-08-12T00:00:00+00:00")]
    rows = list(occ)
    sink = FakeViewSink()
    svc = ScalarStateService(FakeAssertionSource(rows), sink, counting_enabled=True)
    svc.rebuild_scalar_state("ent-postcards")
    assert sink.scalar_states[-1]["audit"]["scalar_basis"] == BASIS_COUNTED
    rows.append(_assertion(assertion_id="a1", operation="absolute", value=2, episode_uuid="ep-3",
                           attribute="yoga_count", scope="", unit="", importance="",
                           valid_at="2023-08-13T00:00:00+00:00"))
    svc.rebuild_scalar_state("ent-postcards")
    last = sink.scalar_states[-1]
    assert last["value"] == 2
    assert "scalar_basis" in last["audit"] and last["audit"]["scalar_basis"] is None
