"""Verb-facet fact append (MENHIR_FRONTIER_VERB_FACETS): flag wiring, pipeline behavior, query builder."""

from __future__ import annotations

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.namespace import tenant_scope_cypher
from menhir.domain.recall import RetrievalScoreKind
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.verb_facets import verb_facets
from menhir.infrastructure.memory_queries import (
    MemoryQueryRepository,
    fact_keyword_lucene_query,
)
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import _setup_search_and_metadata

CLOTHING_Q = "How many items of clothing do I need to pick up or return from a store?"
FURNITURE_Q = "How many pieces of furniture did I buy, assemble, sell, or fix in the past few months?"
NOUN_Q = "How many days did I spend on camping trips in the United States this year?"
ON = RetrievalTuningConfig(enable_verb_facets=True)


def _row(uuid, fact, score=5.0, expired_at=None, valid_at="2023-05-01T00:00:00Z",
         source="entity-1", target="entity-2"):
    # Endpoints default to the two admitted PERSISTENT entities from _setup_search_and_metadata.
    return {
        "uuid": uuid, "fact": fact, "score": score, "valid_at": valid_at, "invalid_at": None,
        "created_at": "2023-05-02T00:00:00Z", "expired_at": expired_at, "group_id": "proj",
        "source_uuid": source, "target_uuid": target,
    }


def _svc(client, adapter):
    return RecallService(graphiti_client=client, graph_adapter=adapter,
                         scoring_service=ScoringService())


class _Search:
    """Records calls; returns rows keyed by the first form of the facet."""

    def __init__(self, by_first_form=None, default=None):
        self.calls = []
        self.by_first_form = by_first_form or {}
        self.default = default or []

    def __call__(self, phrases, namespace, limit=50):
        self.calls.append((tuple(phrases), namespace))
        return list(self.by_first_form.get(phrases[0], self.default))


async def _recall(client, adapter, query, search, tuning=ON, endpoints=(), **kwargs):
    _setup_search_and_metadata(client, adapter)
    adapter.candidate_metadata.extend(dict(e) for e in endpoints)
    for meta in adapter.candidate_metadata:
        meta["namespace"] = "proj"
    adapter.search_facts_by_keywords = search
    return await _svc(client, adapter).recall(
        query, namespace="proj", limit=kwargs.pop("limit", 2), tuning=tuning, **kwargs)


# --- flag wiring -------------------------------------------------------------


@pytest.mark.unit
def test_verb_facets_flag_registered_and_reaches_tuning(monkeypatch):
    feature = FEATURES["frontier_verb_facets"]
    assert feature.env_var == "MENHIR_FRONTIER_VERB_FACETS"
    assert feature.default is False
    assert feature.requires == ()
    assert RetrievalTuningConfig().enable_verb_facets is False
    monkeypatch.setenv("MENHIR_FRONTIER_VERB_FACETS", "1")
    settings = MemorySettings.from_env()
    assert settings.frontier_verb_facets is True
    assert settings.retrieval_tuning().enable_verb_facets is True


