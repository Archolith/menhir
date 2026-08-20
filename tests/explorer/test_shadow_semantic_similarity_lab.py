"""Unit tests for shadow_semantic_similarity_lab.py's pure logic. No embedding calls --
deterministic fixture assembly and math only. The runner (run_shadow_semantic_similarity_lab.py)
is what actually calls the real embedder."""

import math

import pytest

from menhir.explorer.shadow_semantic_similarity_lab import (
    ScoredRow,
    SimilarityLabRow,
    best_f1_point,
    build_similarity_lab_rows,
    category_summary,
    cosine_similarity,
    precision_recall_curve,
    score_rows,
    unique_embed_texts,
)


class TestBuildSimilarityLabRows:
    def test_produces_rows(self):
        rows = build_similarity_lab_rows()
        assert len(rows) > 0

    def test_every_row_has_a_category(self):
        rows = build_similarity_lab_rows()
        valid_categories = {
            "positive", "wrong_state_family", "wrong_scope", "wrong_subject",
            "stale", "missing_metadata", "cross_family_control",
        }
        for row in rows:
            assert row.category in valid_categories

    def test_at_least_one_positive_per_family(self):
        rows = build_similarity_lab_rows()
        families = {row.fixture_qid for row in rows if row.category != "cross_family_control"}
        for qid in families:
            positives = [r for r in rows if r.fixture_qid == qid and r.is_positive]
            assert len(positives) >= 1, f"{qid} has no positive row"

    def test_cross_family_rows_are_never_positive(self):
        rows = build_similarity_lab_rows()
        cross = [r for r in rows if r.category == "cross_family_control"]
        assert len(cross) > 0
        assert all(not r.is_positive for r in cross)

    def test_cross_family_never_pairs_a_family_with_itself(self):
        """A cross_family_control row's message must come from a DIFFERENT family than its
        candidate -- otherwise it's not testing what it claims to test."""
        rows = build_similarity_lab_rows()
        by_family_message = {}
        for row in build_similarity_lab_rows():
            if row.category != "cross_family_control":
                by_family_message.setdefault(row.fixture_qid, row.embed_message_text)
        cross = [r for r in rows if r.category == "cross_family_control"]
        # every cross-family candidate text must NOT belong to the row's own family's
        # within-family candidate set
        own_family_candidate_texts = {}
        for row in build_similarity_lab_rows():
            if row.category != "cross_family_control":
                own_family_candidate_texts.setdefault(row.fixture_qid, set()).add(row.embed_candidate_text)
        for row in cross:
            assert row.embed_candidate_text not in own_family_candidate_texts.get(row.fixture_qid, set())

    def test_stale_category_present(self):
        """The eligibility fixtures include DecoyType.STALE candidates -- confirms the
        temporally-invalid negative category the report asked for is actually represented."""
        rows = build_similarity_lab_rows()
        assert any(r.category == "stale" for r in rows)

    def test_wrong_state_family_present(self):
        rows = build_similarity_lab_rows()
        assert any(r.category == "wrong_state_family" for r in rows)


class TestUniqueEmbedTexts:
    def test_dedupes(self):
        rows = build_similarity_lab_rows()
        texts = unique_embed_texts(rows)
        assert len(texts) == len(set(texts))

    def test_covers_every_row_text(self):
        rows = build_similarity_lab_rows()
        texts = set(unique_embed_texts(rows))
        for row in rows:
            assert row.embed_message_text in texts
            assert row.embed_candidate_text in texts


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_score_negative_one(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vectors_score_zero_not_crash(self):
        assert cosine_similarity([], []) == 0.0

    def test_mismatched_lengths_score_zero_not_crash(self):
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector_scores_zero_not_nan(self):
        result = cosine_similarity([0.0, 0.0], [1.0, 1.0])
        assert result == 0.0
        assert not math.isnan(result)


class TestScoreRows:
    def test_missing_embedding_scores_zero_not_raise(self):
        row = SimilarityLabRow(
            scenario_id="s1", fixture_qid="q1", category="positive", is_positive=True,
            candidate_id="c1", embed_message_text="msg", embed_candidate_text="cand",
        )
        scored = score_rows([row], embeddings={})
        assert scored[0].similarity == 0.0

    def test_present_embeddings_score_correctly(self):
        row = SimilarityLabRow(
            scenario_id="s1", fixture_qid="q1", category="positive", is_positive=True,
            candidate_id="c1", embed_message_text="msg", embed_candidate_text="cand",
        )
        embeddings = {"msg": [1.0, 0.0], "cand": [1.0, 0.0]}
        scored = score_rows([row], embeddings)
        assert scored[0].similarity == pytest.approx(1.0)


class TestPrecisionRecallCurve:
    def _rows(self):
        pos = SimilarityLabRow("s1", "q1", "positive", True, "c1", "m", "pos_cand")
        neg = SimilarityLabRow("s1", "q1", "wrong_state_family", False, "c2", "m", "neg_cand")
        return [
            ScoredRow(row=pos, similarity=0.9),
            ScoredRow(row=neg, similarity=0.2),
        ]

    def test_perfect_separation_hits_f1_one_at_some_threshold(self):
        curve = precision_recall_curve(self._rows())
        best = best_f1_point(curve)
        assert best.f1 == pytest.approx(1.0)
        assert best.true_positives == 1
        assert best.false_positives == 0

    def test_threshold_zero_selects_everything(self):
        curve = precision_recall_curve(self._rows())
        t0 = curve[0]
        assert t0.threshold == 0.0
        assert t0.true_positives == 1
        assert t0.false_positives == 1

    def test_threshold_above_all_scores_selects_nothing(self):
        curve = precision_recall_curve(self._rows())
        t_max = curve[-1]
        assert t_max.threshold == 1.0
        assert t_max.true_positives == 0
        assert t_max.false_positives == 0
        # precision defaults to 1.0 when nothing is selected (vacuously "no false alarms"),
        # recall is 0 -- distinguishing "selected nothing" from "selected everything correctly"
        assert t_max.recall == 0.0

    def test_no_positives_in_set_gives_zero_recall_never_divides_by_zero(self):
        neg = SimilarityLabRow("s1", "q1", "wrong_state_family", False, "c2", "m", "neg_cand")
        curve = precision_recall_curve([ScoredRow(row=neg, similarity=0.5)])
        for point in curve:
            assert point.recall == 0.0


class TestCategorySummary:
    def test_groups_by_category(self):
        pos = SimilarityLabRow("s1", "q1", "positive", True, "c1", "m", "pos_cand")
        neg = SimilarityLabRow("s1", "q1", "stale", False, "c2", "m", "neg_cand")
        scored = [ScoredRow(row=pos, similarity=0.9), ScoredRow(row=neg, similarity=0.1)]
        summary = category_summary(scored)
        assert summary["positive"]["count"] == 1
        assert summary["positive"]["mean"] == pytest.approx(0.9)
        assert summary["stale"]["mean"] == pytest.approx(0.1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
