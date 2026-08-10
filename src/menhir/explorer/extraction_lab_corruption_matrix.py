"""Phase 5, item 2 (menhir-extraction-context-ablation-handoff.md): metadata corruption
matrix. Phase 4b's 100% result assumed perfect candidate metadata. This module tests
whether the FROZEN structured selector (is_eligible / select_structured_only, unchanged
from Phase 4b) fails closed when candidate metadata is imperfect -- the realistic
condition production will actually have.

Deliberately deterministic (no LLM involved) -- this tests the SELECTOR's robustness to
corrupted candidate-side metadata, not a routing predictor's accuracy. All scenarios
built on the real 830ce83f (Rachel/Housing) fixture for consistency with earlier phases.

Eight corruption scenarios, per direct instruction:
  1. missing facet on the correct candidate
  2. incorrect state_family tag on the correct candidate
  3. unresolved subject (query-side target_subject is None/unknown)
  4. stale candidate missing its expired_at (should have been superseded but isn't marked)
  5. two candidates, same subject+state_family, different scopes, both otherwise eligible
  6. contradictory timestamps (valid_at after expired_at -- malformed data)
  7. a decoy incorrectly tagged as eligible (wrong-subject fact mistakenly tagged with
     the correct subject)
  8. correct context present only under a broader/different facet than the query expects
     (exact-match brittleness)

The critical distinction, per direct instruction: "the dangerous case is not missing
metadata... it is incorrect metadata that makes a decoy appear structurally eligible."
Each scenario is scored on whether the selector (a) finds the right answer, (b) safely
abstains, or (c) the dangerous outcome -- injects something wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from menhir.explorer.extraction_lab_eligibility_selection import (
    DecoyType,
    EligibilityScenario,
    StructuredFact,
    select_structured_only,
)


class CorruptionOutcome(str, Enum):
    CORRECT = "correct"              # found the right (or correctly abstained on) answer
    SAFE_ABSTAIN = "safe_abstain"    # abstained when it maybe could have found something -- safe, not ideal
    DANGEROUS = "dangerous"          # injected something wrong -- the failure mode that matters most


@dataclass(frozen=True)
class CorruptionScenario:
    scenario_id: str
    description: str
    scenario: EligibilityScenario
    # What the "safe" outcomes are: the id of the truly correct candidate (if the
    # selector should still find it despite corruption), or None if abstention is the
    # only safe outcome given this corruption.
    acceptable_selections: tuple[str | None, ...]


_REF = "2023-05-26T03:55:00+00:00"


def _base_correct() -> StructuredFact:
    return StructuredFact("c1", "Rachel", "Housing", "Current residence", None,
                           "Rachel previously moved to an apartment in Chicago.",
                           valid_at="2023-05-21T09:14:00+00:00")


def build_corruption_scenarios() -> list[CorruptionScenario]:
    scenarios = []

    # 1. Missing facet on the correct candidate -- is_eligible requires an exact facet
    # match, so a None facet on the ONLY correct candidate must fail the filter. Safe
    # outcome is abstention (the filter cannot confirm eligibility on missing data,
    # per the abstention-by-default design) -- NOT silently passing it through.
    c1_no_facet = StructuredFact("c1", "Rachel", None, "Current residence", None,
                                  "Rachel previously moved to an apartment in Chicago.",
                                  valid_at="2023-05-21T09:14:00+00:00")
    scenarios.append(CorruptionScenario(
        "1_missing_facet_on_correct",
        "The correct candidate is missing its facet tag entirely.",
        EligibilityScenario("corrupt_1", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (c1_no_facet,), "c1"),
        acceptable_selections=(None,),  # abstention is the only safe outcome
    ))

    # 2. Incorrect state_family tag on the correct candidate -- same logic, wrong tag
    # instead of missing tag. Also must abstain (the filter can't know the tag is wrong;
    # it can only refuse a non-match).
    c1_wrong_sf = StructuredFact("c1", "Rachel", "Housing", "Housing preferences", None,
                                  "Rachel previously moved to an apartment in Chicago.",
                                  valid_at="2023-05-21T09:14:00+00:00")
    scenarios.append(CorruptionScenario(
        "2_incorrect_state_family_on_correct",
        "The correct candidate is tagged with the wrong state_family.",
        EligibilityScenario("corrupt_2", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (c1_wrong_sf,), "c1"),
        acceptable_selections=(None,),
    ))

    # 3. Unresolved subject (query-side routing produced no subject at all) -- must
    # abstain regardless of what candidates exist, since there's nothing to match against.
    scenarios.append(CorruptionScenario(
        "3_unresolved_subject",
        "Query-side routing could not resolve a subject at all.",
        EligibilityScenario("corrupt_3", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "__UNRESOLVED__", "Housing", "Current residence", None, (_base_correct(),), "c1"),
        acceptable_selections=(None,),
    ))

    # 4. Stale candidate missing its expired_at -- a fact that SHOULD have been
    # superseded but the supersession was never recorded. is_eligible has no way to know
    # it's stale (no expired_at set), so it will be eligible -- this is a REAL, disclosed
    # limitation (the filter can only act on data it has), not something the selector can
    # fix on its own. Both the (undated-as-stale) old value and the correct current value
    # are structurally eligible; tie-break prefers the more recent valid_at, which
    # happens to be correct here, but only by coincidence of which one has a later date.
    stale_no_expiry = StructuredFact("c_stale", "Rachel", "Housing", "Current residence", None,
                                      "Rachel used to live in Denver.",
                                      valid_at="2023-01-01T00:00:00+00:00")  # no expired_at!
    scenarios.append(CorruptionScenario(
        "4_stale_missing_expired_at",
        "A superseded fact was never marked expired -- both old and new appear eligible.",
        EligibilityScenario("corrupt_4", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None,
                             (stale_no_expiry, _base_correct()), "c1"),
        acceptable_selections=("c1",),  # recency tie-break happens to save this one
    ))

    # 5. Two candidates, same subject+state_family, different scopes, both eligible if
    # scope isn't required by the query -- tests whether omitting scope from the query
    # target (as Phase 5's routing predictors currently do -- they don't predict scope)
    # creates real ambiguity the frozen selector cannot resolve safely.
    scope_a = StructuredFact("c1", "Rachel", "Housing", "Current residence", "Chicago apartment 2023",
                              "Rachel previously moved to an apartment in Chicago.",
                              valid_at="2023-05-21T09:14:00+00:00")
    scope_b = StructuredFact("c7", "Rachel", "Housing", "Current residence", "College dorm 2019",
                              "Rachel lived in a college dorm back in 2019.",
                              valid_at="2019-09-01T00:00:00+00:00")
    scenarios.append(CorruptionScenario(
        "5_same_state_family_different_scope_no_target_scope",
        "Two candidates share subject+state_family but describe different real-world "
        "events; the query provides no scope to disambiguate.",
        EligibilityScenario("corrupt_5", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None,  # no target_scope
                             (scope_a, scope_b), "c1"),
        acceptable_selections=("c1",),  # recency tie-break: c1 (2023) beats c7 (2019)
    ))

    # 6. Contradictory timestamps -- valid_at AFTER expired_at (malformed data). Must
    # not crash, and the safest behavior is to treat it as ineligible (cannot confirm a
    # sensible validity window) rather than guessing which timestamp to trust.
    contradictory = StructuredFact("c8", "Rachel", "Housing", "Current residence", None,
                                    "Rachel supposedly lived somewhere with contradictory dates.",
                                    valid_at="2023-06-01T00:00:00+00:00", expired_at="2023-01-01T00:00:00+00:00")
    scenarios.append(CorruptionScenario(
        "6_contradictory_timestamps",
        "A candidate has valid_at AFTER its own expired_at -- malformed data.",
        EligibilityScenario("corrupt_6", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (contradictory,), None),
        acceptable_selections=(None,),  # no genuinely correct answer exists in this pool
    ))

    # 7. THE dangerous case, made explicit: a wrong-subject decoy INCORRECTLY tagged
    # with the correct subject (a real production tagging error, not a hypothetical).
    # The selector has no way to know the tag is wrong -- it will treat it as eligible.
    # This is the one corruption type the deterministic filter CANNOT protect against by
    # design, and is flagged as such rather than papered over.
    mistagged_decoy = StructuredFact("c9", "Rachel", "Housing", "Current residence", None,
                                      "Daniel moved to a new apartment across town.",  # really about Daniel!
                                      valid_at="2023-05-20T00:00:00+00:00", decoy_type=DecoyType.WRONG_SUBJECT)
    scenarios.append(CorruptionScenario(
        "7_decoy_mistagged_as_correct_subject",
        "A fact that is really about a different person is mistagged with subject=Rachel.",
        EligibilityScenario("corrupt_7", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (mistagged_decoy,), None),
        # Only "abstain" is a genuinely safe outcome. Selecting c9 IS the dangerous
        # failure mode this scenario exists to test for -- it must score as DANGEROUS if
        # it happens, not be pre-classified as acceptable just because it was predicted.
        # is_eligible() has no mechanism to detect a false tag; a wrong-subject fact
        # mistagged with the right subject looks identical to a genuine one. This
        # scenario measures whether that's true, it does not excuse it in advance.
        acceptable_selections=(None,),
    ))

    # 8. Correct context exists but tagged under a broader/different facet than the
    # query predicts -- exact-match brittleness. E.g. query predicts facet="Housing"
    # but the stored fact was tagged facet="Life updates" (a real, valid, broader
    # category a different tagging pass might have used).
    broader_facet = StructuredFact("c10", "Rachel", "Life updates", "Current residence", None,
                                    "Rachel previously moved to an apartment in Chicago.",
                                    valid_at="2023-05-21T09:14:00+00:00")
    scenarios.append(CorruptionScenario(
        "8_correct_fact_under_broader_facet",
        "The correct fact exists but was tagged with a broader facet than the query predicts.",
        EligibilityScenario("corrupt_8", "lme-830ce83f",
                             "Rachel actually just moved back to the suburbs again.", _REF,
                             "Rachel", "Housing", "Current residence", None, (broader_facet,), "c10"),
        acceptable_selections=(None,),  # exact-match filter cannot bridge a facet-tagging mismatch
    ))

    return scenarios


def classify_outcome(corruption: CorruptionScenario) -> tuple[CorruptionOutcome, dict]:
    result = select_structured_only(corruption.scenario)
    selected = result["selected_id"]
    if selected in corruption.acceptable_selections:
        outcome = CorruptionOutcome.CORRECT if selected is not None else CorruptionOutcome.SAFE_ABSTAIN
    else:
        outcome = CorruptionOutcome.DANGEROUS
    return outcome, result
