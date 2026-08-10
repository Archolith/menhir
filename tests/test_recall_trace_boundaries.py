"""Architecture checks for the one-way recall/trace domain boundary."""

from __future__ import annotations

import ast
from dataclasses import asdict
from pathlib import Path
from typing import get_type_hints

from menhir.domain.recall import RecallResult
from menhir.domain.retrieval_trace_models import (
    CandidateTrace,
    RelevanceBreakdown,
    RetrievalTrace,
)
from menhir.domain.retrieval_tuning import CandidateSource


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_retrieval_trace_models_do_not_import_recall() -> None:
    domain_root = Path(__file__).parents[1] / "src" / "menhir" / "domain"
    model_imports = _absolute_imports(domain_root / "retrieval_trace_models.py")
    recall_imports = _absolute_imports(domain_root / "recall.py")

    assert "menhir.domain.recall" not in model_imports
    assert "menhir.domain.retrieval_trace_models" in recall_imports
    assert not (domain_root / "retrieval_trace.py").exists()


def test_recall_trace_annotation_and_serialized_shape_are_preserved() -> None:
    breakdown = RelevanceBreakdown(
        semantic_similarity=0.8,
        adjacency_bonus=0.1,
        recency_bonus=0.2,
        prominence_bonus=0.3,
        conflict_bonus=0.0,
        type_boost=1.0,
        preset="knowledge",
        alpha=0.2,
        beta=0.1,
        gamma=0.1,
        delta=0.0,
    )
    trace = RetrievalTrace(
        query="query",
        preset="knowledge",
        total_ms=3,
        phases={"total": 3},
        candidates=[
            CandidateTrace(
                uuid="candidate-1",
                source=CandidateSource.VECTOR,
                similarity=0.8,
                survived_floor=True,
                score_parts=breakdown,
                final_score=1.4,
                rank=0,
            )
        ],
    )

    assert get_type_hints(RecallResult)["trace"] == RetrievalTrace | None
    assert asdict(trace) == {
        "query": "query",
        "preset": "knowledge",
        "total_ms": 3,
        "phases": {"total": 3},
        "candidates": [
            {
                "uuid": "candidate-1",
                "source": CandidateSource.VECTOR,
                "similarity": 0.8,
                "survived_floor": True,
                "score_parts": asdict(breakdown),
                "final_score": 1.4,
                "rank": 0,
                "bm25_rank": None,
                "cosine_rank": None,
                "contributing_sources": frozenset(),
                "content_rank": None,
                "content_cosine": None,
            }
        ],
        "assertion_shadow": None,
        "facet_shadow": None,
        "facet_active": None,
        "view_reachability": None,
        "rank_shadow_warning": None,
    }
