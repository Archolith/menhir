"""Phase 5, item 4 (menhir-extraction-context-ablation-handoff.md): genuine-tie suite.

Every prior phase's scenarios resolved to 0-or-1 eligible candidates after structured
filtering, so select_structured_then_llm's LLM-fallback path has never once actually
run (confirmed: 0 llm_calls across 210 Phase 4b trials). This module deliberately
constructs scenarios where 2+ candidates survive the hard filter with IDENTICAL
valid_at timestamps -- the one case the existing recency tie-break structurally cannot
resolve -- to test whether the LLM fallback adds real value, or whether (as the
direct instruction anticipated) "temporal normalization or better scope identifiers
eliminate nearly all real ties, making the LLM unnecessary" in practice.

Three scenarios, deliberately different in what they test:
  1. symmetric_no_signal    -- two equally-plausible, differently-worded facts, same
                                timestamp, nothing in the message content favors either.
                                There may be no "right" answer here at all -- a real
                                data-quality problem (two live claims for one state),
                                not a solvable classification problem. Abstaining is as
                                defensible an outcome as picking either one.
  2. message_content_favors_one -- same structural tie, but the CURRENT MESSAGE's own
                                wording overlaps with one candidate's content more than
                                the other's -- tests whether the LLM correctly uses
                                that signal when pure structure cannot.
  3. exact_worked_example    -- the plan's own example verbatim ("Rachel lived in
                                Chicago" vs "Rachel lived in Austin"), same timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.explorer.extraction_lab_eligibility_selection import (
    EligibilityScenario,
    StructuredFact,
    eligible_candidates,
)

_REF = "2023-05-26T03:55:00+00:00"
_SAME_VALID_AT = "2023-05-21T09:14:00+00:00"  # identical for both candidates in every scenario


@dataclass(frozen=True)
class GenuineTieScenario:
    scenario_id: str
    description: str
    scenario: EligibilityScenario
    acceptable_selections: tuple[str | None, ...]  # what counts as a defensible outcome


def build_genuine_tie_scenarios() -> list[GenuineTieScenario]:
    scenarios = []

    # 1. Symmetric, no distinguishing signal anywhere -- both candidates equally
    # plausible, same timestamp, current message doesn't favor either.
    c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None,
                         "Rachel mentioned she signed a lease on a place downtown.",
                         valid_at=_SAME_VALID_AT)
    c2 = StructuredFact("c2", "Rachel", "Housing", "Current residence", None,
                         "Rachel mentioned she's staying in a new place near the park.",
                         valid_at=_SAME_VALID_AT)
    scenarios.append(GenuineTieScenario(
        "1_symmetric_no_signal",
        "Two equally-plausible current-residence claims, identical timestamp, no "
        "content overlap with the query message favoring either.",
        EligibilityScenario("tie_1", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (c1, c2), None),
        # No principled "correct" answer exists -- either a specific pick or
        # abstention is defensible; this scenario measures WHAT the LLM does under
        # genuine irreducible ambiguity, not whether it gets a "right" answer.
        acceptable_selections=("c1", "c2", None),
    ))

    # 2. Same structural tie, but the CURRENT MESSAGE's own wording overlaps with one
    # candidate specifically ("suburbs") -- a real signal beyond pure structure.
    c3 = StructuredFact("c3", "Rachel", "Housing", "Current residence", None,
                         "Rachel mentioned she moved to a place in the suburbs.",
                         valid_at=_SAME_VALID_AT)
    c4 = StructuredFact("c4", "Rachel", "Housing", "Current residence", None,
                         "Rachel mentioned she started a new fitness routine.",  # unrelated content, same tags
                         valid_at=_SAME_VALID_AT)
    scenarios.append(GenuineTieScenario(
        "2_message_content_favors_one",
        "Structural tie, but the query message's own wording ('suburbs') overlaps "
        "specifically with one candidate's content, not the other's.",
        EligibilityScenario("tie_2", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (c3, c4), "c3"),
        acceptable_selections=("c3",),  # a real, findable signal exists here
    ))

    # 3. The plan's own worked example, verbatim.
    c5 = StructuredFact("c5", "Rachel", "Housing", "Current residence", None,
                         "Rachel lived in Chicago.", valid_at=_SAME_VALID_AT)
    c6 = StructuredFact("c6", "Rachel", "Housing", "Current residence", None,
                         "Rachel lived in Austin.", valid_at=_SAME_VALID_AT)
    scenarios.append(GenuineTieScenario(
        "3_exact_worked_example",
        "The plan's own example: 'Rachel lived in Chicago' vs 'Rachel lived in "
        "Austin,' same subject/facet/state_family, identical timestamp.",
        EligibilityScenario("tie_3", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (c5, c6), None),
        acceptable_selections=("c5", "c6", None),  # no real signal favors either
    ))

    return scenarios


def confirm_genuine_ties(scenarios: list[GenuineTieScenario]) -> dict[str, int]:
    """Sanity check BEFORE spending any LLM calls: confirm every scenario really does
    leave 2+ candidates eligible after the hard filter (i.e. these are actually the
    untested case, not accidentally resolved to 0-or-1 like every prior phase's
    scenarios were)."""
    counts = {}
    for gts in scenarios:
        counts[gts.scenario_id] = len(eligible_candidates(gts.scenario))
    return counts
