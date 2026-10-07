"""Cue detector, output parsing, and the frozen prompt / request body of the anchored-time resolver."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.infrastructure.anchored_time import (
    PROMPT_VERSION,
    build_messages,
    detect,
    granularity_of,
    parse_items,
    parse_output,
    strip_user_prefix,
)
from menhir.infrastructure.anchored_time_resolver import build_request

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "anchored_time"


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        ("I moved here on 2023-05-06.", "explicit"),
        ("We met on March 7th at the fair.", "explicit"),
        ("It was the 3rd of June.", "explicit"),
        ("I started the job in October.", "explicit"),
        ("Back in 2015 I lived in Ohio.", "explicit"),
        ("I bought it two weeks ago.", "speech_relative"),
        ("I finished it yesterday.", "speech_relative"),
        ("We are going next weekend.", "speech_relative"),
        ("I saw her this past Sunday.", "speech_relative"),
        ("We went hiking on Saturday.", "speech_relative"),
        ("I booked it a month before the trip.", "event_anchored"),
        ("I have been tired since the surgery.", "event_anchored"),
        ("It happened the day after the wedding.", "event_anchored"),
        ("I've lived here for three years.", "duration"),
        ("I've been swimming for the past six months.", "duration"),
        ("I recently picked up knitting.", "vague"),
        ("I did that a while back.", "vague"),
    ],
)
def test_detect_fires_per_cue_class(text, cue) -> None:
    assert cue in detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "I prefer Postgres for this kind of thing.",
        "Since rent went up, I need a cheaper flat.",  # causal "since"
        "Can you recommend a good book about whales?",
        "My sister plays the violin every Tuesday.",
        "",
        None,
    ],
)
def test_detect_ignores_non_temporal_text(text) -> None:
    assert detect(text) == []


def test_detect_is_recall_first_for_since_with_a_pronoun() -> None:
    # The gate over-fires here on purpose; the prompt's causal-"since" rule returns basis none.
    assert detect("Since I have a big dog, I need a bigger car.") == ["event_anchored"]


def test_strip_user_prefix_removes_only_the_leading_role() -> None:
    assert strip_user_prefix("user: I left yesterday.") == "I left yesterday."
    assert strip_user_prefix("  USER:I left yesterday.") == "I left yesterday."
    assert strip_user_prefix("I told the user: hi") == "I told the user: hi"
    assert strip_user_prefix("assistant: ok") == "assistant: ok"
    assert strip_user_prefix(None) == ""


def test_parse_output_strips_code_fences() -> None:
    assert parse_output('```json\n{"facts": []}\n```') == {"facts": []}
    assert parse_output('  {"facts": [], "missing_events": []} ') == {"facts": [], "missing_events": []}
    with pytest.raises(json.JSONDecodeError):
        parse_output("not json")
    with pytest.raises(json.JSONDecodeError):
        parse_output("")


def test_parse_items_keys_by_index_and_skips_bad_rows() -> None:
    content = json.dumps({
        "facts": [
            {"i": 0, "basis": "none"},
            {"i": "2", "basis": "vague"},
            {"i": None, "basis": "explicit_date"},
            {"i": "x", "basis": "explicit_date"},
            {"basis": "explicit_date"},
            "garbage",
        ],
        "missing_events": [{"event": "a", "expression": "b"}],
    })
    items, missing = parse_items(content)
    assert sorted(items) == [0, 2]
    assert items[2]["basis"] == "vague"
    assert missing == [{"event": "a", "expression": "b"}]


def test_parse_items_tolerates_missing_lists_and_rejects_non_objects() -> None:
    assert parse_items('{"facts": null, "missing_events": "none"}') == ({}, [])
    with pytest.raises(ValueError):
        parse_items("[1, 2]")
    with pytest.raises(ValueError):
        parse_items("{bad json")


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"basis": "explicit_date", "date": "2023"}, "year"),
        ({"basis": "explicit_date", "date": "2023-05"}, "month"),
        ({"basis": "explicit_date", "date": "--05"}, "month"),
        ({"basis": "explicit_date", "date": "2023-05-06"}, "day"),
        ({"basis": "explicit_date", "date": None}, None),
        ({"basis": "speech_relative", "calendar": {"unit": "weekday"}}, "day"),
        ({"basis": "speech_relative", "calendar": {"unit": "weekend"}}, "week"),
        ({"basis": "event_anchored", "offset": {"amount": 2, "unit": "month"}}, "month"),
        ({"basis": "event_anchored", "offset": {"amount": None, "unit": "month"}}, None),
        ({"basis": "vague"}, None),
        (None, None),
    ],
)
def test_granularity(item, expected) -> None:
    assert granularity_of(item) == expected


def test_build_messages_matches_the_frozen_prototype_prompt() -> None:
    golden = json.loads((FIXTURES / "prompt_golden.json").read_text(encoding="utf-8"))
    assert golden["prompt_version"] == PROMPT_VERSION
    assert golden["cases"]
    for case in golden["cases"]:
        assert build_messages(case["turn"], case["speech_date"], case["facts"]) == case["messages"], case["id"]


def test_build_request_is_the_unshaped_p0_body() -> None:
    request = build_request("gpt-6-luna", "I left yesterday.", date(2024, 2, 14), ["The user left."])
    assert request == {
        "model": "gpt-6-luna",
        "messages": build_messages("I left yesterday.", "2024-02-14", ["The user left."]),
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }


class _CapturingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])


@pytest.mark.asyncio
async def test_shaped_luna_request_equals_the_p0_body(monkeypatch) -> None:
    """Through Menhir's provider client the request is exactly what P0 sent to gpt-6-luna."""
    from menhir.infrastructure.graphiti_llm_adapter import _ProviderExtrasAsyncClient

    monkeypatch.delenv("MENHIR_OPENAI_SERVICE_TIER", raising=False)
    completions = _CapturingCompletions()
    inner = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = _ProviderExtrasAsyncClient(inner, "https://api.openai.com/v1")
    request = build_request("gpt-6-luna", "I left yesterday.", date(2024, 2, 14), ["The user left."])
    await client.chat.completions.create(**request)
    p0_body = {
        "messages": build_messages("I left yesterday.", "2024-02-14", ["The user left."]),
        "max_completion_tokens": 3000,
        "response_format": {"type": "json_object"},
    }
    assert completions.calls == [{"model": "gpt-6-luna", **p0_body}]


@pytest.mark.asyncio
async def test_flex_tier_is_added_by_the_provider_client(monkeypatch) -> None:
    from menhir.infrastructure.graphiti_llm_adapter import _ProviderExtrasAsyncClient

    monkeypatch.setenv("MENHIR_OPENAI_SERVICE_TIER", "flex")
    completions = _CapturingCompletions()
    inner = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = _ProviderExtrasAsyncClient(inner, "https://api.openai.com/v1")
    await client.chat.completions.create(
        **build_request("gpt-6-luna", "I left yesterday.", date(2024, 2, 14), ["The user left."])
    )
    (call,) = completions.calls
    assert call["service_tier"] == "flex"
    assert call["max_completion_tokens"] == 3000
    assert "max_tokens" not in call and "temperature" not in call
