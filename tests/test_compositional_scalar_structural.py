"""Pure structural grammar tests for compositional scalar identity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from menhir.services.deterministic_scalar_extractor import DeterministicScalarExtractor
from menhir.services.structural_scalar_composer import (
    REASON_CLAIM_INCOMPLETE,
    REASON_CONSTRAINT_MISMATCH,
    REASON_DUPLICATE_GROUNDING,
    REASON_NO_GROUNDING,
    REASON_OPERATION_UNSUPPORTED,
    REASON_RELATION_UNKNOWN,
    REASON_SUBJECT_MISMATCH,
    REASON_TARGET_UNRESOLVED,
    REASON_UNBOUND_POSSESSIVE,
    REASON_UNSAFE_HEDGE,
    REASON_UNSAFE_HYPOTHETICAL,
    REASON_UNSAFE_LIST,
    REASON_UNSAFE_MODAL,
    REASON_UNSAFE_ONE_OFF,
    REASON_UNSAFE_PAST_ONLY,
    REASON_UNSAFE_QUESTION,
    STRUCTURAL_COMPOSER_VERSION,
    compose_structural_scalar_identity,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal


def _proposal(source, stated_span=None, **overrides):
    span = stated_span or source.rstrip(".")
    start = source.lower().index(span.lower())
    values = {
        "subject_text": "user",
        "attribute": "arbitrary_model_label",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 3,
        "stated_span": span,
        "episode_uuid": "episode-1",
        "span_start": start,
        "span_end": start + len(span),
        "when": None,
    }
    values.update(overrides)
    return TypedScalarProposal(**values)


@pytest.mark.parametrize(
    ("source", "proposal_fields", "relation", "target", "rule_id"),
    [
        ("I have 3 coins.", {}, "quantity", "coins", "struct.quantity.have_v1"),
        (
            "I have completed 12 lessons so far.",
            {"value": 12},
            "quantity",
            "lessons",
            "struct.quantity.completed_v1",
        ),
        (
            "I have finished 7 tasks to date.",
            {"value": 7},
            "quantity",
            "tasks",
            "struct.quantity.completed_v1",
        ),
        (
            "I have closed 40 tickets so far.",
            {"value": 40},
            "quantity",
            "tickets",
            "struct.quantity.completed_v1",
        ),
        (
            "My savings balance is $500.",
            {"value_kind": "money", "unit": "usd", "value": 500},
            "balance",
            "savings",
            "struct.balance.possessive_v1",
        ),
        (
            "I have 500 dollars in my rainy day account.",
            {"value_kind": "money", "unit": "usd", "value": 500},
            "balance",
            "rainy day",
            "struct.balance.account_v1",
        ),
        (
            "I am 180 cm tall.",
            {"value_kind": "measurement", "unit": "cm", "value": 180},
            "measurement",
            "tall",
            "struct.measurement.self_tall_v1",
        ),
        (
            "I weigh 72 kg.",
            {"value_kind": "measurement", "unit": "kg", "value": 72},
            "measurement",
            "weigh",
            "struct.measurement.self_v1",
        ),
        (
            "My weight is 63 kg.",
            {"value_kind": "measurement", "unit": "kg", "value": 63},
            "measurement",
            "weight",
            "struct.measurement.possessive_weight_v1",
        ),
        (
            "my height is 172 cm.",
            {"value_kind": "measurement", "unit": "cm", "value": 172},
            "measurement",
            "height",
            "struct.measurement.possessive_height_v1",
        ),
        (
            "I wake at 07:30.",
            {"value_kind": "clock_time", "value": "07:30"},
            "schedule_time",
            "wake",
            "struct.schedule_time.self_v1",
        ),
        (
            "My bedtime is 22:00.",
            {"value_kind": "clock_time", "value": "22:00"},
            "schedule_time",
            "bedtime",
            "struct.schedule_time.possessive_v1",
        ),
        (
            "I sleep 8 hours per night.",
            {"value_kind": "duration", "unit": "hours", "value": 8},
            "duration",
            "sleep",
            "struct.duration.self_recurring_v1",
        ),
        (
            "I run 3 times per week.",
            {"value_kind": "frequency", "unit": "week", "value": 3},
            "frequency",
            "run",
            "struct.frequency.rate_v1",
        ),
        (
            "I go to the gym every other week.",
            {"value_kind": "frequency", "unit": "week", "value": 0.5},
            "frequency",
            "go to the gym",
            "struct.frequency.interval_v1",
        ),
    ],
)
def test_structural_positive_contract(source, proposal_fields, relation, target, rule_id):
    result = compose_structural_scalar_identity(_proposal(source, **proposal_fields), source)

    assert result.composed is True
    assert result.identity is not None
    assert result.identity.relation_type == relation
    assert result.identity.target_or_scope == (target, "")
    assert result.identity.provenance.derivation_version == STRUCTURAL_COMPOSER_VERSION
    assert result.identity.provenance.rule_id == rule_id
    assert result.receipt.rule_id == rule_id
    assert result.receipt.reason_code is None
    assert source[result.receipt.target_start:result.receipt.target_end] == result.receipt.target_text
    assert result.receipt.target_text == target


@pytest.mark.parametrize(
    ("source", "expected_target", "expected_unit", "expected_value"),
    [
        ("my weight is 70.5 kilograms.", "weight", "kg", 70.5),
        ("My weight is 64 kilos.", "weight", "kg", 64),
        ("my height is 175 centimeters.", "height", "cm", 175),
        ("My height is 181 centimetres.", "height", "cm", 181),
    ],
)
def test_possessive_measurement_extracts_and_composes_end_to_end(
    source, expected_target, expected_unit, expected_value
):
    extraction = DeterministicScalarExtractor().extract(
        [SimpleNamespace(uuid="episode-1", content=source, reference_time=None)]
    )

    assert len(extraction.proposals) == 1
    proposal = extraction.proposals[0]
    assert (proposal.unit, proposal.value) == (expected_unit, expected_value)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is True
    assert result.identity is not None
    assert result.identity.target_or_scope == (expected_target, "")
    assert result.identity.provenance.derivation_version == STRUCTURAL_COMPOSER_VERSION


@pytest.mark.parametrize(
    ("source", "fields", "reason"),
    [
        (
            "Could my weight be 70 kg?",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
            REASON_UNSAFE_QUESTION,
        ),
        (
            "If my weight is 70 kg, I qualify.",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
            REASON_UNSAFE_HYPOTHETICAL,
        ),
        (
            "My weight might be 70 kg.",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
            REASON_UNSAFE_MODAL,
        ),
        (
            "My weight is about 70 kg.",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
            REASON_UNSAFE_HEDGE,
        ),
        (
            "My weight used to be 70 kg.",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
            REASON_UNSAFE_PAST_ONLY,
        ),
        (
            "My weight is 70 kg and my height is 180 cm.",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
            REASON_UNSAFE_LIST,
        ),
    ],
)
def test_possessive_measurement_unsafe_context_still_abstains(source, fields, reason):
    result = compose_structural_scalar_identity(_proposal(source, **fields), source)

    assert result.identity is None
    assert result.receipt.reason_code == reason


def test_date_prefix_and_shorter_grounded_quote_use_safe_sentence_context():
    source = "[2026-01-03] I have 3 hand-painted tokens."
    proposal = _proposal(source, stated_span="have 3 hand-painted tokens")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is True
    assert result.identity.target_or_scope == ("hand-painted tokens", "")


@pytest.mark.parametrize(
    ("source", "span", "reason"),
    [
        ("Did I have 3 coins?", "I have 3 coins", REASON_UNSAFE_QUESTION),
        ("If I have 3 coins, I can trade.", "I have 3 coins", REASON_UNSAFE_HYPOTHETICAL),
        ("Maybe I have 3 coins.", "I have 3 coins", REASON_UNSAFE_MODAL),
        ("I have about 3 coins.", "I have about 3 coins", REASON_UNSAFE_HEDGE),
        ("I used to have 3 coins.", "I used to have 3 coins", REASON_UNSAFE_PAST_ONLY),
        ("I paid $250 for headphones.", "I paid $250 for headphones", REASON_UNSAFE_ONE_OFF),
        ("I have 3 cats and 2 dogs.", "I have 3 cats and 2 dogs", REASON_UNSAFE_LIST),
    ],
)
def test_unsafe_sentence_context_abstains(source, span, reason):
    proposal = _proposal(source, stated_span=span)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is False
    assert result.identity is None
    assert result.receipt.reason_code == reason


def test_target_must_be_inside_the_proposal_grounding():
    source = "I have 3 coins."
    proposal = _proposal(source, stated_span="3", value=3)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CLAIM_INCOMPLETE


def test_value_and_relation_cues_must_be_inside_the_proposal_grounding():
    source = "I have 3 coins."
    proposal = _proposal(source, stated_span="coins", value=3)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CLAIM_INCOMPLETE


def test_unit_cue_must_be_inside_the_proposal_grounding():
    source = "I am 180 cm tall."
    proposal = _proposal(
        source,
        stated_span="I am 180",
        value_kind="measurement",
        value=180,
        unit="cm",
    )

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CLAIM_INCOMPLETE


def test_every_redundant_currency_cue_must_be_inside_the_proposal_grounding():
    source = "My savings balance is $500 USD."
    proposal = _proposal(
        source,
        stated_span="My savings balance is $500",
        value_kind="money",
        value=500,
        unit="usd",
    )

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CLAIM_INCOMPLETE


def test_recurring_duration_period_must_be_inside_the_proposal_grounding():
    source = "I sleep 8 hours per night."
    proposal = _proposal(
        source,
        stated_span="I sleep 8 hours per",
        value_kind="duration",
        value=8,
        unit="hours",
    )

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CLAIM_INCOMPLETE


def test_quantity_target_with_unconsumed_tail_is_not_trimmed_or_guessed():
    source = "I have 3 coins in my game."
    proposal = _proposal(source)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_TARGET_UNRESOLVED


@pytest.mark.parametrize(
    "source",
    [
        "I have 2 other bikes.",
        "I have 3 additional bikes.",
        "I have 3 extra bikes.",
        "I have 3 remaining bikes.",
        "I have 3 more bikes.",
        "I have 3 things.",
        "I have 3 thing.",
        "I have 3 items.",
        "I have 3 item.",
        "I have 3 stuff.",
        "I have 3 one.",
        "I have 3 ones.",
    ],
)
def test_quantity_subset_modifiers_and_empty_targets_abstain(source):
    value = int(source.split()[2])
    proposal = _proposal(source, value=value)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.identity is None
    assert result.receipt.reason_code == REASON_TARGET_UNRESOLVED


@pytest.mark.parametrize(
    "source",
    [
        "I have completed 3 other lessons so far.",
        "I have finished 3 more tasks to date.",
        "I have closed 3 additional tickets so far.",
        "I have completed 3 ones so far.",
        "I have finished 3 items to date.",
        "I have closed 3 stuff so far.",
        "I have completed 3 total tasks so far.",
    ],
)
def test_completed_quantity_subset_and_generic_targets_abstain(source):
    result = compose_structural_scalar_identity(
        _proposal(source, value=3),
        source,
    )

    assert result.identity is None
    assert result.receipt.reason_code == REASON_TARGET_UNRESOLVED


@pytest.mark.parametrize(
    "source",
    [
        "I completed 3 lessons so far.",
        "I had completed 3 lessons so far.",
        "I will have completed 3 lessons so far.",
        "I may have completed 3 lessons so far.",
        "I haven't completed 3 lessons so far.",
        "I have not completed 3 lessons so far.",
        "I have completed 3 lessons since January.",
        "I have finished 3 tasks last year.",
        "I have closed 3 tickets about 4 times.",
        "I have completed 3 lessons.",
        "I have completed 3 lessons up to now.",
        "I have completed 3 lessons and 2 tests so far.",
        "I have finished 3 lessons, then 2 more tasks to date.",
        "Did I have completed 3 lessons so far?",
        "If I have completed 3 lessons so far, I can stop.",
    ],
)
def test_completed_quantity_requires_present_perfect_cumulative_full_span(source):
    result = compose_structural_scalar_identity(
        _proposal(source, value=3),
        source,
    )

    assert result.identity is None


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        (
            "I have completed 3 kilograms so far.",
            {"value_kind": "measurement", "unit": "kg", "value": 3},
        ),
        (
            "I have closed 3 usd so far.",
            {"value_kind": "money", "unit": "usd", "value": 3},
        ),
    ],
)
def test_completed_quantity_is_count_only(source, fields):
    result = compose_structural_scalar_identity(_proposal(source, **fields), source)

    assert result.identity is None


def test_quantity_specific_hyphenated_target_remains_composable():
    source = "I have 3 extra-large bikes."

    result = compose_structural_scalar_identity(_proposal(source), source)

    assert result.composed is True
    assert result.identity is not None
    assert result.identity.target_or_scope[0] == "extra-large bikes"


def test_adjacent_transposition_between_source_target_and_attribute_abstains():
    source = "I have 6 boosk."
    proposal = _proposal(source, attribute="books", value=6)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.identity is None
    assert result.receipt.reason_code == REASON_TARGET_UNRESOLVED
    assert result.receipt.rule_id == "struct.quantity.have_v1"


@pytest.mark.parametrize(
    ("source", "attribute"),
    [
        ("I have 6 books.", "books"),
        ("I have 3 extra-large bikes.", "extra-large bikes"),
        ("I have 3 records.", "records"),
        ("I have 6 boooks.", "books"),
    ],
)
def test_exact_open_world_and_non_transposition_targets_remain_composable(source, attribute):
    result = compose_structural_scalar_identity(
        _proposal(source, attribute=attribute, value=int(source.split()[2])),
        source,
    )

    assert result.composed is True


def test_owned_object_possessive_does_not_become_a_user_quantity():
    source = "My car has 4 wheels."
    proposal = _proposal(source, value=4)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_UNBOUND_POSSESSIVE


def test_named_or_third_party_subject_abstains():
    source = "Alex has 3 coins."
    proposal = _proposal(source, subject_text="Alex")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_SUBJECT_MISMATCH


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        ("We have 3 coins.", {}),
        ("Our bedtime is 22:00.", {"value_kind": "clock_time", "value": "22:00"}),
        (
            "We sleep 8 hours per night.",
            {"value_kind": "duration", "value": 8, "unit": "hours"},
        ),
        (
            "Our savings balance is 500 dollars.",
            {"value_kind": "money", "value": 500, "unit": "usd"},
        ),
    ],
)
def test_collective_self_is_not_collapsed_into_individual_self(source, fields):
    result = compose_structural_scalar_identity(_proposal(source, **fields), source)

    assert result.receipt.reason_code == REASON_SUBJECT_MISMATCH


def test_collective_possessive_measurement_is_not_individual_self():
    source = "Our weight is 70 kg."
    proposal = _proposal(
        source,
        value_kind="measurement",
        value=70,
        unit="kg",
    )

    result = compose_structural_scalar_identity(proposal, source)

    assert result.identity is None
    assert result.receipt.reason_code == REASON_SUBJECT_MISMATCH


def test_duplicate_grounded_span_abstains():
    source = "I have 3 coins. I have 3 coins."
    proposal = _proposal(source, stated_span="I have 3 coins")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_DUPLICATE_GROUNDING


def test_malformed_offsets_abstain():
    source = "I have 3 coins."
    proposal = replace(_proposal(source), span_start=1)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_NO_GROUNDING


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"value": 4}, "struct.value_mismatch"),
        ({"value_kind": "measurement", "unit": "cm"}, REASON_CONSTRAINT_MISMATCH),
        ({"operation": "delta"}, REASON_OPERATION_UNSUPPORTED),
    ],
)
def test_typed_constraint_mismatch_abstains(changes, reason):
    source = "I have 3 coins."
    proposal = replace(_proposal(source), **changes)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == reason


def test_source_unit_must_match_proposal_unit():
    source = "I weigh 72 kg."
    proposal = _proposal(
        source, value_kind="measurement", value=72, unit="cm")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CONSTRAINT_MISMATCH


@pytest.mark.parametrize(
    ("source", "fields", "reason"),
    [
        (
            "My favorite color is 3 bananas.",
            {"value_kind": "measurement", "value": 3, "unit": "bananas"},
            REASON_RELATION_UNKNOWN,
        ),
        (
            "I weigh 72 bananas.",
            {"value_kind": "measurement", "value": 72, "unit": "bananas"},
            REASON_RELATION_UNKNOWN,
        ),
    ],
)
def test_unknown_measurement_units_abstain(source, fields, reason):
    result = compose_structural_scalar_identity(_proposal(source, **fields), source)

    assert result.receipt.reason_code == reason


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        ("I am 180 kg tall.", {"value_kind": "measurement", "value": 180, "unit": "kg"}),
        ("I weigh 180 cm.", {"value_kind": "measurement", "value": 180, "unit": "cm"}),
        ("My weight is 180 cm.", {"value_kind": "measurement", "value": 180, "unit": "cm"}),
        ("My height is 70 kg.", {"value_kind": "measurement", "value": 70, "unit": "kg"}),
        (
            "My weight is 150 pounds.",
            {"value_kind": "measurement", "value": 150, "unit": "pounds"},
        ),
        (
            "My height is 70 inches.",
            {"value_kind": "measurement", "value": 70, "unit": "inches"},
        ),
        ("My dog's weight is 20 kg.", {"value_kind": "measurement", "value": 20, "unit": "kg"}),
        ("My car is 3 kg.", {"value_kind": "measurement", "value": 3, "unit": "kg"}),
        ("My car takes 25 minutes.", {"value_kind": "duration", "value": 25, "unit": "minutes"}),
    ],
)
def test_open_possessive_or_unit_incompatible_shapes_stay_fallback(source, fields):
    result = compose_structural_scalar_identity(_proposal(source, **fields), source)

    assert result.identity is None
    assert result.receipt.reason_code == REASON_RELATION_UNKNOWN


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        (
            "My weight is between 70 and 75 kg.",
            {"value_kind": "measurement", "value": [70, 75], "unit": "kg"},
        ),
        (
            "My weight is 70 kg since June.",
            {"value_kind": "measurement", "value": 70, "unit": "kg"},
        ),
    ],
)
def test_possessive_measurement_noncurrent_or_range_shapes_stay_fallback(source, fields):
    result = compose_structural_scalar_identity(_proposal(source, **fields), source)

    assert result.identity is None


@pytest.mark.parametrize("source", ["I drive at 07:30.", "I arrive at 07:30."])
def test_generic_event_or_activity_time_is_not_a_standing_schedule(source):
    proposal = _proposal(source, value_kind="clock_time", value="07:30")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_RELATION_UNKNOWN


def test_event_like_possessive_time_is_not_a_safe_schedule():
    source = "My departure time is 07:30."
    proposal = _proposal(source, value_kind="clock_time", value="07:30")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_RELATION_UNKNOWN


@pytest.mark.parametrize("modifier", ["next", "previous", "future", "upcoming"])
def test_noncurrent_modifier_is_not_absorbed_into_balance_target(modifier):
    source = f"My {modifier} savings balance is 500 dollars."
    proposal = _proposal(
        source,
        value_kind="money",
        value=500,
        unit="usd",
    )

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_TARGET_UNRESOLVED


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("I have 3 coins two days ago.", REASON_UNSAFE_PAST_ONLY),
        ("I have 3 coins last night.", REASON_UNSAFE_PAST_ONLY),
        ("I have 3 coins sometimes.", REASON_UNSAFE_HEDGE),
        ("I have 3 coins tomorrow.", REASON_UNSAFE_MODAL),
        ("I have 3 coins as of 2026-01-03.", REASON_TARGET_UNRESOLVED),
        ("I have 3 coins next Tuesday.", REASON_TARGET_UNRESOLVED),
        ("I have 3 coins this Friday.", REASON_TARGET_UNRESOLVED),
        ("I have 3 coins every day.", REASON_TARGET_UNRESOLVED),
        ("I have 3 coins later.", REASON_TARGET_UNRESOLVED),
        ("I have 3 coins before lunch.", REASON_TARGET_UNRESOLVED),
        ("I have 3 coins after lunch.", REASON_TARGET_UNRESOLVED),
    ],
)
def test_temporal_or_frequency_tail_is_not_absorbed_into_quantity_target(source, reason):
    result = compose_structural_scalar_identity(_proposal(source), source)

    assert result.receipt.reason_code == reason


def test_case_different_model_quote_keeps_exact_offsets_and_composes():
    source = "I have 3 coins."
    proposal = _proposal(source, stated_span="i have 3 coins")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is True
    assert result.identity.target_or_scope[0] == "coins"


def test_grounded_quote_may_include_terminal_punctuation_without_absorbing_next_sentence():
    source = "I have 3 coins. This sentence is unrelated."
    proposal = _proposal(source, stated_span="I have 3 coins.")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is True
    assert result.identity.target_or_scope[0] == "coins"


def test_numeric_comma_format_matches_normalized_value():
    source = "I have 1,000 coins."
    proposal = _proposal(source, value=1000)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is True
    assert result.identity.value == "1000"


def test_invalid_mixed_clock_abstains():
    source = "I wake at 22:15 PM."
    proposal = _proposal(source, value_kind="clock_time", value="22:15")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == "struct.value_mismatch"


def test_impossible_clock_abstains_before_composition():
    source = "I wake at 25:00."
    proposal = _proposal(source, value_kind="clock_time", value="25:00")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.receipt.reason_code == REASON_CONSTRAINT_MISMATCH


@pytest.mark.parametrize("source", ["I run 3 times.", "I run often."])
def test_frequency_without_exact_rate_or_period_stays_fallback(source):
    proposal = _proposal(source, value_kind="frequency", value=3, unit="week")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.composed is False


def test_open_target_changes_are_not_alias_collapsed():
    coins_source = "I have 3 coins."
    tokens_source = "I have 3 tokens."

    coins = compose_structural_scalar_identity(_proposal(coins_source), coins_source)
    tokens = compose_structural_scalar_identity(_proposal(tokens_source), tokens_source)

    assert coins.identity.target_or_scope[0] == "coins"
    assert tokens.identity.target_or_scope[0] == "tokens"
    assert coins.identity.semantic_key != tokens.identity.semantic_key


def test_approved_whitespace_normalization_collapses_open_target_spacing():
    single_source = "I have 3 rainy day tokens."
    double_source = "I have 3 rainy  day tokens."

    single = compose_structural_scalar_identity(_proposal(single_source), single_source)
    double = compose_structural_scalar_identity(_proposal(double_source), double_source)

    assert double.receipt.target_text == "rainy  day tokens"
    assert single.identity.target_or_scope[0] == double.identity.target_or_scope[0]
    assert single.identity.semantic_key == double.identity.semantic_key


def test_unknown_structure_abstains_without_implicit_state_fallback():
    source = "There are 3 coins."
    proposal = _proposal(source)

    result = compose_structural_scalar_identity(proposal, source)

    assert result.identity is None
    assert result.receipt.reason_code == REASON_RELATION_UNKNOWN


def test_literal_verb_target_is_not_alias_rewritten():
    source = "I weigh 72 kg."
    proposal = _proposal(
        source, value_kind="measurement", value=72, unit="kg")

    result = compose_structural_scalar_identity(proposal, source)

    assert result.identity.target_or_scope[0] == "weigh"
    assert result.identity.target_or_scope[0] != "weight"


def test_structural_composition_is_deterministic_and_replay_stable():
    source = "I run 3 times per week."
    proposal = _proposal(
        source, value_kind="frequency", value=3, unit="week")

    first = compose_structural_scalar_identity(proposal, source)
    second = compose_structural_scalar_identity(proposal, source)

    assert first == second
    assert first.identity.semantic_key == second.identity.semantic_key
