"""Typed-scalar perception (ScalarStateView Piece C.4, commit 1) — prompt-free, offline.

A fake `llm_complete` returns a canned JSON array so the parser is exercised deterministically with
no live model. Proves the precision-first contract: required fields must be real non-blank strings
(no semantic defaults), `operation` must be explicitly present, `episode` a real in-range integer,
`attribute` canonical snake_case, the value well-typed, any `when` a parseable ISO date, and
`stated_span` UNIQUELY located in the episode text (zero/multiple -> dropped). Source identity is
order-independent (located offsets, ordinal always 0). No writes, no gate."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_perception import (
    TYPED_SCALAR_SYSTEM_PROMPT,
    TypedScalarProposal,
    extract_typed_scalars_once,
)


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows) -> "object":
    """A fake LlmComplete returning `rows` as a JSON array; records the system prompt it was given."""
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps(rows)

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def _row(**over):
    base = dict(
        episode=0, subject="user", attribute="wake_time", scope="work_days",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        when="2026-07-01", stated_span="I wake at 7:30 on work days",
    )
    base.update(over)
    return base


_EPISODES = [_Ep(uuid="ep-1", content="On work days I wake at 7:30 on work days, and my car is red.")]


@pytest.mark.unit
def test_empty_episodes_short_circuits():
    assert extract_typed_scalars_once([], _llm([_row()])) == []


@pytest.mark.unit
def test_uses_the_typed_scalar_prompt():
    llm = _llm([_row()])
    extract_typed_scalars_once(_EPISODES, llm)
    assert llm.calls and llm.calls[0][0] == TYPED_SCALAR_SYSTEM_PROMPT   # its own prompt, not the counter one
    assert "[0]" in llm.calls[0][1]                                       # episodes numbered in the log


@pytest.mark.unit
def test_prompt_routes_colon_values_by_context_and_keeps_current_personal_best_absolute():
    """The M:SS shape is not enough: semantics select clock_time vs duration and operation."""
    prompt = TYPED_SCALAR_SYSTEM_PROMPT
    assert "punctuation alone NEVER determines the kind" in prompt
    assert "'meet at 8:30'" in prompt
    assert "'finished in 25:50'" in prompt
    assert "A bare ambiguous colon value" in prompt
    assert "'I'm hoping to beat my personal best time of 25:50'" in prompt
    assert "The hope does NOT make the existing record future, expired, or a delta" in prompt
    assert "current at THAT EPISODE'S source time" in prompt
    assert "emit BOTH absolute source-time observations" in prompt


@pytest.mark.unit
def test_prompt_requires_per_episode_completeness_and_repeatable_grounding():
    """Large batches must not hide later observations or scatter votes across quote boundaries."""
    prompt = TYPED_SCALAR_SYSTEM_PROMPT
    assert "inspect EACH numbered episode independently and make a row checklist" in prompt
    assert "do not let a later episode erase an earlier source-time observation" in prompt
    assert "Make stated_span repeatable" in prompt
    assert "personal best time in a charity 5K run with a time of 27:12" in prompt
    assert "personal best time of 25:50" in prompt
    assert "EXACTLY ONE envelope for EVERY numbered episode" in prompt
    assert "Use an empty observations array" in prompt
    assert "EVERY observation object MUST contain all of" in prompt
    assert "Copy stated_span verbatim from the numbered episode" in prompt
    assert "Emit each source fact only once" in prompt


@pytest.mark.unit
def test_prompt_keeps_time_out_of_slot_names_and_normalizes_interval_rates():
    prompt = TYPED_SCALAR_SYSTEM_PROMPT
    assert "Source-time versions belong in provenance/history, NOT the family name" in prompt
    assert "never 'previous_account_balance' or 'current_account_balance'" in prompt
    assert "'every other week' -> value 0.5" in prompt
    assert "'twice every three months' -> value 2/3" in prompt
    assert "'I've completed 12 lessons so far' -> count 12" in prompt
    assert "'we have closed 40 tickets to date' -> count 40" in prompt


@pytest.mark.unit
def test_well_typed_proposal_parses_and_grounds():
    out = extract_typed_scalars_once(_EPISODES, _llm([_row()]))
    assert len(out) == 1
    p = out[0]
    assert isinstance(p, TypedScalarProposal)
    assert p.attribute == "wake_time" and p.value_kind == "clock_time" and p.value == "07:30"
    assert p.episode_uuid == "ep-1" and p.operation == "absolute"
    assert p.slot_key == ("wake_time", "work_days", "clock_time", "")
    assert p.normalized_value == "07:30"
    # when-discipline (v2): the model gave a date ("2026-07-01") but the span "I wake at 7:30 on work
    # days" carries NO explicit calendar anchor, so the unsupported date is neutralized (a hallucinated
    # date must never reach valid_at) -> None; bind falls to the episode reference.
    assert p.when is None
    # the quote is uniquely located -> offsets index the real substring; ordinal is always 0.
    assert p.span_start >= 0 and p.span_end == p.span_start + len("I wake at 7:30 on work days")
    assert _EPISODES[0].content[p.span_start:p.span_end].lower() == "i wake at 7:30 on work days"
    assert p.claim_ordinal == 0


@pytest.mark.unit
def test_episode_envelopes_flatten_and_preserve_explicit_empty_coverage():
    episodes = [
        _Ep(uuid="ep-0", content="Nothing scalar here."),
        _Ep(uuid="ep-1", content="I have 8 books."),
    ]
    rows = [
        {"episode": 0, "observations": []},
        {
            "episode": 1,
            "observations": [
                {
                    "subject": "user",
                    "attribute": "book_count",
                    "scope": "",
                    "value_kind": "count",
                    "unit": "",
                    "operation": "absolute",
                    "value": 8,
                    "when": "",
                    "stated_span": "I have 8 books",
                }
            ],
        },
    ]
    out = extract_typed_scalars_once(episodes, _llm(rows))
    assert len(out) == 1
    assert out[0].episode_uuid == "ep-1"
    assert out[0].attribute == "book_count"
    assert out[0].value == 8


@pytest.mark.unit
def test_single_episode_bare_envelope_is_accepted_but_bare_observation_is_not():
    """A model may omit only the redundant outer array, never the envelope contract itself."""
    episode = [_Ep(uuid="ep-0", content="I've completed 30 videos so far.")]
    observation = {
        "subject": "user",
        "attribute": "completed_videos",
        "value_kind": "count",
        "operation": "absolute",
        "value": 30,
        "stated_span": "I've completed 30 videos so far",
    }

    out = extract_typed_scalars_once(
        episode,
        _llm({"episode": 0, "observations": [observation]}),
    )
    assert len(out) == 1
    assert out[0].attribute == "completed_videos"
    assert out[0].value == 30

    drops: list[str] = []
    assert extract_typed_scalars_once(
        episode,
        _llm({"episode": 0, **observation}),
        on_drop=drops.append,
    ) == []
    assert drops == ["not_a_json_array"]


@pytest.mark.unit
def test_episode_envelope_coverage_defects_are_audited():
    episodes = [
        _Ep(uuid="ep-0", content="I have 3 pens."),
        _Ep(uuid="ep-1", content="I have 8 books."),
    ]
    rows = [
        {"episode": 0, "observations": []},
        {"episode": 0, "observations": [_row(episode=0, value=99)]},
    ]
    drops: list[str] = []
    assert extract_typed_scalars_once(episodes, _llm(rows), on_drop=drops.append) == []
    assert drops == ["duplicate_episode_envelope", "missing_episode_envelope"]


_NINE_CONTENT = (
    "I am married. My car is red. I have 37 coins. My commute is 25 to 35 minutes. "
    "I go to the gym 3 times a week. My rent is $1200. I weigh 80 kg. I wake at 07:30. "
    "My gym day is Saturday."
)


@pytest.mark.unit
def test_each_of_the_nine_kinds_round_trips_with_real_grounding():
    ep = [_Ep(uuid="ep-x", content=_NINE_CONTENT)]
    rows = [
        _row(attribute="married", value_kind="boolean", value=True, stated_span="I am married"),
        _row(attribute="car_color", value_kind="status", value="red", stated_span="my car is red"),
        _row(attribute="coins", value_kind="count", value=37, stated_span="I have 37 coins"),
        _row(attribute="commute", value_kind="duration", unit="minutes", value=[25, 35],
             stated_span="commute is 25 to 35 minutes"),
        _row(attribute="gym", value_kind="frequency", unit="per_week", value=3,
             stated_span="gym 3 times a week"),
        _row(attribute="rent", value_kind="money", unit="usd", value=1200, stated_span="rent is $1200"),
        _row(attribute="weight", value_kind="measurement", unit="kg", value=80, stated_span="I weigh 80 kg"),
        _row(attribute="wake", value_kind="clock_time", value="07:30", stated_span="wake at 07:30"),
        _row(attribute="gym_day", value_kind="weekday", value="Saturday", stated_span="gym day is Saturday"),
    ]
    out = extract_typed_scalars_once(ep, _llm(rows))
    assert len(out) == 9                                                  # every quote genuinely located
    assert {p.value_kind for p in out} == {
        "boolean", "status", "count", "duration", "frequency",
        "money", "measurement", "clock_time", "weekday"}
    for p in out:                                                         # each really grounded in source
        assert ep[0].content[p.span_start:p.span_end].lower() == p.stated_span.lower()
    dur = next(p for p in out if p.value_kind == "duration")
    assert dur.value == [1500, 2100] and dur.unit == "seconds"


@pytest.mark.unit
def test_value_coercion_rescues_common_near_misses():
    ep = [_Ep(uuid="e", content="my rent is $1200 and I am married")]
    rows = [
        _row(attribute="rent", value_kind="money", unit="usd", value="$1,200", stated_span="rent is $1200"),
        _row(attribute="married", value_kind="boolean", value="true", stated_span="I am married"),
    ]
    out = extract_typed_scalars_once(ep, _llm(rows))
    money = next(p for p in out if p.value_kind == "money")
    boolean = next(p for p in out if p.value_kind == "boolean")
    assert money.value == 1200 and boolean.value is True


@pytest.mark.unit
def test_elapsed_mm_ss_durations_normalize_to_seconds_with_source_grounding():
    """LME 6a1eabeb regression: race durations are not wall-clock times or invalid strings."""
    episodes = [
        _Ep(
            uuid="ep-old",
            content="I set a personal best in a charity 5K run with a time of 27:12.",
        ),
        _Ep(
            uuid="ep-new",
            content="I'm hoping to beat my personal best time of 25:50 this time around.",
        ),
    ]
    rows = [
        _row(
            episode=0,
            attribute="personal_best_5k_time",
            value_kind="duration",
            unit="minutes",
            value="27:12",
            stated_span="personal best in a charity 5K run with a time of 27:12",
        ),
        _row(
            episode=1,
            attribute="personal_best_5k_time",
            value_kind="duration",
            unit="",
            value="25:50",
            stated_span="personal best time of 25:50",
        ),
    ]

    out = extract_typed_scalars_once(episodes, _llm(rows))

    assert [(p.value, p.unit) for p in out] == [(1632, "seconds"), (1550, "seconds")]
    assert [p.episode_uuid for p in out] == ["ep-old", "ep-new"]
    for proposal, episode in zip(out, episodes, strict=True):
        assert (
            episode.content[proposal.span_start:proposal.span_end].lower()
            == proposal.stated_span.lower()
        )


@pytest.mark.unit
def test_same_colon_shape_can_be_clock_time_or_duration_after_semantic_routing():
    episode = _Ep(
        uuid="colon-context",
        content="Meet me at 8:30 after I finish the race in 25:50.",
    )
    rows = [
        _row(
            attribute="meeting_time",
            value_kind="clock_time",
            value="8:30",
            stated_span="Meet me at 8:30",
        ),
        _row(
            attribute="race_time",
            value_kind="duration",
            unit="",
            value="25:50",
            stated_span="finish the race in 25:50",
        ),
    ]

    clock_time, duration = extract_typed_scalars_once([episode], _llm(rows))

    assert (clock_time.value_kind, clock_time.value, clock_time.unit) == (
        "clock_time", "08:30", "",
    )
    assert (duration.value_kind, duration.value, duration.unit) == (
        "duration", 1550, "seconds",
    )


@pytest.mark.unit
@pytest.mark.parametrize("value", ["25:60", "1:60:00", "25:5", "-1:30"])
def test_malformed_colon_durations_still_fail_closed(value):
    ep = [_Ep(uuid="e", content=f"my elapsed time was {value}")]
    row = _row(
        attribute="elapsed_time",
        value_kind="duration",
        unit="seconds",
        value=value,
        stated_span=f"elapsed time was {value}",
    )
    assert extract_typed_scalars_once(ep, _llm([row])) == []


@pytest.mark.unit
def test_colon_duration_normalization_handles_long_fractional_range_and_delta_values():
    ep = [_Ep(
        uuid="duration-edges",
        content=(
            "the task took 1:02:03; my race time was 25:50.5; the usual range is "
            "25:50 to 27:12; and my time changed by -1:22"
        ),
    )]
    rows = [
        _row(
            attribute="task_duration",
            value_kind="duration",
            unit="hours",
            value="1:02:03",
            stated_span="task took 1:02:03",
        ),
        _row(
            attribute="race_time",
            value_kind="duration",
            unit="minutes",
            value="25:50.5",
            stated_span="race time was 25:50.5",
        ),
        _row(
            attribute="usual_race_time",
            value_kind="duration",
            unit="minutes",
            value=["25:50", "27:12"],
            stated_span="usual range is 25:50 to 27:12",
        ),
        _row(
            attribute="race_time",
            value_kind="duration",
            unit="minutes",
            operation="delta",
            value="-1:22",
            stated_span="time changed by -1:22",
        ),
    ]

    out = extract_typed_scalars_once(ep, _llm(rows))

    assert [(p.value, p.unit, p.operation) for p in out] == [
        (3723, "seconds", "absolute"),
        (1550.5, "seconds", "absolute"),
        ([1550, 1632], "seconds", "absolute"),
        (-82, "seconds", "delta"),
    ]


@pytest.mark.unit
def test_mixed_colon_and_numeric_duration_range_does_not_change_units():
    ep = [_Ep(uuid="e", content="the usual range is 25:50 to 27 minutes")]
    row = _row(
        attribute="usual_race_time",
        value_kind="duration",
        unit="minutes",
        value=["25:50", 27],
        stated_span="usual range is 25:50 to 27 minutes",
    )
    assert extract_typed_scalars_once(ep, _llm([row])) == []


# ----------------------------------------------------------------------- fail-closed required fields

@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"subject": None},                                # missing subject (no 'user' default)
    {"operation": None},                              # missing operation (no 'absolute' default)
    {"subject": ["user"]},                            # structured (list) subject
    {"attribute": {"name": "x"}},                     # structured (object) attribute
    {"stated_span": 123},                             # numeric stated_span (non-string)
    {"operation": "multiply"},                        # not an operation
    {"attribute": "Wake Time"},                       # not canonical snake_case
    {"attribute": ""},                                # blank attribute
    {"stated_span": ""},                              # ungrounded (blank)
    {"value_kind": "bogus"},                          # not a ValueKind
    {"value_kind": "boolean", "value": 1},            # int is not a bool
    {"value_kind": "clock_time", "value": "not-a-time"},  # bad clock_time
    {"value_kind": "status", "operation": "delta", "value": 3},  # delta on non-numeric kind
    {"episode": 9},                                   # out of range
    {"episode": "0"},                                 # numeric string, not a real int
    {"episode": True},                                # bool is not a valid index
    {"episode": 0.5},                                 # fractional is not a valid index
    {"scope": ["work"]},                              # optional field present-but-non-string
    {"when": "not-a-date"},                           # supplied-but-unparseable date
])
def test_fail_closed_drops_malformed_rows(bad):
    # each bad row is dropped; a valid row alongside it still survives (drop is per-row, not per-batch).
    good = _row(attribute="coins", value_kind="count", value=5, stated_span="5 coins")
    ep = [_Ep(uuid="e", content="I have 5 coins")]
    out = extract_typed_scalars_once(ep, _llm([_row(**bad), good]))
    assert [p.attribute for p in out] == ["coins"]


@pytest.mark.unit
def test_missing_episode_uuid_is_dropped():
    ep = [_Ep(uuid="", content="I wake at 7:30 on work days")]   # quote present, but no provenance uuid
    assert extract_typed_scalars_once(ep, _llm([_row()])) == []


# ----------------------------------------------------------------------------- grounding uniqueness

@pytest.mark.unit
def test_unlocatable_quote_is_dropped():
    ep = [_Ep(uuid="e", content="totally unrelated text")]
    assert extract_typed_scalars_once(ep, _llm([_row(stated_span="I wake at 7:30")])) == []


@pytest.mark.unit
def test_ambiguous_multi_match_quote_is_dropped():
    # the quote occurs twice -> which occurrence is meant is ambiguous -> drop (precision-first).
    ep = [_Ep(uuid="e", content="my car is red. my car is red again.")]
    out = extract_typed_scalars_once(ep, _llm([
        _row(attribute="car_color", value_kind="status", value="red", stated_span="my car is red")]))
    assert out == []


# --------------------------------------------------------------------- order-independent identity

@pytest.mark.unit
def test_row_order_does_not_change_source_identity():
    # reversing model output must NOT change any claim's durable source_key (identity is the located
    # span + episode, not output order): a retry / different k-sample cannot fork a duplicate claim.
    ep = [_Ep(uuid="e1", content="I have 37 coins and I wake at 07:30.")]
    r_coins = _row(attribute="coins", value_kind="count", value=37, stated_span="37 coins")
    r_wake = _row(attribute="wake", value_kind="clock_time", value="07:30", stated_span="wake at 07:30")
    fwd = {p.attribute: p.source_key for p in extract_typed_scalars_once(ep, _llm([r_coins, r_wake]))}
    rev = {p.attribute: p.source_key for p in extract_typed_scalars_once(ep, _llm([r_wake, r_coins]))}
    assert fwd == rev and len(fwd) == 2


@pytest.mark.unit
def test_same_span_two_interpretations_share_source_key():
    # two proposals citing the SAME source span are competing interpretations of ONE claim -> they
    # share a source_key (the head will keep one current), never separate order-numbered claims.
    ep = [_Ep(uuid="e", content="I wake at 07:30")]
    out = extract_typed_scalars_once(ep, _llm([
        _row(attribute="wake", value_kind="clock_time", value="07:30", stated_span="wake at 07:30"),
        _row(attribute="alarm", value_kind="clock_time", value="07:30", stated_span="wake at 07:30"),
    ]))
    assert len(out) == 2
    assert out[0].source_key == out[1].source_key
    assert all(p.claim_ordinal == 0 for p in out)


# -------------------------------------------------------------------------------- world-time typing

@pytest.mark.unit
def test_absent_when_is_none_and_valid_when_is_normalized():
    ep = [_Ep(uuid="e", content="I have 5 coins")]
    row_no_when = _row(attribute="coins", value_kind="count", value=5, stated_span="5 coins")
    row_no_when.pop("when")
    out = extract_typed_scalars_once(ep, _llm([row_no_when]))
    assert len(out) == 1 and out[0].when is None                          # absent -> explicitly unresolved

    # A slash model date is normalized to ISO AND cross-checked against a FULLY-EXPLICIT source date
    # ("on 2026-03-04"); since they agree, the SOURCE-derived timestamp is persisted (the model date is
    # never the persisted value -- when-discipline v2 P1). Episode reference is None here -> UTC.
    ep2 = [_Ep(uuid="e", content="On 2026-03-04 I had 5 coins")]
    dated = _row(attribute="coins", value_kind="count", value=5,
                 stated_span="On 2026-03-04 I had 5 coins", when="2026/03/04")
    out2 = extract_typed_scalars_once(ep2, _llm([dated]))
    assert out2[0].when == "2026-03-04T00:00:00+00:00"                    # source-derived timestamp (UTC)


# -------------------------------------------------------------- non-finite / impossible values

@pytest.mark.unit
@pytest.mark.parametrize("value_kind,value", [
    ("count", float("nan")),
    ("count", float("inf")),
    ("count", float("-inf")),
    ("measurement", [1, float("inf")]),   # a non-finite range endpoint
    ("clock_time", "24:00"),              # shape ok, hour out of range
    ("clock_time", "12:60"),              # shape ok, minute out of range
])
def test_non_finite_and_impossible_values_are_dropped(value_kind, value):
    # the bad row is otherwise fully grounded, so the ONLY reason it drops is the value validator.
    ep = [_Ep(uuid="e", content="the measured value is here")]
    row = _row(attribute="m", value_kind=value_kind, unit="kg", value=value, stated_span="measured value")
    assert extract_typed_scalars_once(ep, _llm([row])) == []


@pytest.mark.unit
def test_oversized_integer_is_dropped_without_aborting_the_pass():
    # an int too large to represent as a float would raise OverflowError inside math.isfinite; the
    # boundary must DROP that one row (not crash), and a valid sibling in the SAME response survives.
    ep = [_Ep(uuid="e", content="I have huge coins and I have 5 coins")]
    huge = _row(attribute="coins", value_kind="count", value=10 ** 1000, stated_span="huge coins")
    good = _row(attribute="coins", value_kind="count", value=5, stated_span="5 coins")
    out = extract_typed_scalars_once(ep, _llm([huge, good]))
    assert [p.value for p in out] == [5]


@pytest.mark.unit
def test_clock_time_is_canonicalized_to_zero_padded_hh_mm():
    # "7:30" and "07:30" are the same time; C.4.1 zero-pads so they don't scatter the C.4.2 vote.
    ep = [_Ep(uuid="e", content="I wake at 7:30 and my alarm is 07:30")]
    rows = [
        _row(attribute="wake", value_kind="clock_time", value="7:30", stated_span="wake at 7:30"),
        _row(attribute="alarm", value_kind="clock_time", value="07:30", stated_span="alarm is 07:30"),
    ]
    out = extract_typed_scalars_once(ep, _llm(rows))
    assert len(out) == 2
    assert {p.value for p in out} == {"07:30"}                    # both canonical
    assert {p.normalized_value for p in out} == {"07:30"}          # same vote key


# --------------------------------------------------------------------- strict world-time parsing

@pytest.mark.unit
@pytest.mark.parametrize("when", [
    20260701,                     # numeric date (non-string) -> not a valid explicit time
    "2026-07-01 invalid suffix",  # valid prefix + garbage: no truncated-prefix rescue
    "2026-07-01T29:99",           # impossible time appended to a valid date
    "not-a-date",
])
def test_malformed_when_is_dropped(when):
    ep = [_Ep(uuid="e", content="I have 5 coins")]
    row = _row(attribute="coins", value_kind="count", value=5, stated_span="5 coins", when=when)
    assert extract_typed_scalars_once(ep, _llm([row])) == []


# ---------------------------------------------------------------- scope/unit canonicalization

@pytest.mark.unit
def test_scope_and_unit_collapse_spacing_to_one_slot():
    ep = [_Ep(uuid="e", content="I work 8 hours and also 8 hrs on work days")]
    rows = [
        _row(attribute="hours", value_kind="count", unit="per week", scope="work days",
             value=8, stated_span="I work 8 hours"),
        _row(attribute="hours", value_kind="count", unit="per-week", scope="work_days",
             value=8, stated_span="8 hrs"),
    ]
    out = extract_typed_scalars_once(ep, _llm(rows))
    assert len(out) == 2
    # Count is intrinsically unitless; scope spelling still canonicalizes before slot identity.
    assert all(p.scope == "work_days" and p.unit == "" for p in out)
    assert out[0].slot_key == out[1].slot_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "model_value", "model_unit", "expected_value", "expected_unit"),
    [
        ("I practice every other week", 1, "week", 0.5, "week"),
        ("I call home every three days", 1, "day", pytest.approx(1 / 3), "day"),
        ("I volunteer twice every three months", 2, "month", pytest.approx(2 / 3), "month"),
        ("I train 3 times every 2 weeks", 3, "weeks", 1.5, "week"),
        ("I review the plan every week", 7, "weeks", 1, "week"),
    ],
)
def test_exact_interval_frequency_is_normalized_from_source(
    text, model_value, model_unit, expected_value, expected_unit,
):
    ep = [_Ep(uuid="e", content=text)]
    row = _row(
        attribute="practice_frequency",
        value_kind="frequency",
        value=model_value,
        unit=model_unit,
        stated_span=text,
        when="",
    )
    out = extract_typed_scalars_once(ep, _llm([row]))
    assert len(out) == 1
    assert out[0].value == expected_value
    assert out[0].unit == expected_unit


@pytest.mark.unit
def test_interval_frequency_normalizer_does_not_touch_clock_time():
    text = "I meet every other week at 18:00"
    ep = [_Ep(uuid="e", content=text)]
    row = _row(
        attribute="meeting_time",
        value_kind="clock_time",
        value="18:00",
        unit="",
        stated_span=text,
        when="",
    )
    out = extract_typed_scalars_once(ep, _llm([row]))
    assert len(out) == 1
    assert out[0].value == "18:00"
    assert out[0].unit == ""


@pytest.mark.unit
def test_approximate_interval_frequency_still_abstains():
    text = "I practice about every other week"
    ep = [_Ep(uuid="e", content=text)]
    row = _row(
        attribute="practice_frequency",
        value_kind="frequency",
        value=1,
        unit="week",
        stated_span=text,
        when="",
    )
    assert extract_typed_scalars_once(ep, _llm([row])) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attribute", "span", "expected"),
    [
        ("previous_account_balance", "account balance is $900", "account_balance"),
        ("prior_home_value", "home value is $300000", "home_value"),
        ("current_body_weight", "body weight is 72 kg", "body_weight"),
        ("latest_team_size", "team size is 8", "team_size"),
    ],
)
def test_ungrounded_temporal_attribute_prefix_does_not_fork_slot(attribute, span, expected):
    ep = [_Ep(uuid="e", content=span)]
    row = _row(
        attribute=attribute,
        value_kind="count",
        value=8,
        unit="",
        stated_span=span,
        when="",
    )
    out = extract_typed_scalars_once(ep, _llm([row]))
    assert len(out) == 1
    assert out[0].attribute == expected


@pytest.mark.unit
def test_grounded_temporal_attribute_prefix_is_preserved():
    text = "previous employer count is 4"
    ep = [_Ep(uuid="e", content=text)]
    row = _row(
        attribute="previous_employer_count",
        value_kind="count",
        value=4,
        unit="",
        stated_span=text,
        when="",
    )
    out = extract_typed_scalars_once(ep, _llm([row]))
    assert len(out) == 1
    assert out[0].attribute == "previous_employer_count"


@pytest.mark.unit
def test_tolerant_json_parse_of_fenced_and_plus_numbers():
    ep = [_Ep(uuid="e", content="I sold 2 coins")]
    fenced = "```json\n" + json.dumps([_row(
        attribute="coins", value_kind="count", operation="delta", value=-2, stated_span="sold 2 coins")]
    ) + "\n```"

    def complete(system: str, user: str) -> str:
        return fenced

    out = extract_typed_scalars_once(ep, complete)
    assert len(out) == 1 and out[0].operation == "delta" and out[0].value == -2
