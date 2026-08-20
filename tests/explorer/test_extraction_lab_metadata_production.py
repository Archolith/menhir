"""Unit tests for Phase 5's metadata-production module. Deterministic paths (field
matching, keyword rules, oracle) tested directly; LLM-involving predictors tested with
stub backends -- no live LLM or graph calls needed."""

import pytest

from menhir.explorer.extraction_lab_metadata_production import (
    GROUND_TRUTH,
    RoutingGroundTruth,
    field_matches,
    predict_oracle,
    predict_llm_ontology,
    predict_two_stage,
    predict_hybrid_rules_then_llm,
    predict_graph_lookup_only,
    _rule_based_facet,
    _rule_based_subject,
    FACET_ONTOLOGY,
)


class _StubChatBackend:
    """Returns responses from a queue, one per call -- lets a test script a multi-call
    sequence (e.g. two_stage's subject call then facet call)."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _StubClients:
    def __init__(self, driver):
        self.driver = driver


class _StubDriver:
    def __init__(self, records):
        self._records = records

    async def execute_query(self, cypher_query_, **kwargs):
        class _Result:
            def __init__(self, records):
                self.records = records
        return _Result(self._records)


class TestGroundTruth:
    def test_three_fixtures(self):
        assert len(GROUND_TRUTH) == 3

    def test_all_fields_populated(self):
        for gt in GROUND_TRUTH:
            assert gt.subject and gt.facet and gt.state_family and gt.event_type
            assert gt.current_message
            assert gt.source_namespace == gt.fixture_qid


class TestFieldMatches:
    def test_exact_match(self):
        assert field_matches("Rachel", "Rachel") is True

    def test_case_insensitive(self):
        assert field_matches("rachel", "Rachel") is True

    def test_whitespace_insensitive(self):
        assert field_matches("  Rachel  ", "Rachel") is True

    def test_none_does_not_match(self):
        assert field_matches(None, "Rachel") is False

    def test_mismatch(self):
        assert field_matches("Daniel", "Rachel") is False


class TestRuleBasedFacet:
    def test_housing_keywords(self):
        assert _rule_based_facet("She moved into a new apartment.") == "Housing"

    def test_mortgage_keywords(self):
        assert _rule_based_facet("I got pre-approved for a loan from Wells Fargo.") == "Mortgage"

    def test_therapy_keywords(self):
        assert _rule_based_facet("I see Dr. Smith every week for my session.") == "Therapy schedule"

    def test_no_match_returns_none(self):
        assert _rule_based_facet("The weather is nice today.") is None

    def test_housing_keyword_wins_over_later_rules_when_both_present(self):
        """Regression note (found during Phase 5 live testing, not a bug -- documents
        real, intentional first-match behavior): a message can contain BOTH a Housing
        cue and a Mortgage cue: the rule list order decides, and Housing is checked
        first. This is exactly the 852ce960 case where the deterministic rule mis-
        classifies a mortgage-fact message as Housing because it also mentions moving
        into a new home. Locking in the current (imperfect) behavior as a documented
        fact about this approach, not silently fixing it -- Phase 5's report relies on
        this exact behavior being real, not patched away."""
        message = (
            "I'm planning to move into my new home soon - remember when I got "
            "pre-approved for $400,000 from Wells Fargo?"
        )
        assert _rule_based_facet(message) == "Housing"


class TestRuleBasedSubject:
    def test_known_name_hint(self):
        assert _rule_based_subject("Rachel moved to Chicago.") == "Rachel"

    def test_dr_smith_hint(self):
        assert _rule_based_subject("I see Dr. Smith every week.") == "Dr. Smith"

    def test_user_prefix(self):
        assert _rule_based_subject("user: I need help with something.") == "user"

    def test_no_match_returns_none(self):
        assert _rule_based_subject("The weather is nice today.") is None


class TestPredictOracle:
    def test_returns_ground_truth_verbatim(self):
        gt = GROUND_TRUTH[0]
        pred = predict_oracle(gt)
        assert pred.subject == gt.subject
        assert pred.facet == gt.facet
        assert pred.state_family == gt.state_family
        assert pred.approach == "oracle"


class TestPredictGraphLookupOnly:
    async def test_uses_first_match_no_facet_or_state_family(self):
        gt = GROUND_TRUTH[0]
        driver = _StubDriver([{"name": "Rachel"}, {"name": "Chicago"}])
        clients = _StubClients(driver)
        pred = await predict_graph_lookup_only(clients, gt)
        assert pred.subject == "Rachel"
        assert pred.facet is None
        assert pred.state_family is None

    async def test_no_matches_returns_none_subject(self):
        gt = GROUND_TRUTH[0]
        driver = _StubDriver([])
        clients = _StubClients(driver)
        pred = await predict_graph_lookup_only(clients, gt)
        assert pred.subject is None


class TestPredictLlmOntology:
    async def test_correct_parse(self):
        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"subject": "Rachel", "facet": "Housing", "state_family": "Current residence", "reasoning": "x"}'
        ])
        pred = await predict_llm_ontology(backend, gt)
        assert pred.subject == "Rachel"
        assert pred.facet == "Housing"
        assert pred.state_family == "Current residence"

    async def test_unknown_values_become_none(self):
        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"subject": "unknown", "facet": "Unknown", "state_family": "unknown", "reasoning": "x"}'
        ])
        pred = await predict_llm_ontology(backend, gt)
        assert pred.subject is None
        assert pred.facet is None
        assert pred.state_family is None


class TestPredictTwoStage:
    async def test_two_calls_made(self):
        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"subject": "Rachel", "reasoning": "explicit name"}',
            '{"facet": "Housing", "state_family": "Current residence", "reasoning": "x"}',
        ])
        pred = await predict_two_stage(backend, gt)
        assert pred.subject == "Rachel"
        assert pred.facet == "Housing"
        assert pred.state_family == "Current residence"
        assert len(backend.calls) == 2

    async def test_second_call_receives_first_calls_subject(self):
        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"subject": "Rachel", "reasoning": "x"}',
            '{"facet": "Housing", "state_family": "Current residence", "reasoning": "x"}',
        ])
        await predict_two_stage(backend, gt)
        second_prompt = backend.calls[1]["user_prompt"]
        assert "Rachel" in second_prompt


class TestPredictHybridRulesThenLlm:
    async def test_full_rule_match_still_calls_llm_for_state_family(self):
        """830ce83f: rules resolve subject+facet correctly, but Housing has 2 possible
        state_family values in the ontology, so exactly 1 LLM call is still needed --
        this is NOT the "0 LLM calls" case the module's docstring speculated might
        happen; confirmed via live testing that it doesn't, for this fixture set."""
        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend(['{"state_family": "Current residence", "reasoning": "x"}'])
        pred = await predict_hybrid_rules_then_llm(backend, gt)
        assert pred.subject == "Rachel"
        assert pred.facet == "Housing"
        assert pred.state_family == "Current residence"
        assert len(backend.calls) == 1

    async def test_no_correction_mechanism_for_a_confident_wrong_rule(self):
        """852ce960: the rule confidently (and wrongly) matches Housing instead of
        Mortgage. Because the rule DID produce a facet (non-None), the full-LLM
        fallback (which could have corrected it) never triggers -- only the narrower
        state_family sub-call runs, scoped to the WRONG facet's candidate list. This
        locks in that real, disclosed limitation as expected behavior."""
        gt = GROUND_TRUTH[1]  # 852ce960
        backend = _StubChatBackend(['{"state_family": "Housing preferences", "reasoning": "x"}'])
        pred = await predict_hybrid_rules_then_llm(backend, gt)
        assert pred.facet == "Housing"  # wrong, but that's the documented behavior
        assert pred.facet != gt.facet  # confirms this really is a misclassification
        assert len(backend.calls) == 1  # only the state_family call, never the full fallback

    async def test_no_rule_match_falls_back_to_full_llm(self):
        gt = RoutingGroundTruth(
            "synthetic", "Something entirely unrelated to any keyword.", None,
            "Someone", "SomeFacet", "SomeState", "some_event",
        )
        backend = _StubChatBackend([
            '{"subject": "Someone", "facet": "SomeFacet", "state_family": "SomeState", "reasoning": "fallback"}'
        ])
        pred = await predict_hybrid_rules_then_llm(backend, gt)
        assert pred.subject == "Someone"
        assert pred.facet == "SomeFacet"
        assert len(backend.calls) == 1
        assert "llm fallback" in pred.raw_reasoning


