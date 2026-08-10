"""M7 Phase 4B — M0 retrieval quality baseline.

Runs the fixed M0 query set against a populated graph and measures whether
graph-augmented recall matches or exceeds raw vector search quality.

This is an online test — it requires live Neo4j, Graphiti, and LLM connections.
Run with: pytest --run-online -m online tests/test_m0_retrieval_baseline.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from menhir.domain.recall import QueryPreset

# needs_llm: drives real graphiti search lanes (bm25 + cosine) and has no skip-guard, so with no
# embedder configured it fails with "all requested Graphiti search lanes failed" rather than
# skipping. CI supplies Neo4j but no LLM/embedder.
pytestmark = [pytest.mark.online, pytest.mark.needs_llm]

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "milestone0_query_set.json"
ABSOLUTE_GATE_THRESHOLD = 7  # at least 7/10 queries must hit expected keywords
QUERY_COUNT = 10


def _load_queries() -> list[dict]:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        queries = json.load(fh)
    assert len(queries) == QUERY_COUNT, f"Expected {QUERY_COUNT} queries, got {len(queries)}"
    return queries


def _keywords_hit(results_text: str, expected_keywords: list[str]) -> bool:
    """Check if at least one expected keyword appears in the combined results text."""
    text_lower = results_text.lower()
    return any(kw.lower() in text_lower for kw in expected_keywords)


@pytest.fixture
def m0_queries() -> list[dict]:
    return _load_queries()


async def _get_live_services():
    """Build live services from the canonical runtime bootstrap path.

    Imports lazily to avoid touching Neo4j/Graphiti unless running online.
    """
    from menhir.core.runtime import start_runtime

    ctx = await start_runtime()
    return ctx.built


class TestM0RetrievalBaseline:
    """M0 retrieval quality gate — absolute and comparative."""

    async def test_graph_augmented_absolute_gate(self, m0_queries):
        """At least 7/10 queries return results containing expected keywords."""
        built = await _get_live_services()
        recall_svc = built.recall_service

        hits = 0
        for q in m0_queries:
            preset_name = q.get("intent", "knowledge")
            try:
                preset = QueryPreset(preset_name)
            except ValueError:
                preset = QueryPreset.KNOWLEDGE

            result = await recall_svc.recall(q["query"], preset=preset, limit=10)
            combined = " ".join(
                str(r.content or r.name or "") for r in result.results
            )
            if _keywords_hit(combined, q["expected_keywords"]):
                hits += 1

        assert hits >= ABSOLUTE_GATE_THRESHOLD, (
            f"Graph-augmented recall hit {hits}/{QUERY_COUNT} queries "
            f"(need {ABSOLUTE_GATE_THRESHOLD})"
        )

    async def test_graph_augmented_vs_vector_only(self, m0_queries):
        """Graph-augmented results must match or exceed vector-only keyword hit rate."""
        built = await _get_live_services()
        recall_svc = built.recall_service
        graphiti = built.graphiti_client

        vector_hits = 0
        graph_hits = 0

        for q in m0_queries:
            preset_name = q.get("intent", "knowledge")
            try:
                preset = QueryPreset(preset_name)
            except ValueError:
                preset = QueryPreset.KNOWLEDGE

            keywords = q["expected_keywords"]

            # Vector-only: raw Graphiti search
            raw_results = await graphiti.search_scored(q["query"], num_results=10)
            raw_text = " ".join(str(name) + " " + str(uuid) for uuid, name, _ in raw_results)
            if _keywords_hit(raw_text, keywords):
                vector_hits += 1

            # Graph-augmented: full recall pipeline
            result = await recall_svc.recall(q["query"], preset=preset, limit=10)
            combined = " ".join(
                str(r.content or r.name or "") for r in result.results
            )
            if _keywords_hit(combined, keywords):
                graph_hits += 1

        assert graph_hits >= vector_hits, (
            f"Graph-augmented ({graph_hits}) scored lower than vector-only ({vector_hits}). "
            "Graph scoring should not degrade retrieval quality."
        )