# --- pipeline ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_never_calls_search(stub_graphiti_client, stub_memory_graph_adapter):
    search = _Search(default=[_row("e1", "picked up the blazer")])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, search,
                        tuning=RetrievalTuningConfig())
    assert search.calls == []
    assert [r.uuid for r in res.results] == ["entity-1", "entity-2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_noun_question_never_calls_search(stub_graphiti_client, stub_memory_graph_adapter):
    search = _Search(default=[_row("e1", "camped in Utah")])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, NOUN_Q, search)
    assert search.calls == []
    assert len(res.results) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_appends_one_fact_per_facet_after_ranked_results(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    search = _Search({
        "pick up": [_row("e1", "User picked up the blazer from the tailor")],
        "return": [_row("e2", "User needs to return the boots to Zara", score=3.0)],
    })
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, search)
    assert search.calls == [
        (("pick up", "picked up", "picking up", "picks up"), "proj"),
        (("return", "returned", "returning", "returns"), "proj"),
    ]
    assert [r.uuid for r in res.results] == ["entity-1", "entity-2", "e1", "e2"]
    for item in res.results[2:]:
        assert item.retrieval_score_kind is RetrievalScoreKind.VERB_FACET
        assert item.name == item.content
        assert item.memory_type == "SEMANTIC"
    assert res.results[2].content == "User picked up the blazer from the tailor"
    # Raw fulltext score is kept off the ranked scale so context dedup never prefers it.
    assert res.results[2].retrieval_score == 5.0
    assert res.results[2].final_score == 0.0
    assert res.results[2].temporal_facts[0].valid_at == "2023-05-01T00:00:00Z"
    assert res.results[2].stale_anchor_info == {"stale_anchor": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedup_against_results_texts_and_across_facets(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.temporal_fact_rows = [{
        "node_uuid": "entity-1", "fact": "attached fact text", "valid_at": None,
        "invalid_at": None, "created_at": "2023-01-01T00:00:00Z", "expired_at": None,
    }]
    shared = _row("shared", "User returned the boots")
    search = _Search({
        "pick up": [
            _row("entity-2", "uuid already in results"),
            _row("x1", "test content"),            # equals an existing result's content
            _row("x2", "attached fact text"),      # equals a temporal_facts fact
            shared,
        ],
        "return": [shared, _row("r2", "User returned the jacket")],
    })
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, search)
    assert [r.uuid for r in res.results[2:]] == ["shared", "r2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cap_is_three_per_recall(stub_graphiti_client, stub_memory_graph_adapter):
    rows = {
        form: [_row(f"u-{form}", f"fact for {form}")]
        for form in ("buy", "bought", "assemble", "sell", "fix")
    }
    search = _Search({"bought": rows["bought"], "assemble": rows["assemble"],
                      "sell": rows["sell"], "fix": rows["fix"]})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, FURNITURE_Q, search)
    assert len(search.calls) == 3
    assert len(res.results) == 2 + 3


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("include_invalidated", "expected"), [(False, []), (True, ["old"])])
async def test_include_invalidated_controls_expired_rows(
    stub_graphiti_client, stub_memory_graph_adapter, include_invalidated, expected,
):
    search = _Search({"pick up": [_row("old", "User picked up the old coat",
                                       expired_at="2023-06-01T00:00:00Z")]})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, search,
                        include_invalidated=include_invalidated)
    assert [r.uuid for r in res.results[2:]] == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_appended_uuids_never_reach_access_updates(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    touched = []
    monkeypatch.setattr(stub_memory_graph_adapter, "touch_retrieved_nodes",
                        lambda uuids: touched.append(list(uuids)) or len(uuids))
    search = _Search({"pick up": [_row("e1", "User picked up the blazer")]})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, search)
    assert "e1" in [r.uuid for r in res.results]
    assert touched and all("e1" not in batch for batch in touched)


def _endpoint(uuid, **fields):
    return {"uuid": uuid, "name": uuid, "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": uuid, "freshness": "ACTIVE", **fields}


# A fact is admitted only when both endpoint memories pass ranked recall's admission.
@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "kwargs", "kept"),
    [
        (_endpoint("n"), {}, True),
        (_endpoint("n", scope="SESSION", session_id="s1"), {}, False),  # include_session default off
        (_endpoint("n", scope="SESSION", session_id="s1"),
         {"include_session": True, "session_id": "s1"}, True),
        (_endpoint("n", scope="SESSION", session_id="s2"),
         {"include_session": True, "session_id": "s1"}, False),         # another session
        (_endpoint("n", freshness="GONE"), {}, False),
        (_endpoint("n", scope="CANDIDATE"), {}, False),
        (_endpoint("n", structure_role="directory"), {}, False),
    ],
)
@pytest.mark.parametrize("side", ["source", "target"])
async def test_fact_needs_both_endpoints_admitted(
    stub_graphiti_client, stub_memory_graph_adapter, endpoint, kwargs, kept, side,
):
    row = _row("f1", "User picked up the blazer", **{side: "n"})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q,
                        _Search({"pick up": [row]}), endpoints=[endpoint], **kwargs)
    assert ("f1" in [r.uuid for r in res.results]) is kept


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_from_other_namespace_endpoint_dropped(stub_graphiti_client, stub_memory_graph_adapter):
    search = _Search({"pick up": [_row("f1", "User picked up the blazer", target="n")]})
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    for meta in stub_memory_graph_adapter.candidate_metadata:
        meta["namespace"] = "proj"
    stub_memory_graph_adapter.candidate_metadata.append(_endpoint("n", namespace="elsewhere"))
    stub_memory_graph_adapter.search_facts_by_keywords = search
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        CLOTHING_Q, namespace="proj", limit=2, tuning=ON)
    assert "f1" not in [r.uuid for r in res.results]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("row_kw", [{"target": "no-metadata"}, {"source": None}])
