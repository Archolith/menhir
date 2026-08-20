"""Unit tests for Phase 4b's eligibility-filter selector. Deterministic paths (structured_only,
oracle, is_eligible) tested directly; LLM-involving paths tested with a stub backend --
no live LLM calls needed."""

import pytest

from menhir.explorer.extraction_lab_eligibility_selection import (
    DecoyType,
    EligibilityScenario,
    StructuredFact,
    is_eligible,
    eligible_candidates,
    select_llm_only,
    select_structured_only,
    select_structured_then_llm,
    select_oracle,
)
from menhir.explorer.extraction_lab_eligibility_fixtures import build_all_eligibility_scenarios


class _StubChatBackend:
    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _scenario(candidates, correct, target_scope=None, ref="2023-06-01T00:00:00+00:00"):
    return EligibilityScenario(
        "test_scenario", "test-fixture", "current message text", ref,
        "Rachel", "Housing", "Current residence", target_scope, candidates, correct,
    )


class TestBuildAllEligibilityScenarios:
    def test_21_scenarios_total(self):
        assert len(build_all_eligibility_scenarios()) == 21

    def test_each_fixture_has_7_scenarios(self):
        scenarios = build_all_eligibility_scenarios()
        by_fixture: dict[str, int] = {}
        for s in scenarios:
            by_fixture[s.fixture_qid] = by_fixture.get(s.fixture_qid, 0) + 1
        assert by_fixture == {"lme-830ce83f": 7, "lme-852ce960": 7, "lme-2698e78f": 7}

    def test_negative_scenarios_have_none_correct(self):
        scenarios = build_all_eligibility_scenarios()
        negative_suffixes = ("_5_near_neighbor_decoys_only", "_6_no_subject_match", "_7_missing_metadata")
        for s in scenarios:
            if s.scenario_id.endswith(negative_suffixes):
                assert s.correct_candidate_id is None, s.scenario_id

    def test_positive_scenarios_correct_id_present_in_pool(self):
        scenarios = build_all_eligibility_scenarios()
        for s in scenarios:
            if s.correct_candidate_id is not None:
                ids = {c.id for c in s.candidates}
                assert s.correct_candidate_id in ids, s.scenario_id


class TestIsEligible:
    def test_subject_mismatch_ineligible(self):
        s = _scenario((), None)
        c = StructuredFact("c1", "Daniel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is False

    def test_facet_missing_ineligible(self):
        s = _scenario((), None)
        c = StructuredFact("c1", "Rachel", None, "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is False

    def test_state_family_mismatch_ineligible(self):
        s = _scenario((), None)
        c = StructuredFact("c1", "Rachel", "Housing", "Housing preferences", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is False

    def test_matching_all_dimensions_eligible(self):
        s = _scenario((), None)
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is True

    def test_missing_valid_at_ineligible(self):
        """No timestamp at all -- cannot confirm currency, must fail safe."""
        s = _scenario((), None)
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at=None)
        assert is_eligible(s, c) is False

    def test_valid_at_after_reference_time_ineligible(self):
        """A fact that only became true AFTER the current message cannot be prior context."""
        s = _scenario((), None, ref="2023-01-01T00:00:00+00:00")
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-06-01T00:00:00+00:00")
        assert is_eligible(s, c) is False

    def test_expired_before_reference_time_ineligible(self):
        s = _scenario((), None, ref="2023-06-01T00:00:00+00:00")
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x",
                            valid_at="2023-01-01T00:00:00+00:00", expired_at="2023-03-01T00:00:00+00:00")
        assert is_eligible(s, c) is False

    def test_scope_required_but_missing_ineligible(self):
        """When the scenario needs scope discrimination, a candidate lacking a scope
        tag cannot confirm it matches -- abstention-by-default on missing metadata."""
        s = _scenario((), None, target_scope="Chicago apartment")
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is False

    def test_scope_matches_eligible(self):
        s = _scenario((), None, target_scope="Chicago apartment")
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", "Chicago apartment", "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is True

    def test_scope_not_required_missing_scope_still_eligible(self):
        """When target_scope is None (not needed for this scenario), a candidate with
        no scope tag is fine -- only required when the scenario explicitly needs it."""
        s = _scenario((), None, target_scope=None)
        c = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        assert is_eligible(s, c) is True


class TestSelectStructuredOnly:
    def test_no_eligible_candidates_abstains(self):
        s = _scenario(
            (StructuredFact("c1", "Daniel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00"),),
            None,
        )
        result = select_structured_only(s)
        assert result["selected_id"] is None
        assert result["correct"] is True
        assert result["llm_calls"] == 0

    def test_single_eligible_candidate_selected(self):
        c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        s = _scenario((c1,), "c1")
        result = select_structured_only(s)
        assert result["selected_id"] == "c1"
        assert result["correct"] is True

    def test_multiple_eligible_prefers_most_recent(self):
        older = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "old", valid_at="2023-01-01T00:00:00+00:00")
        newer = StructuredFact("c2", "Rachel", "Housing", "Current residence", None, "new", valid_at="2023-05-01T00:00:00+00:00")
        s = _scenario((older, newer), "c2")
        result = select_structured_only(s)
        assert result["selected_id"] == "c2"

    def test_all_21_real_scenarios_correct(self):
        """Regression guard for the fixture data itself -- structured_only must get
        every authored scenario right (deterministic, no LLM variance to blame)."""
        for s in build_all_eligibility_scenarios():
            result = select_structured_only(s)
            assert result["correct"] is True, f"{s.scenario_id}: expected {s.correct_candidate_id}, got {result['selected_id']}"


