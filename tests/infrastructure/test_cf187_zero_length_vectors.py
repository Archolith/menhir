"""CF-187: a stored zero-length vector must be a first-class corruption signal.

A stored empty list is a real, non-NULL property whose size() is 0, so it is
invisible to the IS NULL sweeps, to the ``d > 0`` mixed check, and to the wrong-dim
counts when ``expected_dim`` is None (the default posture). Zero-length vectors are
wrong against EVERY expected dimension, known or not, so they must reach
``blocking`` even when the embed model's dimension cannot be inferred.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure import embedding_dimensions as ed


class _FakeNeo4j:
    """Fake repo: returns canned rows keyed by a distinguishing query substring."""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self._responses = responses
        self.uri = "bolt://fake:7687"
        self.database = "neo4j"

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        for needle, rows in self._responses.items():
            if needle in query:
                return rows
        return []


class _Provider:
    def __init__(self, embed_model: str = "") -> None:
        self.kind = type("K", (), {"value": "local"})()
        self.base_url = "http://localhost:8080/v1"
        self.api_key = ""
        self.embed_model = embed_model

    def supports_graphiti_openai_contract(self) -> bool:
        return True


def _patch(monkeypatch, *, expected_dim=None, embed_model: str = "") -> None:
    monkeypatch.setattr(
        ed.ProviderConfig,
        "for_graphiti_embedder",
        classmethod(lambda cls, settings: _Provider(embed_model=embed_model)),
    )
    monkeypatch.setattr(
        ed,
        "expected_graphiti_embedding_dimension",
        lambda settings: expected_dim,
    )


def _health(monkeypatch, responses, *, expected_dim=None):
    _patch(monkeypatch, expected_dim=expected_dim)
    neo4j = _FakeNeo4j(responses)
    return ed.embedding_dimension_health(neo4j, expected_dim=expected_dim)


def _compat(monkeypatch, responses, *, expected_dim=None):
    _patch(monkeypatch, expected_dim=expected_dim)
    neo4j = _FakeNeo4j(responses)
    return ed.evaluate_embedding_compatibility(neo4j, object(), use_cache=False)


@pytest.fixture(autouse=True)
def _clear_cache():
    ed.reset_embedding_dimension_cache()
    yield
    ed.reset_embedding_dimension_cache()


pytestmark = pytest.mark.unit


def _empty_responses() -> dict[str, list[dict]]:
    return {
        "MATCH (n:Entity) WHERE n.name_embedding IS NULL": [{"c": 0}],
        "MATCH (n:Community) WHERE n.name_embedding IS NULL": [{"c": 0}],
        "r.fact_embedding IS NULL": [{"c": 0}],
    }


def _responses(entity=None, community=None, edge=None):
    responses = _empty_responses()
    if entity is not None:
        responses["MATCH (n:Entity)"] = entity
    if community is not None:
        responses["MATCH (n:Community)"] = community
    if edge is not None:
        responses["r.fact_embedding IS NOT NULL"] = edge
    return responses


def test_finding_default_posture_entity_zeros(monkeypatch):
    """expected_dim=None (default posture), entity rows {0:5, 1536:100}, no nulls:
    health not ok, zero_entity_count == 5, and evaluate blocking with reason naming the zeros."""
    responses = _responses(entity=[{"dim": 0, "count": 5}, {"dim": 1536, "count": 100}])
    health = _health(monkeypatch, responses)
    assert health["ok"] is False
    assert health["zero_entity_count"] == 5
    compat = _compat(monkeypatch, responses)
    assert compat.blocking is True
    assert "0" in compat.reason or "5" in compat.reason


def test_finding_default_posture_community_zeros(monkeypatch):
    responses = _responses(community=[{"dim": 0, "count": 5}, {"dim": 1536, "count": 100}])
    health = _health(monkeypatch, responses)
    assert health["ok"] is False
    assert health["zero_community_count"] == 5
    compat = _compat(monkeypatch, responses)
    assert compat.blocking is True


def test_finding_default_posture_edge_zeros(monkeypatch):
    responses = _responses(edge=[{"dim": 0, "count": 5}, {"dim": 1536, "count": 100}])
    health = _health(monkeypatch, responses)
    assert health["ok"] is False
    assert health["zero_edge_count"] == 5
    compat = _compat(monkeypatch, responses)
    assert compat.blocking is True


def test_positive_control_no_zero_vectors_unaffected(monkeypatch):
    """A graph with no zero-length vectors is unaffected in every field."""
    responses = _responses(entity=[{"dim": 1536, "count": 100}])
    health = _health(monkeypatch, responses, expected_dim=1536)
    assert health["ok"] is True
    assert health["zero_entity_count"] == 0
    assert health["zero_community_count"] == 0
    assert health["zero_edge_count"] == 0
    assert health["wrong_entity_count"] == 0
    assert health["mixed"] is False


def test_positive_control_zeros_are_not_a_second_dimension(monkeypatch):
    """{0:5, 1536:100} is NOT mixed (a zero must not read as a real dimension), yet
    still blocking -- pins the split between the two signals."""
    responses = _responses(entity=[{"dim": 0, "count": 5}, {"dim": 1536, "count": 100}])
    health = _health(monkeypatch, responses)
    assert health["mixed"] is False
    compat = _compat(monkeypatch, responses)
    assert compat.mixed is False
    assert compat.blocking is True


def test_positive_control_two_real_dimensions_still_mixed(monkeypatch):
    """{768:3, 1536:100} is still mixed True."""
    responses = _responses(entity=[{"dim": 768, "count": 3}, {"dim": 1536, "count": 100}])
    health = _health(monkeypatch, responses)
    assert health["mixed"] is True
    assert health["ok"] is False