class TestFacetOntology:
    def test_contains_all_three_fixture_facets(self):
        for gt in GROUND_TRUTH:
            assert gt.facet in FACET_ONTOLOGY
            assert gt.state_family in FACET_ONTOLOGY[gt.facet]


class TestKnownTriples:
    def test_includes_all_three_ground_truth_triples(self):
        from menhir.explorer.extraction_lab_metadata_production import _known_triples

        triples = _known_triples()
        for gt in GROUND_TRUTH:
            assert (gt.subject, gt.facet, gt.state_family) in triples

    def test_includes_decoy_triples_too(self):
        """Realistic: a production system doesn't know in advance which known triples
        are 'correct' for a given query -- decoys (e.g. Daniel's housing fact) are
        real, stored triples too, and must appear in the candidate-aware list."""
        from menhir.explorer.extraction_lab_metadata_production import _known_triples

        triples = _known_triples()
        assert ("Daniel", "Housing", "Current residence") in triples


class TestPredictCandidateAwareRanked:
    async def test_returns_top_hypothesis(self):
        from menhir.explorer.extraction_lab_metadata_production import predict_candidate_aware_ranked

        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"hypotheses": [{"subject": "Rachel", "facet": "Housing", '
            '"state_family": "Current residence", "confidence": 0.9}]}'
        ])
        pred = await predict_candidate_aware_ranked(backend, gt)
        assert pred.subject == "Rachel"
        assert pred.facet == "Housing"
        assert pred.state_family == "Current residence"
        assert pred.approach == "candidate_aware_ranked"

    async def test_empty_hypotheses_returns_none_fields(self):
        from menhir.explorer.extraction_lab_metadata_production import predict_candidate_aware_ranked

        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend(['{"hypotheses": []}'])
        pred = await predict_candidate_aware_ranked(backend, gt)
        assert pred.subject is None
        assert pred.facet is None
        assert pred.state_family is None

    async def test_second_hypothesis_preserved_in_reasoning_for_ranked_recovery(self):
        from menhir.explorer.extraction_lab_metadata_production import predict_candidate_aware_ranked

        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"hypotheses": ['
            '{"subject": "Rachel", "facet": "Housing", "state_family": "Housing preferences", "confidence": 0.6},'
            '{"subject": "Rachel", "facet": "Housing", "state_family": "Current residence", "confidence": 0.4}'
            ']}'
        ])
        pred = await predict_candidate_aware_ranked(backend, gt)
        assert pred.facet == "Housing"
        assert pred.state_family == "Housing preferences"  # top-ranked, even though wrong
        assert "Current residence" in pred.raw_reasoning  # 2nd hypothesis preserved for recovery logic

    async def test_only_one_llm_call(self):
        from menhir.explorer.extraction_lab_metadata_production import predict_candidate_aware_ranked

        gt = GROUND_TRUTH[0]
        backend = _StubChatBackend([
            '{"hypotheses": [{"subject": "Rachel", "facet": "Housing", '
            '"state_family": "Current residence", "confidence": 0.9}]}'
        ])
        await predict_candidate_aware_ranked(backend, gt)
        assert len(backend.calls) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
