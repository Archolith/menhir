"""CF-198: ``ok`` means "embeddings are healthy", not merely "no dimension mismatch".

The serve guard gates on ``blocking`` (which must keep excluding missing vectors),
but ``evaluate_embedding_compatibility.ok`` had been redefined as ``not blocking``,
silently dropping the null/missing-vector signal that ``embedding_dimension_health``
already folds into its own ``ok``. Missing vectors are invisible to vector recall;
they must flip ``ok`` (and surface via a ``missing_vectors`` count and a warning)
without ever hard-blocking startup.
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
    def __init__(self, embed_model: str = "text-embedding-3-small") -> None:
        self.kind = type("K", (), {"value": "openai"})()
        self.base_url = "http://localhost:8080/v1"
        self.api_key = ""
        self.embed_model = embed_model

    def supports_graphiti_openai_contract(self) -> bool:
        return True


def _patch(monkeypatch, *, expected_dim, embed_model: str = "text-embedding-3-small") -> None:
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


def _responses(
    *,
    entity=None,
    community=None,
    edge=None,
    null_entity=0,
    null_community=0,
    null_edge=0,
):
    responses = _empty_responses()
    if entity is not None:
        responses["MATCH (n:Entity)"] = entity
    if community is not None:
        responses["MATCH (n:Community)"] = community
    if edge is not None:
        responses["r.fact_embedding IS NOT NULL"] = edge
    if null_entity:
        responses["MATCH (n:Entity) WHERE n.name_embedding IS NULL"] = [{"c": null_entity}]
    if null_community:
        responses["MATCH (n:Community) WHERE n.name_embedding IS NULL"] = [{"c": null_community}]
    if null_edge:
        responses["r.fact_embedding IS NULL"] = [{"c": null_edge}]
    return responses


def _eval(monkeypatch, responses, *, expected_dim=1536):
    _patch(monkeypatch, expected_dim=expected_dim)
    neo4j = _FakeNeo4j(responses)
    return ed.evaluate_embedding_compatibility(neo4j, object(), use_cache=False)


def _health(monkeypatch, responses, *, expected_dim=1536):
    _patch(monkeypatch, expected_dim=expected_dim)
    neo4j = _FakeNeo4j(responses)
    return ed.embedding_dimension_health(neo4j, expected_dim=expected_dim)


def test_finding_missing_vectors_flip_ok_but_not_blocking(monkeypatch):
    """null_entity_count > 0, zero wrong, not mixed: ok is False AND blocking is False.
    Both halves matter -- asserting only `ok is False` would pass a wrong fix that
    made the missing vectors blocking."""
    responses = _responses(entity=[{"dim": 1536, "count": 100}], null_entity=5)
    compat = _eval(monkeypatch, responses)
    assert compat.ok is False
    assert compat.blocking is False


def test_missing_vectors_equals_sum_of_null_counts(monkeypatch):
    responses = _responses(
        entity=[{"dim": 1536, "count": 100}],
        null_entity=5,
        null_community=3,
        null_edge=7,
    )
    compat = _eval(monkeypatch, responses)
    assert compat.missing_vectors == 5 + 3 + 7


def test_reason_is_not_ok_and_names_the_count(monkeypatch):
    responses = _responses(entity=[{"dim": 1536, "count": 100}], null_entity=5)
    compat = _eval(monkeypatch, responses)
    assert compat.reason != "ok"
    assert "5" in compat.reason


def _graph_shapes():
    return [
        # clean: everything healthy
        (
            "clean",
            _responses(entity=[{"dim": 1536, "count": 100}]),
        ),
        # nulls only
        (
            "nulls_only",
            _responses(entity=[{"dim": 1536, "count": 100}], null_entity=5, null_edge=3),
        ),
        # wrong dimension only (known expected)
        (
            "wrong_only",
            _responses(entity=[{"dim": 768, "count": 100}]),
        ),
        # mixed dimensions
        (
            "mixed",
            _responses(entity=[{"dim": 768, "count": 50}, {"dim": 1536, "count": 50}]),
        ),
    ]


@pytest.mark.parametrize("name,responses", _graph_shapes())
def test_compat_ok_agrees_with_health_ok(monkeypatch, name, responses):
    """The invariant: compat.ok must equal health["ok"] -- the whole point of CF-198.
    Parameterized over several graph shapes."""
    health = _health(monkeypatch, responses)
    compat = _eval(monkeypatch, responses)
    assert compat.ok == health["ok"], f"shape '{name}' drifted"


def test_positive_control_clean_graph(monkeypatch):
    """A clean graph still gives ok True, blocking False, reason == 'ok', missing_vectors == 0."""
    responses = _responses(entity=[{"dim": 1536, "count": 100}])
    compat = _eval(monkeypatch, responses)
    assert compat.ok is True
    assert compat.blocking is False
    assert compat.reason == "ok"
    assert compat.missing_vectors == 0


def test_positive_control_mixed_graph_still_blocks(monkeypatch):
    """The change must not weaken the guard: mixed is still blocking."""
    responses = _responses(entity=[{"dim": 768, "count": 50}, {"dim": 1536, "count": 50}])
    compat = _eval(monkeypatch, responses)
    assert compat.blocking is True


def test_positive_control_wrong_dim_known_expected_still_blocks(monkeypatch):
    """A wrong-dimension graph with a known expected_dim still blocks."""
    responses = _responses(entity=[{"dim": 768, "count": 100}])
    compat = _eval(monkeypatch, responses)
    assert compat.blocking is True