class TestSelectOracle:
    def test_all_21_real_scenarios_correct(self):
        for s in build_all_eligibility_scenarios():
            result = select_oracle(s)
            assert result["correct"] is True


class TestSelectLlmOnly:
    async def test_correct_selection(self):
        c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "Rachel moved to Chicago.", valid_at="2023-05-01T00:00:00+00:00")
        s = _scenario((c1,), "c1")
        backend = _StubChatBackend('{"selected_id": "c1", "reasoning": "matches"}')
        result = await select_llm_only(backend, s)
        assert result["correct"] is True
        assert result["selector"] == "llm_only"
        assert result["llm_calls"] == 1

    async def test_hallucinated_id_treated_as_incorrect(self):
        c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        s = _scenario((c1,), "c1")
        backend = _StubChatBackend('{"selected_id": "zzz", "reasoning": "x"}')
        result = await select_llm_only(backend, s)
        assert result["correct"] is False
        assert result["selected_id"] == "INVALID:zzz"


class TestSelectStructuredThenLlm:
    async def test_zero_eligible_abstains_without_llm_call(self):
        s = _scenario(
            (StructuredFact("c1", "Daniel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00"),),
            None,
        )
        backend = _StubChatBackend('{"selected_id": "SHOULD_NOT_BE_CALLED", "reasoning": "x"}')
        result = await select_structured_then_llm(backend, s)
        assert result["selected_id"] is None
        assert result["llm_calls"] == 0
        assert len(backend.calls) == 0

    async def test_single_eligible_selected_without_llm_call(self):
        c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "x", valid_at="2023-05-01T00:00:00+00:00")
        s = _scenario((c1,), "c1")
        backend = _StubChatBackend('{"selected_id": "SHOULD_NOT_BE_CALLED", "reasoning": "x"}')
        result = await select_structured_then_llm(backend, s)
        assert result["selected_id"] == "c1"
        assert result["llm_calls"] == 0
        assert len(backend.calls) == 0

    async def test_multiple_eligible_falls_back_to_llm_among_survivors_only(self):
        """When 2+ candidates survive the filter, the LLM must be consulted, and the
        prompt it receives must only contain the SURVIVING candidates, never the ones
        the filter already excluded."""
        c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None, "Rachel signed a lease.", valid_at="2023-05-01T00:00:00+00:00")
        c2 = StructuredFact("c2", "Rachel", "Housing", "Current residence", None, "Rachel is staying with a friend.", valid_at="2023-05-01T00:00:00+00:00")
        excluded = StructuredFact("c3", "Daniel", "Housing", "Current residence", None, "Daniel moved.", valid_at="2023-05-01T00:00:00+00:00")
        s = _scenario((c1, c2, excluded), "c1")
        backend = _StubChatBackend('{"selected_id": "c1", "reasoning": "picked"}')
        result = await select_structured_then_llm(backend, s)
        assert result["llm_calls"] == 1
        assert len(backend.calls) == 1
        prompt = backend.calls[0]["user_prompt"]
        assert "c1" in prompt and "c2" in prompt
        assert "c3" not in prompt  # excluded candidate must never reach the LLM


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
