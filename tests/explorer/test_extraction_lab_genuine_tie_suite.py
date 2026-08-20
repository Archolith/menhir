"""Unit tests for the genuine-tie suite. Deterministic checks only (confirming the
scenarios really are structural ties) -- the actual LLM tie-breaking behavior is
scored live by the runner script, not here."""

import pytest

from menhir.explorer.extraction_lab_genuine_tie_suite import (
    build_genuine_tie_scenarios,
    confirm_genuine_ties,
)
from menhir.explorer.extraction_lab_eligibility_selection import select_structured_only


class TestBuildGenuineTieScenarios:
    def test_three_scenarios(self):
        assert len(build_genuine_tie_scenarios()) == 3

    def test_unique_ids(self):
        ids = [s.scenario_id for s in build_genuine_tie_scenarios()]
        assert len(ids) == len(set(ids))

    def test_all_scenarios_are_genuine_ties(self):
        """Regression guard: every scenario MUST leave exactly 2 eligible candidates
        after the hard filter -- if this ever drops to 0 or 1, the scenario stopped
        testing what it's named for (silently, without anyone noticing)."""
        scenarios = build_genuine_tie_scenarios()
        counts = confirm_genuine_ties(scenarios)
        for scenario_id, count in counts.items():
            assert count == 2, f"{scenario_id} has {count} eligible candidates, not a genuine tie"

    def test_structured_only_cannot_resolve_these_via_recency(self):
        """Confirms WHY these are genuine ties: select_structured_only (pure recency
        tie-break, no LLM) must pick SOMETHING (Python's max() breaks identical-key
        ties by iteration order, not by any real signal) -- proving the tie-break
        alone provides no principled answer here, unlike every prior phase's
        scenarios where recency alone was sufficient."""
        scenarios = build_genuine_tie_scenarios()
        for gts in scenarios:
            result = select_structured_only(gts.scenario)
            # It always picks *something* from the tied pair (never abstains, since
            # both are eligible) -- that "something" is an artifact of list order, not
            # a real decision. This is exactly why the LLM fallback exists.
            assert result["selected_id"] is not None
            assert result["selected_id"] in {c.id for c in gts.scenario.candidates}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
