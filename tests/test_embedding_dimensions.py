from __future__ import annotations

import pytest
from menhir.config import MemorySettings
from menhir.infrastructure.embedding_dimensions import (
    embedding_dimension_health,
    evaluate_embedding_compatibility,
    infer_embedding_dimension_for_model,
)


class _RecordingNeo4j:
    """Fake Neo4j repo: records queries, returns canned rows by substring match."""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.queries: list[str] = []
        self._responses = responses

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(query)
        for needle, rows in self._responses.items():
            if needle in query:
                return rows
        return []


@pytest.mark.unit
class TestEmbeddingDimensionHealth:
    """Regression guards for the structural-aware, NULL-aware health check."""

    def _responses(self, *, null_entity=0, null_edge=0):
        return {
            # dim-distribution queries (IS NOT NULL)
            "n.name_embedding IS NOT NULL": [{"dim": 1536, "count": 5}],
            "r.fact_embedding IS NOT NULL": [{"dim": 1536, "count": 3}],
            # NULL-count queries
            "MATCH (n:Entity) WHERE n.name_embedding IS NULL": [{"c": null_entity}],
            "MATCH (n:Community) WHERE n.name_embedding IS NULL": [{"c": 0}],
            "r.fact_embedding IS NULL": [{"c": null_edge}],
        }

    def test_null_count_queries_exclude_structural_nodes(self):
        """The NULL-count queries MUST filter out structural nodes/edges.

        Structural graph nodes (code files/symbols) and ANCHORED_TO anchors are
        intentionally never embedded; counting them as 'missing' caused a false
        alarm and a backfill that polluted the vector space. Lock the exclusion.
        """
        neo4j = _RecordingNeo4j(self._responses())
        embedding_dimension_health(neo4j, expected_dim=1536)
        null_queries = [q for q in neo4j.queries if "embedding IS NULL" in q]
        assert null_queries, "expected NULL-count queries to run"
        for q in null_queries:
            assert "structure_role IS NULL" in q, f"structural exclusion missing from: {q}"

    def test_ok_true_when_no_missing_or_wrong(self):
        neo4j = _RecordingNeo4j(self._responses())
        health = embedding_dimension_health(neo4j, expected_dim=1536)
        assert health["ok"] is True
        assert health["null_entity_count"] == 0
        assert health["null_edge_count"] == 0

    def test_ok_false_when_semantic_embeddings_missing(self):
        neo4j = _RecordingNeo4j(self._responses(null_entity=42))
        health = embedding_dimension_health(neo4j, expected_dim=1536)
        assert health["ok"] is False
        assert health["null_entity_count"] == 42

    def test_mixed_dimensions_flagged_even_without_expected(self):
        """A graph holding two distinct dimensions is broken regardless of model."""
        neo4j = _RecordingNeo4j({
            "n.name_embedding IS NOT NULL": [{"dim": 768, "count": 20000}, {"dim": 1536, "count": 51}],
            "r.fact_embedding IS NOT NULL": [{"dim": 768, "count": 3}],
            "MATCH (n:Entity) WHERE n.name_embedding IS NULL": [{"c": 0}],
            "MATCH (n:Community) WHERE n.name_embedding IS NULL": [{"c": 0}],
            "r.fact_embedding IS NULL": [{"c": 0}],
        })
        # expected_dim omitted (unknown model) — mixed must still be caught
        health = embedding_dimension_health(neo4j)
        assert health["mixed"] is True
        assert health["ok"] is False


class _StaticNeo4j:
    """Fake repo returning fixed per-dimension distributions for compatibility tests."""

    def __init__(self, entity_dims: list[dict]) -> None:
        self._entity = entity_dims

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        if "n.name_embedding IS NOT NULL" in query:
            return self._entity
        return []  # no communities, edges, or null rows

    def close(self) -> None:  # evaluate_* never calls close, but be safe
        pass


