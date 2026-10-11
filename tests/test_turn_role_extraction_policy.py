"""Extraction-policy role handling comes from the receipt's structured `turn_role`, never from a
text prefix (issue #271).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.graphiti_extraction_policy import (  # noqa: E402
    CombinedExtractionReceipt,
    _is_unresolved_self_like_endpoint,
    _sanitize_combined_payload,
    parse_titled_list,
)

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 2, 14, 18, 30, tzinfo=timezone.utc)
# "last month" is a cue the existing anchored-time suite already relies on (detect() passes).
PLAIN_TURN = "I moved to Lisbon last month and I still love hiking."
PREFIXED_TURN = "user: I moved to Lisbon last month and I still love hiking."


# ---- receipt ------------------------------------------------------------------------------------


def test_receipt_turn_role_defaults_to_unknown() -> None:
    assert CombinedExtractionReceipt(episode_key="e", episode_text="x").turn_role == "unknown"
    receipt = policy.begin_extraction_receipt("e", "x")
    try:
        assert receipt.turn_role == "unknown"
    finally:
        policy.clear_extraction_receipt()


def test_begin_extraction_receipt_stores_the_passed_turn_role() -> None:
    receipt = policy.begin_extraction_receipt("e", "x", turn_role="assistant")
    try:
        assert receipt.turn_role == "assistant"
    finally:
        policy.clear_extraction_receipt()


def test_receipt_turn_role_helper_is_unknown_without_a_receipt() -> None:
    assert policy._receipt_turn_role(None) == "unknown"
    assert policy._receipt_turn_role(
        CombinedExtractionReceipt(episode_key="e", episode_text="x", turn_role="tool")
    ) == "tool"


def test_the_text_prefix_role_parser_is_gone() -> None:
    assert not hasattr(policy, "_episode_role")


# ---- anchored time gate -------------------------------------------------------------------------


class _RecordingResolver:
    """Resolver that records what it is given and fails closed (no overlay needed)."""

    model = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def resolve(self, turn, speech_date, facts):
        self.calls.append((turn, speech_date, list(facts)))
        return SimpleNamespace(status="error", error_class="Fake", items=None, latency_s=0.0, cached=False)


def _edge(fact: str, uuid: str) -> SimpleNamespace:
    return SimpleNamespace(uuid=uuid, fact=fact, valid_at=SPEECH, invalid_at=None)


async def _apply(text: str, turn_role: str):
    resolver = _RecordingResolver()
    receipt = policy.begin_extraction_receipt("ep-1", text, turn_role=turn_role)
    try:
        await policy._apply_anchored_time(
            resolver, receipt, SimpleNamespace(uuid="g-1", valid_at=SPEECH),
            [_edge("The user moved to Lisbon.", "e0")],
        )
        return resolver, receipt.anchored_time
    finally:
        policy.clear_extraction_receipt()


@pytest.mark.asyncio
async def test_unprefixed_text_with_user_role_reaches_the_resolver_unchanged() -> None:
    resolver, report = await _apply(PLAIN_TURN, "user")
    assert report.reason != "not_user_turn"
    assert len(resolver.calls) == 1
    assert resolver.calls[0][0] == PLAIN_TURN


@pytest.mark.asyncio
async def test_prefixed_text_with_user_role_is_passed_through_unstripped() -> None:
    resolver, report = await _apply(PREFIXED_TURN, "user")
    assert report.reason != "not_user_turn"
    assert resolver.calls[0][0] == PREFIXED_TURN


@pytest.mark.asyncio
async def test_user_prefix_with_unknown_role_is_skipped_and_resolver_not_called() -> None:
    resolver, report = await _apply(PREFIXED_TURN, "unknown")
    assert (report.status, report.reason) == ("skipped", "not_user_turn")
    assert resolver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assistant", "tool", "agent", "system", "unknown"])
async def test_only_a_user_role_passes_the_user_turn_gate(role) -> None:
    resolver, report = await _apply(PLAIN_TURN, role)
    assert (report.status, report.reason) == ("skipped", "not_user_turn")
    assert resolver.calls == []


# ---- self-like endpoint -------------------------------------------------------------------------


def test_assistant_turn_never_treats_user_as_an_unresolved_self_endpoint() -> None:
    assert _is_unresolved_self_like_endpoint("user", "assistant") is False


@pytest.mark.parametrize(("name", "role"), [("user", "user"), ("i", "unknown"), ("user", "unknown")])
def test_non_assistant_turns_keep_self_like_endpoint_recovery(name, role) -> None:
    assert _is_unresolved_self_like_endpoint(name, role) is True


# ---- titled list --------------------------------------------------------------------------------


def test_titled_list_without_a_prefix_parses() -> None:
    assert parse_titled_list("agents names below:\nAdmon\nBeryl\nCoyle") == (
        "agents names below", ["Admon", "Beryl", "Coyle"],
    )


def test_role_like_prefix_is_ordinary_text_not_stripped() -> None:
    # Not stripped: the stripped reading would parse as ("agents names below", [...]).
    ordinary = parse_titled_list("label: agents names below:\nAdmon\nBeryl\nCoyle")
    assert ordinary is not None and ordinary[0] == "label"
    assert parse_titled_list("user: agents names below:\nAdmon\nBeryl\nCoyle") is None


@pytest.mark.parametrize("label", ["user", "User", "assistant", "the user", "me", "system"])
def test_speaker_or_self_label_is_never_a_list_container(label) -> None:
    # Prefixed text a caller still sends must not mint MEMBER_OF edges into the self entity.
    assert parse_titled_list(f"{label}: agents\nAdmon\nBeryl\nCoyle") is None
    assert parse_titled_list(f"{label}:\nAdmon\nBeryl\nCoyle") is None


# ---- assistant policy in _sanitize_combined_payload ----------------------------------------------


def _self_label_payload() -> dict:
    return {"extracted_entities": [{"name": "user", "entity_type_id": -1}], "edges": []}


def _echo_payload() -> dict:
    return {
        "extracted_entities": [
            {"name": "user", "entity_type_id": -1},
            {"name": "web scraper", "entity_type_id": -1},
        ],
        "edges": [{
            "relation_type": "WANTS_TO_BUILD", "source_entity_name": "user",
            "target_entity_name": "web scraper", "fact": "user wants to build a web scraper",
            "episode_indices": [0],
        }],
    }


def test_assistant_self_label_payload_is_an_assistant_turn_by_receipt_role() -> None:
    text = "You mentioned you like tea."  # no prefix at all
    receipt = CombinedExtractionReceipt(episode_key="e", episode_text=text, turn_role="assistant")
    _sanitize_combined_payload(_self_label_payload(), receipt, text)
    assert receipt.assistant_self_only_relationless is True


def test_assistant_prefix_text_with_user_role_is_not_an_assistant_turn() -> None:
    text = "assistant: You mentioned you like tea."
    receipt = CombinedExtractionReceipt(episode_key="e", episode_text=text, turn_role="user")
    _sanitize_combined_payload(_self_label_payload(), receipt, text)
    assert receipt.assistant_self_only_relationless is False


def test_assistant_prefix_text_with_unknown_role_is_not_an_assistant_turn() -> None:
    text = "assistant: You mentioned you like tea."
    receipt = CombinedExtractionReceipt(episode_key="e", episode_text=text)
    _sanitize_combined_payload(_self_label_payload(), receipt, text)
    assert receipt.assistant_self_only_relationless is False


def test_user_echo_edge_is_suppressed_only_when_receipt_role_is_assistant() -> None:
    text = "You mentioned you want to build a web scraper."
    assistant = CombinedExtractionReceipt(episode_key="e1", episode_text=text, turn_role="assistant")
    out = _sanitize_combined_payload(_echo_payload(), assistant, text)
    assert out["edges"] == [] and assistant.self_echo_edges_suppressed == 1

    prefixed = "assistant: " + text
    user = CombinedExtractionReceipt(episode_key="e2", episode_text=prefixed, turn_role="user")
    out = _sanitize_combined_payload(_echo_payload(), user, prefixed)
    assert len(out["edges"]) == 1 and user.self_echo_edges_suppressed == 0