async def test_fact_with_unknown_endpoint_fails_closed(
    stub_graphiti_client, stub_memory_graph_adapter, row_kw,
):
    search = _Search({"pick up": [_row("f1", "User picked up the blazer", **row_kw),
                                  _row("f2", "User picked up the scarf")]})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, search)
    assert [r.uuid for r in res.results[2:]] == ["f2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_searches_are_bounded_when_nothing_matches(stub_graphiti_client, stub_memory_graph_adapter):
    q = ("How many items did I buy, sell, fix, paint, wash, fold, sew or iron in the past few months?")
    assert len(verb_facets(q)) > 5
    search = _Search()
    await _recall(stub_graphiti_client, stub_memory_graph_adapter, q, search)
    assert len(search.calls) == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_programming_errors_are_not_swallowed(stub_graphiti_client, stub_memory_graph_adapter):
    def broken(phrases, namespace, limit=50):
        raise TypeError("bad call")

    with pytest.raises(TypeError):
        await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, broken)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_failure_keeps_ranked_results(stub_graphiti_client, stub_memory_graph_adapter):
    def boom(phrases, namespace, limit=50):
        raise RuntimeError("neo4j down")

    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, CLOTHING_Q, boom)
    assert [r.uuid for r in res.results] == ["entity-1", "entity-2"]


# --- Lucene builder / query --------------------------------------------------


@pytest.mark.unit
def test_lucene_quotes_and_escapes_phrases():
    assert fact_keyword_lucene_query(["pick up", "say \"hi\"", "a\\b"], None) == (
        '("pick up" OR "say \\"hi\\"" OR "a\\\\b")'
    )


@pytest.mark.unit
def test_lucene_group_clause():
    assert fact_keyword_lucene_query(["x"], ["g1", 'g"2']) == (
        '(group_id:"g1" OR group_id:"g\\"2") AND ("x")'
    )


@pytest.mark.unit
@pytest.mark.parametrize("groups", [None, [""], ["a", ""], []])
def test_lucene_group_clause_omitted(groups):
    assert fact_keyword_lucene_query(["x"], groups) == '("x")'


@pytest.mark.unit
@pytest.mark.parametrize("phrases", [[], [""], ["", "  "]])
def test_lucene_no_phrases_returns_none(phrases):
    assert fact_keyword_lucene_query(phrases, ["g"]) is None


class _FakeNeo4j:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return []


@pytest.mark.unit
def test_search_facts_by_keywords_query_and_params():
    neo = _FakeNeo4j()
    repo = MemoryQueryRepository(neo)
    assert repo.search_facts_by_keywords([""], "g") == []
    assert neo.calls == []
    # Default silo: tenancy via tenant_scope_cypher with both spellings; no Lucene group clause
    # because '' cannot be matched by Lucene.
    repo.search_facts_by_keywords(["pick up"], "default", limit=7)
    query, params = neo.calls[-1]
    assert "queryRelationships('edge_name_and_fact', $q, {limit: $limit})" in query
    assert tenant_scope_cypher("r") in query
    assert "startNode(r).uuid AS source_uuid, endNode(r).uuid AS target_uuid" in query
    assert params == {"q": '("pick up")', "limit": 7, "tenant_namespaces": ["default", ""]}
    # A named silo also pre-filters in Lucene so other tenants cannot fill the fulltext limit.
    repo.search_facts_by_keywords(["pick up"], "lme-1")
    _, params = neo.calls[-1]
    assert params["q"] == '(group_id:"lme-1") AND ("pick up")'
    assert params["tenant_namespaces"] == ["lme-1"]
    # Unscoped (None): no Lucene group clause and the tenancy predicate is a no-op.
    repo.search_facts_by_keywords(["pick up"], None)
    _, params = neo.calls[-1]
    assert params["q"] == '("pick up")' and params["tenant_namespaces"] is None