@pytest.mark.unit
class TestEvaluateEmbeddingCompatibility:
    """Block only when a mismatch is certain; never false-block an unknown model."""

    def _settings(self, embed_model: str) -> MemorySettings:
        return MemorySettings(
            graphiti_provider="openai",
            graphiti_embed_provider="openai",
            openai_embed_model=embed_model,
        )

    def test_matching_dimension_is_ok(self):
        neo4j = _StaticNeo4j([{"dim": 1536, "count": 6916}])
        c = evaluate_embedding_compatibility(neo4j, self._settings("text-embedding-3-small"))
        assert c.ok is True and c.blocking is False

    def test_known_model_wrong_stored_dim_blocks(self):
        # graph is all 768 but the configured model is 1536 -> certain mismatch
        neo4j = _StaticNeo4j([{"dim": 768, "count": 20000}])
        c = evaluate_embedding_compatibility(neo4j, self._settings("text-embedding-3-small"))
        assert c.blocking is True
        assert "1536" in c.banner()

    def test_mixed_dimensions_block(self):
        neo4j = _StaticNeo4j([{"dim": 768, "count": 20000}, {"dim": 1536, "count": 51}])
        c = evaluate_embedding_compatibility(neo4j, self._settings("text-embedding-3-small"))
        assert c.blocking is True
        assert c.mixed is True

    def test_unknown_model_uniform_graph_does_not_block(self):
        # dimension can't be inferred; a uniform graph is unverifiable, not broken
        neo4j = _StaticNeo4j([{"dim": 1024, "count": 5000}])
        c = evaluate_embedding_compatibility(neo4j, self._settings("some-custom-embedder-v9"))
        assert c.blocking is False
        assert c.expected_dim is None
        assert "unknown" in c.reason.lower()

    def test_unknown_model_mixed_graph_still_blocks(self):
        # even for an unknown model, mixed dimensions are unambiguously broken
        neo4j = _StaticNeo4j([{"dim": 1024, "count": 5000}, {"dim": 768, "count": 10}])
        c = evaluate_embedding_compatibility(neo4j, self._settings("some-custom-embedder-v9"))
        assert c.blocking is True

@pytest.mark.unit
class TestInferEmbeddingDimensionForModel:
    """Tests for infer_embedding_dimension_for_model utility."""

    @pytest.mark.parametrize("model_name, expected_dim", [
        ("text-embedding-3-small", 1536),
        ("text-embedding-3-large", 3072),
        ("text-embedding-ada-002", 1536),
        ("nomic-embed-text-v1.5", 768),
        ("bge-base-en-v1.5", 768),
        ("bge-large-en-v1.5", 1024),
    ])
    def test_known_models_exact_match(self, model_name, expected_dim):
        assert infer_embedding_dimension_for_model(model_name) == expected_dim

    def test_case_insensitivity(self):
        assert infer_embedding_dimension_for_model("TEXT-EMBEDDING-3-SMALL") == 1536
        assert infer_embedding_dimension_for_model("Nomic-Embed-Text-v1.5") == 768

    def test_whitespace_handling(self):
        assert infer_embedding_dimension_for_model("  text-embedding-3-small  ") == 1536
        assert infer_embedding_dimension_for_model("\ntext-embedding-3-large\t") == 3072

    def test_substring_match(self):
        assert infer_embedding_dimension_for_model("openai/text-embedding-3-small") == 1536
        assert infer_embedding_dimension_for_model("ollama/nomic-embed-text-v1.5:latest") == 768

    def test_empty_input(self):
        assert infer_embedding_dimension_for_model("") is None
        assert infer_embedding_dimension_for_model("   ") is None

    def test_none_input(self):
        # The type hint says str, but the code handles None via (model_name or "")
        assert infer_embedding_dimension_for_model(None) is None  # type: ignore

    def test_unknown_model(self):
        assert infer_embedding_dimension_for_model("unknown-model") is None
        assert infer_embedding_dimension_for_model("my-fancy-embedding-v2") is None
