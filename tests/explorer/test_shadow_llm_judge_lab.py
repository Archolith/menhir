"""Unit tests for shadow_llm_judge_lab.py's pure logic. No real LLM calls -- the runner
(run_shadow_llm_judge_lab.py) makes those."""

import pytest

from menhir.explorer.shadow_llm_judge_lab import (
    JudgedRow,
    category_breakdown,
    confusion_counts,
    judge_user_prompt,
    parse_judge_response,
    precision_recall_f1,
)
from menhir.explorer.shadow_semantic_similarity_lab import SimilarityLabRow


def _row(category="positive", is_positive=True):
    return SimilarityLabRow(
        scenario_id="s1", fixture_qid="q1", category=category, is_positive=is_positive,
        candidate_id="c1", embed_message_text="msg", embed_candidate_text="cand",
    )


class TestJudgeUserPrompt:
    def test_includes_both_texts(self):
        prompt = judge_user_prompt("the message", "the candidate fact")
        assert "the message" in prompt
        assert "the candidate fact" in prompt

    def test_asks_for_single_word_answer(self):
        prompt = judge_user_prompt("m", "c")
        assert "MATCH" in prompt


class TestParseJudgeResponse:
    def test_match(self):
        assert parse_judge_response("MATCH") is True

    def test_no_match(self):
        assert parse_judge_response("NO_MATCH") is False

    def test_no_match_with_space(self):
        assert parse_judge_response("NO MATCH") is False

    def test_case_insensitive(self):
        assert parse_judge_response("match") is True
        assert parse_judge_response("no_match") is False

    def test_no_match_not_misread_as_match(self):
        """NO_MATCH contains the substring MATCH -- must not be misparsed as True."""
        assert parse_judge_response("Answer: NO_MATCH.") is False

    def test_surrounding_prose_still_parses(self):
        assert parse_judge_response("I believe this is a MATCH because...") is True

    def test_none_input_is_unparseable(self):
        assert parse_judge_response(None) is None

    def test_empty_string_is_unparseable(self):
        assert parse_judge_response("") is None

    def test_garbage_is_unparseable(self):
        assert parse_judge_response("I'm not sure how to answer this") is None


class TestConfusionCounts:
    def test_true_positive(self):
        rows = [JudgedRow(row=_row(is_positive=True), judgment=True)]
        counts = confusion_counts(rows)
        assert counts == {"tp": 1, "fp": 0, "tn": 0, "fn": 0, "unparseable": 0}

    def test_false_negative(self):
        rows = [JudgedRow(row=_row(is_positive=True), judgment=False)]
        counts = confusion_counts(rows)
        assert counts["fn"] == 1

    def test_false_positive(self):
        rows = [JudgedRow(row=_row(is_positive=False), judgment=True)]
        counts = confusion_counts(rows)
        assert counts["fp"] == 1

    def test_true_negative(self):
        rows = [JudgedRow(row=_row(is_positive=False), judgment=False)]
        counts = confusion_counts(rows)
        assert counts["tn"] == 1

    def test_unparseable_not_folded_into_either_class(self):
        rows = [JudgedRow(row=_row(is_positive=True), judgment=None)]
        counts = confusion_counts(rows)
        assert counts["unparseable"] == 1
        assert counts["tp"] == 0
        assert counts["fn"] == 0


class TestPrecisionRecallF1:
    def test_perfect(self):
        metrics = precision_recall_f1({"tp": 5, "fp": 0, "fn": 0})
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 1.0
        assert metrics["f1"] == 1.0

    def test_no_positives_predicted_or_present(self):
        metrics = precision_recall_f1({"tp": 0, "fp": 0, "fn": 0})
        assert metrics["precision"] == 1.0  # vacuous
        assert metrics["recall"] == 0.0
        assert metrics["f1"] == 0.0

    def test_never_divides_by_zero(self):
        # tp + fp == 0 but fn > 0 -- judge rejected everything, including real positives
        metrics = precision_recall_f1({"tp": 0, "fp": 0, "fn": 3})
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 0.0


class TestCategoryBreakdown:
    def test_correct_positive_and_incorrect_negative(self):
        rows = [
            JudgedRow(row=_row(category="positive", is_positive=True), judgment=True),
            JudgedRow(row=_row(category="wrong_state_family", is_positive=False), judgment=True),
        ]
        breakdown = category_breakdown(rows)
        assert breakdown["positive"] == {"correct": 1, "incorrect": 0, "unparseable": 0}
        assert breakdown["wrong_state_family"] == {"correct": 0, "incorrect": 1, "unparseable": 0}

    def test_unparseable_tracked_separately(self):
        rows = [JudgedRow(row=_row(), judgment=None)]
        breakdown = category_breakdown(rows)
        assert breakdown["positive"]["unparseable"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
