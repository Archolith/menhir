"""Unit tests for Phase 4's context selector: scenario construction, response parsing,
and scoring logic. All mocked -- no live LLM calls needed to verify correctness."""

import pytest

from menhir.explorer.extraction_lab_context_selection import (
    CandidateFact,
    DecoyType,
    SelectionScenario,
    build_all_scenarios,
    _parse_selector_response,
    _selector_prompt,
    run_selector,
)


class _StubChatBackend:
    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class TestBuildAllScenarios:
    def test_15_scenarios_total(self):
        scenarios = build_all_scenarios()
        assert len(scenarios) == 15

    def test_each_fixture_has_5_scenarios(self):
        scenarios = build_all_scenarios()
        by_fixture: dict[str, int] = {}
        for s in scenarios:
            by_fixture[s.fixture_qid] = by_fixture.get(s.fixture_qid, 0) + 1
        assert by_fixture == {
            "lme-830ce83f": 5,
            "lme-852ce960": 5,
            "lme-2698e78f": 5,
        }

    def test_scenario_5_always_expects_none(self):
        """The 'no relevant fact' scenario must always have correct_candidate_id=None."""
        scenarios = build_all_scenarios()
        s5s = [s for s in scenarios if s.scenario_id.endswith("_5_no_relevant_fact")]
        assert len(s5s) == 3
        for s in s5s:
            assert s.correct_candidate_id is None

    def test_scenario_5_candidate_pool_never_contains_the_correct_answer(self):
        """Scenario 5 is a negative control -- if a 'correct' candidate ended up in its
        pool by accident, the test would be meaningless."""
        scenarios = build_all_scenarios()
        s5s = [s for s in scenarios if s.scenario_id.endswith("_5_no_relevant_fact")]
        for s in s5s:
            assert all(c.decoy_type != DecoyType.NONE for c in s.candidates)

    def test_scenario_3_contains_wrong_subject_decoys(self):
        """The false-grounding trap scenario must actually contain wrong-subject decoys,
        not just the correct fact alone."""
        scenarios = build_all_scenarios()
        s3s = [s for s in scenarios if s.scenario_id.endswith("_3_same_facet_multi_subject")]
        for s in s3s:
            decoy_types = {c.decoy_type for c in s.candidates}
            assert DecoyType.WRONG_SUBJECT in decoy_types

    def test_scenario_4_contains_a_stale_decoy(self):
        scenarios = build_all_scenarios()
        s4s = [s for s in scenarios if s.scenario_id.endswith("_4_stale_vs_current")]
        for s in s4s:
            decoy_types = {c.decoy_type for c in s.candidates}
            assert DecoyType.STALE in decoy_types

    def test_non_none_correct_ids_exist_in_their_own_pool(self):
        """Sanity: every scenario expecting a real selection must have that id actually
        present among its own candidates (a basic self-consistency check on the fixture
        data, not just the selector)."""
        scenarios = build_all_scenarios()
        for s in scenarios:
            if s.correct_candidate_id is not None:
                ids = {c.id for c in s.candidates}
                assert s.correct_candidate_id in ids, s.scenario_id


class TestSelectorPromptConstruction:
    def test_prompt_includes_all_candidates(self):
        candidates = (
            CandidateFact("c1", "Rachel", "Housing", "Current residence", "Rachel moved to Chicago."),
            CandidateFact("c2", "Daniel", "Housing", "Current residence", "Daniel moved apartments.", DecoyType.WRONG_SUBJECT),
        )
        prompt = _selector_prompt("Rachel moved back to the suburbs.", candidates)
        assert "c1" in prompt
        assert "c2" in prompt
        assert "Rachel moved to Chicago." in prompt
        assert "Daniel moved apartments." in prompt


class TestParseSelectorResponse:
    def test_parses_valid_selection(self):
        selected_id, reasoning = _parse_selector_response('{"selected_id": "c1", "reasoning": "matches"}')
        assert selected_id == "c1"
        assert reasoning == "matches"

    def test_parses_null_selection(self):
        selected_id, reasoning = _parse_selector_response('{"selected_id": null, "reasoning": "nothing fits"}')
        assert selected_id is None

    def test_strips_markdown_fences(self):
        selected_id, _ = _parse_selector_response('```json\n{"selected_id": "c1", "reasoning": "x"}\n```')
        assert selected_id == "c1"

    def test_strips_think_tags(self):
        selected_id, _ = _parse_selector_response(
            '<think>pondering...</think>{"selected_id": "c1", "reasoning": "x"}'
        )
        assert selected_id == "c1"

    def test_rejects_non_string_selected_id(self):
        with pytest.raises(ValueError):
            _parse_selector_response('{"selected_id": 5, "reasoning": "x"}')

    def test_missing_json_object_raises(self):
        with pytest.raises(ValueError):
            _parse_selector_response("not json at all")


