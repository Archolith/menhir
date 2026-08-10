"""Table-driven tests for the deterministic v0.1 scalar extractor (Phase 1, shadow-safe).

Every constructed raw observation is routed through `typed_scalar_rules.parse_scalar_row`, so
grounding, value validation, hedge abstention, temporal disposition, normalization, and source
identity are the SHARED pipeline's guarantees — these tests assert the extractor's template
registry, receipt/abstention contract, and deterministic identity, plus the shared pipeline's
inherited temporal guards.

Examples are deliberately generic (coins, books, weight, commute, gym) — never benchmark-task
specific phrases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from menhir.domain.typed_assertion import build_source_key
from menhir.services.deterministic_scalar_extractor import (
    EXTRACTOR_VERSION,
    OUTCOME_ADMITTED,
    OUTCOME_DROPPED,
    REASON_TEMPLATE_COLLISION,
    REASON_UNCONSUMED_CONTEXT,
    TEMPLATE_VERSION,
    DeterministicScalarExtractor,
    SurfaceTemplate,
)
from menhir.services.typed_scalar_rules import (
    SELF_SUBJECT_DISPLAY,
    classify_absolute_semantics,
    parse_scalar_row,
)


@dataclass(frozen=True)
class _Episode:
    uuid: str
    content: str
    reference_time: str | None = None


def _run(content: str, templates=None):
    episodes = [_Episode("ep-1", content)]
    extractor = (DeterministicScalarExtractor(templates=templates)
                 if templates is not None else DeterministicScalarExtractor())
    return extractor.extract(episodes)


def _assert_proposals(res, content: str, expected):
    """Assert exact proposal contract: fields, offsets, source_key, provenance, no when."""
    assert len(res.proposals) == len(expected), [p.stated_span for p in res.proposals]
    for prop, (attribute, value_kind, operation, unit, value, span) in zip(res.proposals, expected):
        assert (prop.attribute, prop.value_kind, prop.operation, prop.unit, prop.value) == (
            attribute, value_kind, operation, unit, value)
        assert prop.subject_text == SELF_SUBJECT_DISPLAY
        assert prop.scope == ""
        assert prop.stated_span == span
        start = content.index(span)
        end = start + len(span)
        assert (prop.span_start, prop.span_end) == (start, end)
        assert prop.source_key == build_source_key("ep-1", start, end, 0)
        assert prop.episode_uuid == "ep-1"
        assert prop.when is None
        assert prop.claim_ordinal == 0


# ------------------------------------------------------------------------------------------------ #
# Positive contract cases: exact proposal, offset, and source-key assertions
# ------------------------------------------------------------------------------------------------ #

POSITIVE_CASES = [
    ("I have 37 coins.", (("coins", "count", "absolute", "", 37, "I have 37 coins"),)),
    ("I have 12 books and I wake up at 7:30.",
     (("books", "count", "absolute", "", 12, "I have 12 books"),
      ("wake_time", "clock_time", "absolute", "", "07:30", "I wake up at 7:30"))),
    ("I've got 5 cats.", (("cats", "count", "absolute", "", 5, "I've got 5 cats"),)),
    ("I have got 5 cats.", (("cats", "count", "absolute", "", 5, "I have got 5 cats"),)),
    ("I have 3,500 coins.", (("coins", "count", "absolute", "", 3500, "I have 3,500 coins"),)),
    ("my savings balance is $500.",
     (("savings_balance", "money", "absolute", "usd", 500, "my savings balance is $500"),)),
    ("I have $250.00 in my checking account.",
     (("checking_balance", "money", "absolute", "usd", 250.0,
       "I have $250.00 in my checking account"),)),
    ("I have 300 dollars in my savings account.",
     (("savings_balance", "money", "absolute", "usd", 300,
       "I have 300 dollars in my savings account"),)),
    ("I weigh 70 kg.", (("weight", "measurement", "absolute", "kg", 70, "I weigh 70 kg"),)),
    ("my weight is 70.5 kilograms.",
     (("weight", "measurement", "absolute", "kg", 70.5, "my weight is 70.5 kilograms"),)),
    ("I am 180 cm tall.", (("height", "measurement", "absolute", "cm", 180, "I am 180 cm tall"),)),
    ("my height is 175 centimeters.",
     (("height", "measurement", "absolute", "cm", 175, "my height is 175 centimeters"),)),
    ("I am 75% done with my degree.",
     (("degree_progress", "measurement", "absolute", "percent", 75,
       "I am 75% done with my degree"),)),
    ("I wake up at 7:30.", (("wake_time", "clock_time", "absolute", "", "07:30",
                             "I wake up at 7:30"),)),
    ("my wake time is 6:15.", (("wake_time", "clock_time", "absolute", "", "06:15",
                                "my wake time is 6:15"),)),
    ("I go to bed at 10:15 PM.", (("bed_time", "clock_time", "absolute", "", "22:15",
                                   "I go to bed at 10:15 PM"),)),
    ("my bedtime is 11:00 pm.", (("bed_time", "clock_time", "absolute", "", "23:00",
                                  "my bedtime is 11:00 pm"),)),
    ("I wake up at 22:15.", (("wake_time", "clock_time", "absolute", "", "22:15",
                              "I wake up at 22:15"),)),
    ("my commute takes 45 minutes.",
     (("commute_duration", "duration", "absolute", "minutes", 45, "my commute takes 45 minutes"),)),
    ("my workout lasts 45 minutes.",
     (("workout_duration", "duration", "absolute", "minutes", 45, "my workout lasts 45 minutes"),)),
    ("my shift is 8 hours.", (("shift_duration", "duration", "absolute", "hours", 8,
                               "my shift is 8 hours"),)),
    ("I sleep 8 hours a night.", (("sleep_duration", "duration", "absolute", "hours", 8,
                                   "I sleep 8 hours a night"),)),
    ("I go to the gym 3 times a week.",
     (("gym_frequency", "frequency", "absolute", "week", 3, "I go to the gym 3 times a week"),)),
    ("I run every other week.", (("run_frequency", "frequency", "absolute", "week", 0.5,
                                  "I run every other week"),)),
    ("I go to the gym every day.",
     (("gym_frequency", "frequency", "absolute", "day", 1, "I go to the gym every day"),)),
    ("I swim every 2 weeks.", (("swim_frequency", "frequency", "absolute", "week", 0.5,
                                "I swim every 2 weeks"),)),
    ("I have between 30 and 40 coins.",
     (("coins", "count", "absolute", "", [30, 40], "I have between 30 and 40 coins"),)),
    ("I weigh between 70 and 75 kg.",
     (("weight", "measurement", "absolute", "kg", [70, 75], "I weigh between 70 and 75 kg"),)),
    ("I added 5 coins to my collection.",
     (("coins", "count", "delta", "", 5, "I added 5 coins to my collection"),)),
    ("I sold 2 coins from my collection.",
     (("coins", "count", "delta", "", -2, "I sold 2 coins from my collection"),)),
    ("I gave away 4 books from my collection.",
     (("books", "count", "delta", "", -4, "I gave away 4 books from my collection"),)),
    ("I used to wake up at 9:00.",
     (("wake_time", "clock_time", "expire", "", "09:00", "I used to wake up at 9:00"),)),
    ("I used to weigh 70 kg.", (("weight", "measurement", "expire", "kg", 70,
                                 "I used to weigh 70 kg"),)),
    ("I used to have 20 coins.", (("coins", "count", "expire", "", 20,
                                   "I used to have 20 coins"),)),
    ("I used to weigh 70 kg, now I weigh 68 kg.",
     (("weight", "measurement", "expire", "kg", 70, "I used to weigh 70 kg"),
      ("weight", "measurement", "absolute", "kg", 68, "now I weigh 68 kg"))),
    ("I used to have 20 coins, now I have 37 coins.",
     (("coins", "count", "expire", "", 20, "I used to have 20 coins"),
      ("coins", "count", "absolute", "", 37, "now I have 37 coins"))),
    ("I now have 37 coins.", (("coins", "count", "absolute", "", 37, "I now have 37 coins"),)),
    ("now I have 37 coins.", (("coins", "count", "absolute", "", 37, "now I have 37 coins"),)),
    ("actually, I weigh 70 kg.",
     (("weight", "measurement", "absolute", "kg", 70, "actually, I weigh 70 kg"),)),
    ("I RUN 3 TIMES A WEEK.",
     (("run_frequency", "frequency", "absolute", "week", 3, "I RUN 3 TIMES A WEEK"),)),
    ("[2026-07-18] I have 37 coins.",
     (("coins", "count", "absolute", "", 37, "I have 37 coins"),)),
    ("I have 37 coins and I have 38 coins.",
     (("coins", "count", "absolute", "", 37, "I have 37 coins"),
      ("coins", "count", "absolute", "", 38, "I have 38 coins"))),
]

_POSITIVE_IDS = [
    "count_have", "two_claims_one_sentence", "contraction", "have_got",
    "thousands_separator", "money_balance_dollar", "money_account_dollar_decimal",
    "money_account_dollars", "measure_weight_verb", "measure_weight_state_decimal",
    "measure_height_verb", "measure_height_state", "percent_completion",
    "clock_wake_at", "clock_wake_state", "clock_bed_pm", "clock_bed_pm_lower",
    "clock_24h_no_meridiem",
    "duration_commute", "duration_workout", "duration_shift", "duration_sleep",
    "frequency_rate", "frequency_every_other", "frequency_every_day", "frequency_every_n",
    "range_between_count", "range_between_weight", "delta_add_accumulator",
    "delta_sold_accumulator", "delta_gave_away_accumulator",
    "expire_wake", "expire_weight", "expire_have", "previous_current_weight",
    "previous_current_coins", "now_mid_verb", "now_leading", "correction_weigh",
    "frequency_rate_uppercase", "date_prefix", "two_values_two_claims",
]


@pytest.mark.parametrize("content,expected", POSITIVE_CASES, ids=_POSITIVE_IDS)
def test_positive_contract(content, expected):
    res = _run(content)
    _assert_proposals(res, content, expected)
    # every fully deterministic episode is reported eligible for router-bypass consideration
    assert res.fully_eligible_episode_uuids == ("ep-1",)
    assert res.episode_receipts[0].fully_covered is True
    assert res.episode_receipts[0].reasons == ()


# ------------------------------------------------------------------------------------------------ #
# Abstention cases: stable reason codes, zero proposals, never router-eligible
# ------------------------------------------------------------------------------------------------ #

ABSTRACTION_CASES = [
    ("my car has 4 wheels.", {"non_self_subject", "unmatched_numeric_cue"}),
    ("I paid $250 for the tickets.", {"unmatched_currency_cue"}),
    ("I spent $30 on groceries.", {"unmatched_currency_cue"}),
    ("I bought 3 books.", {"unmatched_change_verb_cue", "unmatched_numeric_cue"}),
    ("I met my friend at 3:00 pm.", {"unmatched_clock_cue"}),
    ("The meeting starts at 10:15 AM.", {"unmatched_clock_cue"}),
    ("I have about 30 coins.", {"unmatched_numeric_cue"}),
    ("I have 30 or 40 coins.", {"unmatched_numeric_cue"}),
    ("I have a dozen books.", {"unmatched_numeric_cue"}),
    ("my day off is Monday.", {"unmatched_weekday_cue"}),
    ("I used to smoke.", {"unmatched_used_to_cue"}),
    ("my battery is at 75%.", {"non_self_subject", "unmatched_percent_cue"}),
    ("Yesterday I had 20 coins.", {"unmatched_numeric_cue"}),
    ("I weighed 70 kg last year.", {"unmatched_numeric_cue"}),
    ("I run twice a month.", {"unmatched_frequency_cue"}),
    ("I have 37 baseball cards.", {"unmatched_numeric_cue"}),
    ("my weight is 68 pounds.", {"unmatched_numeric_cue"}),
    ("I walk 30 minutes.", {"unmatched_unit_cue", "unmatched_numeric_cue"}),
    # bare change verbs have NO held-slot/accumulator context in the span -> not admissible
    ("I added 5 coins.", {"unmatched_change_verb_cue", "unmatched_numeric_cue"}),
    ("I sold 2 coins.", {"unmatched_change_verb_cue", "unmatched_numeric_cue"}),
    ("I gave away 4 books.", {"unmatched_change_verb_cue", "unmatched_numeric_cue"}),
    # boolean/status are fallback-only: explicit forms must cue, never admit
    ("I am married.", {"unmatched_status_cue"}),
    ("I am employed.", {"unmatched_status_cue"}),
    ("I am retired.", {"unmatched_status_cue"}),
    ("my alarm is false.", {"unmatched_boolean_cue"}),
    ("My car is red.", {"unmatched_status_cue"}),
    ("I have finished reading Dune.", {"unmatched_boolean_cue"}),
    # mixed 12/24h clocks must abstain: a meridiem is only valid on hours 1..12
    ("I wake up at 22:15 PM.", {"template_mismatch"}),
    ("my wake time is 00:15 am.", {"template_mismatch"}),
]

_ABSTRACTION_IDS = [
    "owned_object", "one_off_payment", "one_off_spend", "one_off_purchase",
    "event_clock_time", "event_clock_starts", "hedged_about", "discrete_alternatives",
    "number_word", "weekday_fallback", "bare_used_to",
    "owned_battery_percent", "past_only_yesterday", "past_tense_weight",
    "frequency_word_twice", "unregistered_noun", "unregistered_unit",
    "unmatched_unit_cue", "bare_delta_add", "bare_delta_sold", "bare_delta_gave_away",
    "status_married", "status_employed", "status_retired", "boolean_false",
    "semantic_status", "semantic_boolean",
    "clock_24h_with_meridiem", "clock_zero_hour_meridiem",
]


@pytest.mark.parametrize("content,required_reasons", ABSTRACTION_CASES, ids=_ABSTRACTION_IDS)
def test_abstention_reason_codes(content, required_reasons):
    res = _run(content)
    assert res.proposals == ()
    ep = res.episode_receipts[0]
    assert ep.fully_covered is False
    assert required_reasons <= set(ep.reasons)
    assert "ep-1" not in res.fully_eligible_episode_uuids


PARTIAL_COVERAGE_CASES = [
    # a deterministically parseable claim admits, but an unmatched scalar cue in the same
    # episode makes the episode NOT router-eligible: episode-level routing keeps the LLM
    ("I have 12 books and 5 pens.", 1, {"unmatched_numeric_cue"}),
    ("I wake up at 7:30 every day.", 1, {"unmatched_frequency_cue"}),
    # omitted slot noun: the expire admits, but "now I have 37" is not a complete quote
    # (no slot cue), so it is fallback-only and the episode is not router-eligible
    ("I used to have 20 coins, now I have 37.", 1, {"unmatched_numeric_cue"}),
]


@pytest.mark.parametrize("content,expected_count,required_reasons", PARTIAL_COVERAGE_CASES,
                         ids=["partial_list", "trailing_frequency_cue", "omitted_slot_noun"])
def test_partial_coverage_claims_still_admitted(content, expected_count, required_reasons):
    res = _run(content)
    assert len(res.proposals) == expected_count
    ep = res.episode_receipts[0]
    assert ep.fully_covered is False
    assert required_reasons <= set(ep.reasons)
    assert res.fully_eligible_episode_uuids == ()


# ------------------------------------------------------------------------------------------------ #
# Unconsumed context: a valid-looking span must not make the episode router-eligible while
# semantic context outside the matched span is ignored. The proposal receipt may remain
# admitted (routing is episode-level); the episode must never be eligible.
# ------------------------------------------------------------------------------------------------ #

UNSAFE_CONTEXT_CASES = [
    ("I have 37 coins?",),
    ("If I have 37 coins, I can buy it.",),
    ("Maybe I have 37 coins.",),
    ("I have 37 coins for now.",),
    ("I have 37 coins in a game.",),
]

_UNSAFE_CONTEXT_IDS = ["interrogative", "conditional_clause", "modal_hedge",
                       "bounded_qualifier", "extra_clause"]


@pytest.mark.parametrize("content", UNSAFE_CONTEXT_CASES, ids=_UNSAFE_CONTEXT_IDS)
def test_unconsumed_context_blocks_router_eligibility(content):
    res = _run(content)
    assert len(res.proposals) == 1  # receipt may admit; runtime routing is episode-level
    ep = res.episode_receipts[0]
    assert ep.fully_covered is False
    assert REASON_UNCONSUMED_CONTEXT in ep.reasons
    assert res.reason_counts == ((REASON_UNCONSUMED_CONTEXT, 1),)
    assert res.fully_eligible_episode_uuids == ()


def test_unconsumed_context_allows_connectors_and_terminals():
    # "and" and commas between matched spans are consumed connectors, not residual context
    res = _run("I have 12 books and I wake up at 7:30.")
    assert len(res.proposals) == 2
    assert res.fully_eligible_episode_uuids == ("ep-1",)
    assert res.episode_receipts[0].reasons == ()
    # previous/current comma join is a consumed connector
    res2 = _run("I used to weigh 70 kg, now I weigh 68 kg.")
    assert len(res2.proposals) == 2
    assert res2.fully_eligible_episode_uuids == ("ep-1",)


# ------------------------------------------------------------------------------------------------ #
# Sentence-boundary offsets: exact absolute offsets across multiple sentences and extra
# separator whitespace (regression: a lossy split must not truncate late-sentence ranges)
# ------------------------------------------------------------------------------------------------ #


def test_sentence_offsets_across_multiple_sentences():
    content = "I have 37 coins.  I weigh 70 kg.   I wake up at 7:30."
    res = _run(content)
    assert res.episode_receipts[0].sentence_count == 3
    assert len(res.proposals) == 3
    for prop in res.proposals:
        start = content.index(prop.stated_span)
        end = start + len(prop.stated_span)
        assert (prop.span_start, prop.span_end) == (start, end)
        assert prop.source_key == build_source_key("ep-1", start, end, 0)
    assert res.fully_eligible_episode_uuids == ("ep-1",)


def test_late_sentence_boundary_not_lost():
    # text after the last split boundary must still be scanned; an unsafe trailing clause in
    # the final sentence must block router eligibility (regression for the lossy splitter)
    res = _run("I have 37 coins. I wake up at 7:30?")
    assert len(res.proposals) == 2
    ep = res.episode_receipts[0]
    assert ep.fully_covered is False
    assert REASON_UNCONSUMED_CONTEXT in ep.reasons
    assert res.fully_eligible_episode_uuids == ()


# ------------------------------------------------------------------------------------------------ #
# Receipts, versions, and shared-pipeline drop attribution
# ------------------------------------------------------------------------------------------------ #


def test_receipt_structure_and_versions():
    res = _run("I have 37 coins.")
    assert res.extractor_version == EXTRACTOR_VERSION == "det-v0.1"
    assert res.template_version == TEMPLATE_VERSION == "templates-v0.1"
    assert res.reason_counts == ()
    (ep,) = res.episode_receipts
    assert ep.episode_uuid == "ep-1"
    assert ep.episode_index == 0
    assert ep.sentence_count == 1
    (cand,) = ep.candidate_receipts
    assert cand.template_id == "count_have"
    assert cand.class_id == "c_count"
    assert cand.outcome == OUTCOME_ADMITTED
    assert cand.drop_reason is None
    assert cand.proposal is res.proposals[0]
    assert (cand.source_start, cand.source_end) == (0, len("I have 37 coins"))
    assert cand.stated_span == "I have 37 coins"
    assert {"exact_span", "self_subject", "closed_slot_mapping"} <= set(cand.checks)
    assert res.fully_eligible_episode_uuids == ("ep-1",)


def test_previous_current_class_composition():
    res = _run("I used to weigh 70 kg, now I weigh 68 kg.")
    assert len(res.proposals) == 2
    class_ids = [c.class_id for c in res.episode_receipts[0].candidate_receipts]
    assert class_ids == ["c_expire", "c_prev_current"]


def test_delta_class_ids_and_signs():
    res = _run("I added 5 coins to my collection. I sold 2 coins from my collection.")
    assert [p.value for p in res.proposals] == [5, -2]
    ids = [c.class_id for c in res.episode_receipts[0].candidate_receipts]
    assert ids == ["c_delta_add", "c_delta_sub"]


def test_duplicate_span_ambiguity_abstains_via_shared_pipeline():
    res = _run("I have 37 coins. I have 37 coins.")
    assert res.proposals == ()
    ep = res.episode_receipts[0]
    assert len(ep.candidate_receipts) == 2
    assert all(c.outcome == OUTCOME_DROPPED
               and c.drop_reason == "span_not_uniquely_located"
               for c in ep.candidate_receipts)
    # any candidate drop makes the episode NOT fully covered and NOT router-eligible;
    # the two independent candidates each count one span-not-unique incident
    assert ep.fully_covered is False
    assert ep.reasons == ("span_not_uniquely_located", "span_not_uniquely_located")
    assert res.reason_counts == (("span_not_uniquely_located", 2),)
    assert res.fully_eligible_episode_uuids == ()


def test_template_collision_abstains():
    overlapping = (
        SurfaceTemplate("collide_a", "c_count", "coins", "count", "", "absolute",
                        re.compile(r"\bi\s+have\s+(?P<n>\d+)\s+(?P<noun>coins)\b", re.IGNORECASE),
                        attribute_from=lambda m: m.group("noun")),
        SurfaceTemplate("collide_b", "c_count", "coins", "count", "", "absolute",
                        re.compile(r"\bi\s+have\s+(?P<n>\d+)\s+(?P<noun>coins)\b", re.IGNORECASE),
                        attribute_from=lambda m: m.group("noun")),
    )
    res = _run("I have 5 coins.", templates=overlapping)
    assert res.proposals == ()
    ep = res.episode_receipts[0]
    assert ep.fully_covered is False
    assert REASON_TEMPLATE_COLLISION in ep.reasons
    assert all(c.outcome == OUTCOME_DROPPED and c.drop_reason == REASON_TEMPLATE_COLLISION
               for c in ep.candidate_receipts)
    assert res.reason_counts == ((REASON_TEMPLATE_COLLISION, 1),)


def test_correction_admission_class():
    """A correction admission whose grounded span INCLUDES the correction cue is labeled
    c_correction via the shared span-local classifier (Phase 1 correction coverage)."""
    res = _run("actually, I weigh 70 kg.")
    assert len(res.proposals) == 1
    assert res.proposals[0].value == 70
    (cand,) = res.episode_receipts[0].candidate_receipts
    assert cand.class_id == "c_correction"
    assert cand.stated_span == "actually, I weigh 70 kg"
    assert res.fully_eligible_episode_uuids == ("ep-1",)
    # the correction surface is guarded, so the plain template cannot double-admit the span
    assert len([c for c in res.episode_receipts[0].candidate_receipts
                if c.outcome == OUTCOME_ADMITTED]) == 1
    # and the shared classifier still labels a span containing the cue as correction
    assert classify_absolute_semantics("actually, I weigh 70 kg", "absolute") == "correction"


def test_empty_inputs():
    empty = DeterministicScalarExtractor().extract([])
    assert empty.proposals == ()
    assert empty.episode_receipts == ()
    assert empty.reason_counts == ()
    assert empty.fully_eligible_episode_uuids == ()
    blank = DeterministicScalarExtractor().extract([_Episode("ep-e", "")])
    assert blank.proposals == ()
    assert blank.episode_receipts[0].fully_covered is True
    assert blank.fully_eligible_episode_uuids == ("ep-e",)


# ------------------------------------------------------------------------------------------------ #
# Metamorphic paraphrase / punctuation / whitespace safety
# ------------------------------------------------------------------------------------------------ #

_METAMORPHIC = [
    "I have 37 coins.",
    "I have  37 coins.",
    "i have 37 coins!",
    "I HAVE 37 COINS.",
    "I have 37 coins !",
    "I have 37 coins",
]


@pytest.mark.parametrize("variant", _METAMORPHIC)
def test_metamorphic_semantics_preserved(variant):
    res = _run(variant)
    assert len(res.proposals) == 1
    prop = res.proposals[0]
    assert (prop.attribute, prop.value_kind, prop.operation, prop.unit, prop.value) == (
        "coins", "count", "absolute", "", 37)
    assert prop.subject_text == SELF_SUBJECT_DISPLAY
    # quote is the complete fragment as written, exactly once, offsets inside the content
    start = variant.index(prop.stated_span)
    assert (prop.span_start, prop.span_end) == (start, start + len(prop.stated_span))


# ------------------------------------------------------------------------------------------------ #
# Property-style replay stability / idempotence / fuzz safety (dependency-free)
# ------------------------------------------------------------------------------------------------ #


def test_replay_stability_same_input_equal_output():
    """The same episode set extracted twice (and by a fresh extractor) yields EQUAL complete
    extractions: versions, episode receipts, proposals, eligibility, and reason counts."""
    episodes = [
        _Episode("ep-a", "I have 37 coins. I wake up at 7:30."),
        _Episode("ep-b", "Maybe I have 37 coins."),
        _Episode("ep-c", "I used to weigh 70 kg, now I weigh 68 kg."),
        _Episode("ep-d", ""),
    ]
    first = DeterministicScalarExtractor().extract(episodes)
    second = DeterministicScalarExtractor().extract(episodes)
    third = DeterministicScalarExtractor().extract(list(episodes))
    assert first == second == third


def test_span_replay_preserves_interpretation_and_identity():
    """Re-extracting each admitted positive's complete stated_span in its own episode preserves
    the scalar interpretation fields and produces valid offsets/source identity at 0..len."""
    replayed = 0
    for case_index, (_content, expected) in enumerate(POSITIVE_CASES):
        for span_index, (attribute, value_kind, operation, unit, value, span) in enumerate(expected):
            uuid = f"replay-{case_index}-{span_index}"
            res = DeterministicScalarExtractor().extract([_Episode(uuid, span)])
            assert len(res.proposals) == 1, span
            prop = res.proposals[0]
            assert (prop.attribute, prop.value_kind, prop.operation, prop.unit, prop.value) == (
                attribute, value_kind, operation, unit, value)
            assert prop.stated_span == span
            assert (prop.span_start, prop.span_end) == (0, len(span))
            assert prop.source_key == build_source_key(uuid, 0, len(span), 0)
            assert prop.episode_uuid == uuid
            replayed += 1
    assert replayed == sum(len(expected) for _content, expected in POSITIVE_CASES)


_ADVERSARIAL_FRAGMENTS = [
    "I have 37 coins", "I have 37 coins?", "I have 37 coins !", "I have  37 coins.",
    "maybe I have 37 coins", "I don't have 37 coins", "I have 0 coins", "I have -3 coins",
    "I have 3.5 coins", "I have 3,5 coins", "I have a dozen books", "my car has 4 wheels",
    "my battery is at 75%", "I wake up at 25:99", "I wake up at 7:60", "I wake up at 12:30 AM",
    "I wake up at 22:15 PM", "I wake up at 22:15", "I added 5 coins", "I sold 2 coins",
    "I bought 3 books", "I paid $250", "I added 5 coins to my collection",
    "I sold 2 coins from my collection", "I used to smoke", "I used to have 20 coins",
    "I am married", "I am retired", "my alarm is false", "my day off is Monday",
    "I run twice a month", "I weigh 70 kg?", "if I weigh 70 kg I am happy",
    "I have between 30 and 40 coins", "I have 30 or 40 coins",
    "I have 37 coins and I have 38 coins", "I have 37 coins and 12 books",
    "I have 37 coins until Friday", "I have 37 coins for now", "I have 37 coins in a game",
    "I have 37 coins. I have 37 coins", "now I have 37 coins", "I now have 37 coins",
    "I've got 5 cats", "I have 12 books", "I sleep 8 hours a night", "I go to the gym every day",
    "I run every other week", "my commute takes 45 minutes", "my weight is 68 pounds",
    "I walk 30 minutes", "I met my friend at 3:00 pm", "The meeting starts at 10:15 AM",
    "I used to weigh 70 kg, now I weigh 68 kg", "actually, I weigh 70 kg",
]


def test_adversarial_corpus_never_raises_and_is_idempotent():
    """A closed Cartesian-style adversarial corpus (punctuation/whitespace/digits/modal/
    negation/question fragments) never raises, replays byte-equal, and every proposal span
    lies inside its episode content, exact at its offsets, with matching source identity."""
    episodes = [_Episode(f"adv-{i}", fragment) for i, fragment in enumerate(_ADVERSARIAL_FRAGMENTS)]
    first = DeterministicScalarExtractor().extract(episodes)
    second = DeterministicScalarExtractor().extract(episodes)
    assert first == second
    content_by_uuid = {episode.uuid: episode.content for episode in episodes}
    for prop in first.proposals:
        start, end = prop.span_start, prop.span_end
        content = content_by_uuid[prop.episode_uuid]
        assert 0 <= start <= end <= len(content)
        assert content[start:end] == prop.stated_span
        assert prop.source_key == build_source_key(prop.episode_uuid, start, end, 0)
    assert len(first.proposals) > 0  # the corpus admits its generic positive forms
    assert any(not ep.fully_covered for ep in first.episode_receipts)  # and abstains safely


# ------------------------------------------------------------------------------------------------ #
# Shared-pipeline inheritance: temporal dispositions the extractor routes every row through
# ------------------------------------------------------------------------------------------------ #


def _raw_row(span: str, value=20) -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "coins",
        "scope": "",
        "unit": "",
        "value_kind": "count",
        "operation": "absolute",
        "value": value,
        "when": None,
        "stated_span": span,
        "display": "",
    }


@pytest.mark.parametrize("span,reason", [
    ("Yesterday I had 20 coins", "temporal_past_only"),
    ("I have 37 coins for now", "temporal_bounded_unsupported"),
    ("I will have 100 coins next week", "temporal_future_unresolved"),
], ids=["past_only", "bounded", "future_relative"])
def test_shared_pipeline_temporal_dispositions_drop(span, reason):
    episodes = [_Episode("ep-t", span, reference_time="2026-07-18T00:00:00Z")]
    captured: list[str] = []
    assert parse_scalar_row(_raw_row(span), episodes, captured.append) is None
    assert captured == [reason]
