"""Both recall surfaces must say the relevance tier is a rank score, and say it the same way."""

from types import SimpleNamespace

from menhir.api.routes_support import RecallMemory
from menhir.mcp.formatters import _compact_scored_item

BASIS = "rank_score_threshold_not_similarity"


def test_mcp_and_rest_recall_report_the_same_basis():
    scored = SimpleNamespace(
        uuid="u1",
        name="n",
        scope="SESSION",
        final_score=0.5,
        summary="s",
        breakdown=SimpleNamespace(semantic_similarity=1.0),
    )
    rest = RecallMemory(
        uuid="u1", name="n", content=None, scope="SESSION", memory_type="episodic", final_score=0.5
    )

    assert _compact_scored_item(scored)["relevance_basis"] == BASIS
    assert rest.relevance_basis == BASIS


def test_tier_thresholds_are_unchanged():
    def tier(score):
        scored = SimpleNamespace(breakdown=SimpleNamespace(semantic_similarity=score))
        return _compact_scored_item(scored)["relevance"]

    assert [tier(s) for s in (0.7, 0.69, 0.4, 0.39)] == ["high", "medium", "medium", "low"]