class TestRunSelector:
    async def test_correct_selection_scores_correct_true(self):
        scenario = SelectionScenario(
            "test_1", "test-fixture", "Rachel moved back to the suburbs.", "Rachel", "Housing",
            (CandidateFact("c1", "Rachel", "Housing", "Current residence", "Rachel moved to Chicago."),),
            correct_candidate_id="c1",
        )
        backend = _StubChatBackend('{"selected_id": "c1", "reasoning": "matches"}')
        result = await run_selector(backend, scenario)
        assert result["correct"] is True
        assert result["is_decoy_selection"] is False
        assert result["selected_id"] == "c1"

    async def test_decoy_selection_scores_correct_false_and_flags_decoy(self):
        scenario = SelectionScenario(
            "test_2", "test-fixture", "Rachel moved back to the suburbs.", "Rachel", "Housing",
            (
                CandidateFact("c1", "Rachel", "Housing", "Current residence", "Rachel moved to Chicago."),
                CandidateFact("c2", "Daniel", "Housing", "Current residence", "Daniel moved apartments.", DecoyType.WRONG_SUBJECT),
            ),
            correct_candidate_id="c1",
        )
        backend = _StubChatBackend('{"selected_id": "c2", "reasoning": "topically similar"}')
        result = await run_selector(backend, scenario)
        assert result["correct"] is False
        assert result["is_decoy_selection"] is True
        assert result["decoy_type"] == "wrong_subject"

    async def test_correctly_selecting_nothing_scores_correct_true(self):
        scenario = SelectionScenario(
            "test_5", "test-fixture", "Rachel moved back to the suburbs.", "Rachel", "Housing",
            (CandidateFact("c1", "Daniel", "Housing", "Current residence", "Daniel moved apartments.", DecoyType.WRONG_SUBJECT),),
            correct_candidate_id=None,
        )
        backend = _StubChatBackend('{"selected_id": null, "reasoning": "no match for Rachel"}')
        result = await run_selector(backend, scenario)
        assert result["correct"] is True
        assert result["selected_id"] is None

    async def test_incorrectly_selecting_a_decoy_when_none_was_correct(self):
        """The precise false-grounding failure mode: correct answer is 'nothing', but
        the selector picks a decoy anyway."""
        scenario = SelectionScenario(
            "test_5b", "test-fixture", "Rachel moved back to the suburbs.", "Rachel", "Housing",
            (CandidateFact("c1", "Daniel", "Housing", "Current residence", "Daniel moved apartments.", DecoyType.WRONG_SUBJECT),),
            correct_candidate_id=None,
        )
        backend = _StubChatBackend('{"selected_id": "c1", "reasoning": "seemed relevant"}')
        result = await run_selector(backend, scenario)
        assert result["correct"] is False
        assert result["is_decoy_selection"] is True

    async def test_hallucinated_id_not_in_pool_treated_as_incorrect(self):
        """A selector inventing an id that was never offered must not silently match or
        crash -- it's a real failure mode worth capturing distinctly."""
        scenario = SelectionScenario(
            "test_6", "test-fixture", "Rachel moved back to the suburbs.", "Rachel", "Housing",
            (CandidateFact("c1", "Rachel", "Housing", "Current residence", "Rachel moved to Chicago."),),
            correct_candidate_id="c1",
        )
        backend = _StubChatBackend('{"selected_id": "c99", "reasoning": "x"}')
        result = await run_selector(backend, scenario)
        assert result["correct"] is False
        assert result["selected_id"] == "INVALID:c99"
        assert result["is_decoy_selection"] is False  # not a known decoy, just invalid

    async def test_no_backend_raises(self):
        scenario = SelectionScenario(
            "test_7", "test-fixture", "msg", "Rachel", "Housing",
            (CandidateFact("c1", "Rachel", "Housing", "Current residence", "x"),),
            correct_candidate_id="c1",
        )

        class _NoCreateBackend:
            pass

        with pytest.raises(RuntimeError):
            await run_selector(_NoCreateBackend(), scenario)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
