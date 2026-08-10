"""Unit tests for the Phase 5 metadata corruption matrix. Fully deterministic (no LLM,
no graph) -- tests the frozen Phase 4b selector's robustness to corrupted candidate
metadata directly."""

import pytest

from menhir.explorer.extraction_lab_corruption_matrix import (
    CorruptionOutcome,
    build_corruption_scenarios,
    classify_outcome,
)


class TestBuildCorruptionScenarios:
    def test_eight_scenarios(self):
        assert len(build_corruption_scenarios()) == 8

    def test_unique_ids(self):
        ids = [s.scenario_id for s in build_corruption_scenarios()]
        assert len(ids) == len(set(ids))


class TestClassifyOutcome:
    def test_missing_facet_on_correct_abstains_safely(self):
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, _ = classify_outcome(scenarios["1_missing_facet_on_correct"])
        assert outcome == CorruptionOutcome.SAFE_ABSTAIN

    def test_incorrect_state_family_on_correct_abstains_safely(self):
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, _ = classify_outcome(scenarios["2_incorrect_state_family_on_correct"])
        assert outcome == CorruptionOutcome.SAFE_ABSTAIN

    def test_unresolved_subject_abstains_safely(self):
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, _ = classify_outcome(scenarios["3_unresolved_subject"])
        assert outcome == CorruptionOutcome.SAFE_ABSTAIN

    def test_stale_missing_expired_at_still_finds_correct_via_recency(self):
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, result = classify_outcome(scenarios["4_stale_missing_expired_at"])
        assert outcome == CorruptionOutcome.CORRECT
        assert result["selected_id"] == "c1"

    def test_contradictory_timestamps_abstains_safely(self):
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, _ = classify_outcome(scenarios["6_contradictory_timestamps"])
        assert outcome == CorruptionOutcome.SAFE_ABSTAIN

    def test_mistagged_decoy_is_the_confirmed_dangerous_case(self):
        """The one scenario that MUST score as dangerous -- this is the exact failure
        mode the whole corruption matrix exists to catch. If this ever silently starts
        passing, something changed about is_eligible() in a way that needs scrutiny,
        not celebration -- a deterministic exact-match filter cannot detect a false tag
        by construction, so this scenario should stay dangerous until the underlying
        data-integrity guarantee changes (e.g. cryptographic/provenance-verified tags),
        not until the filter logic changes."""
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, result = classify_outcome(scenarios["7_decoy_mistagged_as_correct_subject"])
        assert outcome == CorruptionOutcome.DANGEROUS
        assert result["selected_id"] == "c9"

    def test_broader_facet_mismatch_abstains_safely(self):
        scenarios = {s.scenario_id: s for s in build_corruption_scenarios()}
        outcome, _ = classify_outcome(scenarios["8_correct_fact_under_broader_facet"])
        assert outcome == CorruptionOutcome.SAFE_ABSTAIN

    def test_exactly_one_dangerous_outcome_across_all_eight(self):
        """Headline regression guard for the whole matrix: 5 safely abstain, 2 still
        find the correct answer despite corruption, exactly 1 dangerous. If this ratio
        changes, the change needs to be understood before trusting the selector
        further -- not silently absorbed."""
        outcomes = [classify_outcome(s)[0] for s in build_corruption_scenarios()]
        assert outcomes.count(CorruptionOutcome.DANGEROUS) == 1
        assert outcomes.count(CorruptionOutcome.SAFE_ABSTAIN) == 5
        assert outcomes.count(CorruptionOutcome.CORRECT) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
