"""CF-154 — a safe abstention must never be classified as DANGEROUS.

`classify_outcome`'s `else` branch was `DANGEROUS` whenever the selected id was not in
`acceptable_selections` — including `selected_id is None`, an abstention that injects
nothing. The abstention tests here construct a real `CorruptionScenario` whose
`select_structured_only` call abstains (reachable from the real selector, no monkeypatch),
then assert it resolves to `SAFE_ABSTAIN`.

The shipped `build_corruption_scenarios()` ratio is asserted unchanged (1/5/2) so the
latency of the defect is recorded rather than assumed.
"""

from __future__ import annotations

import pytest

from menhir.explorer.extraction_lab_corruption_matrix import (
    CorruptionOutcome,
    CorruptionScenario,
    build_corruption_scenarios,
    classify_outcome,
)
from menhir.explorer.extraction_lab_eligibility_selection import (
    EligibilityScenario,
    StructuredFact,
    select_structured_only,
)

_REF = "2023-05-26T03:55:00+00:00"


def _abstaining_scenario() -> CorruptionScenario:
    """A real scenario whose `select_structured_only` abstains: a single candidate whose
    `valid_at` is in the future relative to `reference_time`, so `is_eligible` rejects it and
    no candidate survives the filter -> `selected_id is None`."""
    future_candidate = StructuredFact(
        "c1", "Rachel", "Housing", "Current residence", None,
        "Rachel used to live in Denver.",
        valid_at="2024-01-01T00:00:00+00:00",  # in the future vs reference time -> ineligible
    )
    scenario = EligibilityScenario(
        "t_abstain", "lme-x", "Rachel actually just moved back to the suburbs again.", _REF,
        "Rachel", "Housing", "Current residence", None, (future_candidate,), "c1",
    )
    return CorruptionScenario(
        "t_abstain", "abstention on an ineligible-only pool", scenario,
        acceptable_selections=("c1",),  # no None member
    )


def _scenario_for_selection(selected_id: str | None) -> CorruptionScenario:
    """A scenario where we control the selection via monkeypatch; acceptable = ("c1",)."""
    candidate = StructuredFact(
        "c1", "Rachel", "Housing", "Current residence", None, "ok",
        valid_at="2023-05-21T09:14:00+00:00",
    )
    scenario = EligibilityScenario(
        "t_ctl", "lme-x", "msg", _REF, "Rachel", "Housing", "Current residence", None,
        (candidate,), "c1",
    )
    return CorruptionScenario(
        "t_ctl", "control", scenario, acceptable_selections=("c1",),
    )


def _with_selection(corruption: CorruptionScenario, selected_id: str | None) -> dict:
    return {
        "scenario_id": corruption.scenario.scenario_id,
        "selected_id": selected_id,
        "correct_candidate_id": corruption.scenario.correct_candidate_id,
        "correct": selected_id == corruption.scenario.correct_candidate_id,
        "is_decoy_selection": False,
        "decoy_type": None,
        "reasoning": "monkeypatched",
        "selector": "structured_only",
        "llm_calls": 0,
    }


@pytest.mark.unit
def test_abstention_with_no_none_in_acceptable_is_safe(monkeypatch):
    """Regression (the defect): `selected_id=None` with `acceptable_selections=("c1",)` must be
    SAFE_ABSTAIN, not DANGEROUS — an abstention injects nothing. Uses a REAL scenario that
    abstains (future valid_at), no monkeypatch."""
    corruption = _abstaining_scenario()
    assert select_structured_only(corruption.scenario)["selected_id"] is None
    outcome, _ = classify_outcome(corruption)
    assert outcome == CorruptionOutcome.SAFE_ABSTAIN


@pytest.mark.unit
def test_wrong_selection_still_dangerous(monkeypatch):
    """POSITIVE CONTROL: a genuinely wrong selection (`c9`) must STILL be DANGEROUS — without
    this, a fix that returned SAFE_ABSTAIN unconditionally would pass."""
    monkeypatch.setattr(
        "menhir.explorer.extraction_lab_corruption_matrix.select_structured_only",
        lambda scenario: _with_selection(_scenario_for_selection(None), "c9"),
    )
    outcome, _ = classify_outcome(_scenario_for_selection(None))
    assert outcome == CorruptionOutcome.DANGEROUS


@pytest.mark.unit
def test_correct_selection_still_correct(monkeypatch):
    """POSITIVE CONTROL: `selected_id="c1"` with `acceptable_selections=("c1",)` must STILL be
    CORRECT — the `if` branch must be untouched by the fix."""
    monkeypatch.setattr(
        "menhir.explorer.extraction_lab_corruption_matrix.select_structured_only",
        lambda scenario: _with_selection(_scenario_for_selection(None), "c1"),
    )
    outcome, _ = classify_outcome(_scenario_for_selection(None))
    assert outcome == CorruptionOutcome.CORRECT


@pytest.mark.unit
def test_abstention_with_none_in_acceptable_still_safe(monkeypatch):
    """POSITIVE CONTROL: `selected_id=None` with `acceptable_selections=(None,)` must STILL be
    SAFE_ABSTAIN — the `if` branch must be untouched by the fix."""
    corruption = _scenario_for_selection(None)
    # rebuild with acceptable_selections=(None,)
    corruption = CorruptionScenario(
        corruption.scenario_id, corruption.description, corruption.scenario,
        acceptable_selections=(None,),
    )
    monkeypatch.setattr(
        "menhir.explorer.extraction_lab_corruption_matrix.select_structured_only",
        lambda scenario: _with_selection(_scenario_for_selection(None), None),
    )
    outcome, _ = classify_outcome(corruption)
    assert outcome == CorruptionOutcome.SAFE_ABSTAIN


@pytest.mark.unit
def test_shipped_matrix_ratio_unchanged():
    """Latency guard: the shipped 8-scenario matrix still scores 1 DANGEROUS / 5 SAFE_ABSTAIN /
    2 CORRECT after this fix — the defect was latent, not live, and must remain so."""
    outcomes = [classify_outcome(s)[0] for s in build_corruption_scenarios()]
    assert outcomes.count(CorruptionOutcome.DANGEROUS) == 1
    assert outcomes.count(CorruptionOutcome.SAFE_ABSTAIN) == 5
    assert outcomes.count(CorruptionOutcome.CORRECT) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
