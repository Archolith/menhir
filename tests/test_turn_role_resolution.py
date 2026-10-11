"""Pure tests for `menhir.domain.turn_role` (issue #271): the speaker role comes from structured
data, never from the episode text.
"""

from __future__ import annotations

import pytest

from menhir.domain.turn_role import (
    TURN_ROLES,
    UNKNOWN_TURN_ROLE,
    normalize_turn_role,
    resolve_turn_role,
)

pytestmark = pytest.mark.unit

TURN = "I moved to Lisbon two weeks ago."


def _claimed(**overrides):
    row = {
        "content": TURN,
        "turn_evidence_count": 1,
        "turn_evidence_text": TURN,
        "turn_evidence_role": "user",
        "turn_role": None,
    }
    row.update(overrides)
    return row


# ---- normalize_turn_role ------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(TURN_ROLES))
def test_every_turn_role_normalizes_to_itself(role) -> None:
    assert normalize_turn_role(role) == role


def test_the_role_vocabulary_is_exactly_the_five_speakers() -> None:
    assert set(TURN_ROLES) == {"user", "assistant", "tool", "agent", "system"}
    assert UNKNOWN_TURN_ROLE == "unknown"
    assert UNKNOWN_TURN_ROLE not in TURN_ROLES


def test_normalize_strips_and_lowercases() -> None:
    assert normalize_turn_role(" User ") == "user"
    assert normalize_turn_role("ASSISTANT\n") == "assistant"


@pytest.mark.parametrize("value", [None, "", "   ", "human", "unknown", 1, True, b"user", ["user"]])
def test_normalize_rejects_anything_that_is_not_a_known_role(value) -> None:
    assert normalize_turn_role(value) is None


# ---- resolve_turn_role: evidence-proven ----------------------------------------------------------


def test_evidence_proven_user_when_one_turn_and_content_equals_turn_text() -> None:
    assert resolve_turn_role(_claimed()) == "user"


def test_evidence_wins_over_a_declared_role() -> None:
    assert resolve_turn_role(_claimed(turn_role="assistant")) == "user"


def test_evidence_role_is_used_as_recorded_not_hardcoded_to_user() -> None:
    claimed = _claimed(turn_evidence_role="assistant", turn_role="user")
    assert resolve_turn_role(claimed) == "assistant"


def test_evidence_role_is_normalized() -> None:
    assert resolve_turn_role(_claimed(turn_evidence_role=" Tool ")) == "tool"


def test_agent_paraphrase_linked_to_a_user_turn_is_not_user() -> None:
    claimed = _claimed(content="The user relocated to Lisbon recently.")
    assert resolve_turn_role(claimed) == "unknown"


def test_agent_paraphrase_falls_back_to_the_declared_role() -> None:
    claimed = _claimed(content="The user relocated to Lisbon recently.", turn_role="agent")
    assert resolve_turn_role(claimed) == "agent"


@pytest.mark.parametrize("suffix", [" ", "\n", "."])
def test_content_must_be_byte_equal_to_the_turn_text(suffix) -> None:
    assert resolve_turn_role(_claimed(content=TURN + suffix)) == "unknown"


def test_two_linked_turns_with_equal_text_are_not_evidence_proven() -> None:
    assert resolve_turn_role(_claimed(turn_evidence_count=2)) == "unknown"
    assert resolve_turn_role(_claimed(turn_evidence_count=2, turn_role="tool")) == "tool"


@pytest.mark.parametrize("count", [None, 0, "1", True, 1.0, -1])
def test_only_an_exact_int_one_counts_as_evidence(count) -> None:
    assert resolve_turn_role(_claimed(turn_evidence_count=count)) == "unknown"


@pytest.mark.parametrize("key", ["content", "turn_evidence_text"])
def test_missing_or_non_string_text_is_not_evidence(key) -> None:
    assert resolve_turn_role(_claimed(**{key: None})) == "unknown"
    assert resolve_turn_role(_claimed(**{key: b"x"})) == "unknown"


def test_evidence_with_an_invalid_role_is_not_evidence() -> None:
    assert resolve_turn_role(_claimed(turn_evidence_role="human")) == "unknown"
    assert resolve_turn_role(_claimed(turn_evidence_role="human", turn_role="system")) == "system"


# ---- resolve_turn_role: declared role / unknown --------------------------------------------------


def test_a_text_prefix_grants_nothing() -> None:
    claimed = {"content": "user: I moved to Lisbon"}
    assert resolve_turn_role(claimed) == "unknown"


def test_a_conflicting_text_prefix_does_not_override_the_declared_role() -> None:
    claimed = {"content": "assistant: hi", "turn_role": "user"}
    assert resolve_turn_role(claimed) == "user"


def test_a_user_prefix_on_equal_evidence_text_does_not_change_the_evidence_role() -> None:
    text = "assistant: I moved to Lisbon"
    claimed = _claimed(content=text, turn_evidence_text=text, turn_evidence_role="user")
    assert resolve_turn_role(claimed) == "user"


def test_declared_role_is_normalized() -> None:
    assert resolve_turn_role({"content": "x", "turn_role": " Assistant "}) == "assistant"


@pytest.mark.parametrize("declared", ["human", "", "unknown", 3, None])
def test_invalid_declared_role_resolves_to_unknown(declared) -> None:
    assert resolve_turn_role({"content": "x", "turn_role": declared}) == "unknown"


def test_empty_claim_resolves_to_unknown() -> None:
    assert resolve_turn_role({}) == "unknown"
