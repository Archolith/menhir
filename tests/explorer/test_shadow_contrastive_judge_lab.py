"""Unit tests for shadow_contrastive_judge_lab.py's pure logic. No real LLM calls -- the
runner (run_shadow_contrastive_judge_lab.py) makes those."""

import pytest

from menhir.explorer.shadow_contrastive_judge_lab import (
    ContrastiveCandidate,
    ContrastiveResult,
    ContrastiveTestCase,
    build_contrastive_test_cases,
    contrastive_user_prompt,
    parse_contrastive_response,
    score_contrastive_result,
)


class TestBuildContrastiveTestCases:
    def test_three_test_cases(self):
        cases = build_contrastive_test_cases()
        assert len(cases) == 3
        assert {c.fixture_qid for c in cases} == {"lme-830ce83f", "lme-852ce960", "lme-2698e78f"}

    def test_seven_candidates_per_case(self):
        """6 distinct real candidates per family (1 true positive + 5 decoy types) + 1
        cross-family control."""
        for case in build_contrastive_test_cases():
            assert len(case.candidates) == 7

    def test_exactly_one_true_positive_per_case(self):
        for case in build_contrastive_test_cases():
            positives = [c for c in case.candidates if c.is_positive]
            assert len(positives) == 1

    def test_boundaries_decoy_present_in_2698_case(self):
        cases = {c.fixture_qid: c for c in build_contrastive_test_cases()}
        case = cases["lme-2698e78f"]
        boundary_candidates = [c for c in case.candidates if c.category == "wrong_state_family"]
        assert len(boundary_candidates) == 1
        assert "boundaries" in boundary_candidates[0].text.lower()

    def test_every_decoy_category_present_per_case(self):
        expected = {
            "positive", "wrong_state_family", "wrong_scope", "stale",
            "wrong_subject", "missing_metadata", "cross_family_control",
        }
        for case in build_contrastive_test_cases():
            categories = {c.category for c in case.candidates}
            assert categories == expected, f"{case.fixture_qid} missing: {expected - categories}"

    def test_cross_family_control_is_never_positive(self):
        for case in build_contrastive_test_cases():
            control = next(c for c in case.candidates if c.category == "cross_family_control")
            assert not control.is_positive

    def test_candidate_ids_unique_within_case(self):
        for case in build_contrastive_test_cases():
            ids = [c.candidate_id for c in case.candidates]
            assert len(ids) == len(set(ids))

    def test_true_positive_ids_helper(self):
        case = build_contrastive_test_cases()[0]
        true_ids = case.true_positive_ids()
        assert len(true_ids) == 1
        assert all(
            c.is_positive for c in case.candidates if c.candidate_id in true_ids
        )


class TestContrastiveUserPrompt:
    def test_includes_message_and_all_candidates(self):
        prompt = contrastive_user_prompt("the message", [("id1", "text1"), ("id2", "text2")])
        assert "the message" in prompt
        assert "id1" in prompt and "text1" in prompt
        assert "id2" in prompt and "text2" in prompt


def _case():
    return ContrastiveTestCase(
        fixture_qid="q1", message="msg",
        candidates=(
            ContrastiveCandidate("c1", "true fact", "positive", True),
            ContrastiveCandidate("c2", "boundary decoy", "wrong_state_family", False),
            ContrastiveCandidate("c3", "unrelated", "cross_family_control", False),
        ),
    )


class TestParseContrastiveResponse:
    def test_none_response_is_a_failure(self):
        result = parse_contrastive_response(_case(), None)
        assert result.selected_ids is None
        assert result.raw_response is None

    def test_valid_json_parses(self):
        raw = '{"message": {}, "candidates": [], "selected_candidate_ids": ["c1"]}'
        result = parse_contrastive_response(_case(), raw)
        assert result.selected_ids == frozenset({"c1"})
        assert result.parsed_audit is not None

    def test_empty_selection_parses_as_empty_frozenset_not_none(self):
        raw = '{"message": {}, "candidates": [], "selected_candidate_ids": []}'
        result = parse_contrastive_response(_case(), raw)
        assert result.selected_ids == frozenset()

    def test_malformed_json_is_unparseable(self):
        result = parse_contrastive_response(_case(), "not json at all")
        assert result.selected_ids is None
        assert result.raw_response == "not json at all"

    def test_missing_selected_ids_field_is_unparseable(self):
        raw = '{"message": {}, "candidates": []}'
        result = parse_contrastive_response(_case(), raw)
        assert result.selected_ids is None
        # still keeps the parsed audit for inspection
        assert result.parsed_audit is not None

    def test_markdown_fenced_json_parses(self):
        raw = '```json\n{"selected_candidate_ids": ["c1"]}\n```'
        result = parse_contrastive_response(_case(), raw)
        assert result.selected_ids == frozenset({"c1"})


class TestScoreContrastiveResult:
    def test_unparseable_result_scores_none_not_zero(self):
        result = ContrastiveResult(_case(), "garbage", None, None)
        assert score_contrastive_result(result) is None

    def test_correct_selection_scores_clean(self):
        result = ContrastiveResult(_case(), "raw", frozenset({"c1"}), {})
        score = score_contrastive_result(result)
        assert score.true_positive_preserved is True
        assert score.any_negative_selected is False
        assert score.boundaries_selected is False

    def test_boundaries_wrongly_selected_is_flagged(self):
        result = ContrastiveResult(_case(), "raw", frozenset({"c1", "c2"}), {})
        score = score_contrastive_result(result)
        assert score.true_positive_preserved is True
        assert score.any_negative_selected is True
        assert score.boundaries_selected is True

    def test_true_positive_missed_is_flagged(self):
        result = ContrastiveResult(_case(), "raw", frozenset({"c3"}), {})
        score = score_contrastive_result(result)
        assert score.true_positive_preserved is False
        assert score.any_negative_selected is True

    def test_empty_selection_means_positive_not_preserved(self):
        result = ContrastiveResult(_case(), "raw", frozenset(), {})
        score = score_contrastive_result(result)
        assert score.true_positive_preserved is False
        assert score.any_negative_selected is False

    def test_scoring_never_reads_parsed_audit_fields(self):
        """Explicit design constraint: scoring must depend ONLY on selected_candidate_ids,
        never on the extracted same_slot/slot strings -- even if parsed_audit contains
        misleading or wrong same_slot values, the score must match selected_ids alone."""
        misleading_audit = {
            "candidates": [
                {"id": "c1", "same_slot": False},  # audit says NOT same slot...
                {"id": "c2", "same_slot": True},   # ...and says boundary decoy IS same slot
            ],
        }
        # but selected_candidate_ids (the only thing that should be scored) says c1 was chosen
        result = ContrastiveResult(_case(), "raw", frozenset({"c1"}), misleading_audit)
        score = score_contrastive_result(result)
        assert score.true_positive_preserved is True
        assert score.any_negative_selected is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
