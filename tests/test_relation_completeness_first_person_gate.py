"""Issue #90: the first-person self-binding rules must not reach third-person episodes.

`_RELATION_COMPLETENESS_INSTRUCTIONS` bundled two rules -- always emit a relationship for a
stated fact (subject-neutral) and represent I/me/my as `user` (first-person only) -- and was
appended to every episode. gpt-4o-mini applied the second to a third-person subject and rewrote
"Alice owns 37 coins" as "User owns 37 coins"; no `Alice` node was ever created.

These tests drive the two instruction builders directly. No LLM: they pin what the model is
TOLD, which is the only part an offline test can pin. That gating the text restores `Alice`
under the real model is verified by the live `SS_DIAG=1` reproducer, not here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure import graphiti_extraction_patches as gep

pytestmark = pytest.mark.unit

THIRD_PERSON = [
    "Alice owns 37 coins.",
    "Alice has read 12 books.",
    "Alice wakes up at 7:30 AM.",
    "The user account was locked by the admin.",   # `user` as an ordinary noun, no author
]
FIRST_PERSON = [
    "I own 37 coins.",
    "I'm actually using a new app I recently downloaded.",   # contraction
    "Alice and I own coins.",                                 # mixed
    "My wake time is 7:30 AM.",
    "Give it to me.",
]

_SELF_BINDING_MARKERS = ("`user`", "I/me/my", "new app", "WANTS_TO_KNOW_MORE_ABOUT")
_ENDPOINT = SimpleNamespace(marker="MENHIR-SELF-abc123")


# --------------------------------------------------------------------------------------
# The predicate
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", THIRD_PERSON)
def test_third_person_is_not_first_person(text: str) -> None:
    assert gep._is_first_person(text) is False


@pytest.mark.parametrize("text", FIRST_PERSON)
def test_first_person_is_detected(text: str) -> None:
    assert gep._is_first_person(text) is True


def test_empty_and_none_are_not_first_person() -> None:
    assert gep._is_first_person("") is False
    assert gep._is_first_person(None) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Relation-completeness block
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", THIRD_PERSON)
def test_third_person_gets_core_only(text: str) -> None:
    block = gep._relation_completeness_instructions(None, text)
    assert block.startswith("MENHIR RELATION COMPLETENESS:")
    assert "Do not return an entity without a relationship" in block
    assert "Do not invent a relationship" in block
    for marker in _SELF_BINDING_MARKERS:
        assert marker not in block, f"self-binding text {marker!r} leaked into a third-person prompt"
    assert "speaker" not in block, "third-person core must steer toward the subject, not the speaker"


@pytest.mark.parametrize("text", FIRST_PERSON)
def test_first_person_gets_core_plus_self_binding(text: str) -> None:
    block = gep._relation_completeness_instructions(None, text)
    assert "Do not return an entity without a relationship" in block
    for marker in _SELF_BINDING_MARKERS:
        assert marker in block


def test_first_person_keeps_do_not_invent_as_the_closing_rule() -> None:
    block = gep._relation_completeness_instructions(None, "I own 37 coins.")
    assert block.rstrip().endswith("omit the entity as well.")
    assert block.index("`user`") < block.index("Do not invent a relationship")


def test_endpoint_variant_third_person_has_no_marker() -> None:
    block = gep._relation_completeness_instructions(_ENDPOINT, "Alice owns 37 coins.")
    assert _ENDPOINT.marker not in block
    assert "`user`" not in block


def test_endpoint_variant_first_person_binds_to_marker_not_user() -> None:
    block = gep._relation_completeness_instructions(_ENDPOINT, "I own 37 coins.")
    assert _ENDPOINT.marker in block
    assert "`user`" not in block


# --------------------------------------------------------------------------------------
# Relationless-repair block
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", THIRD_PERSON)
def test_repair_third_person_has_no_first_person_rules(text: str) -> None:
    block = gep._relationless_repair_instructions(None, text)
    assert block.startswith("CORRECTIVE RE-EXTRACTION:")
    assert "Do not invent facts." in block
    assert "`user`" not in block
    assert "I own" not in block


@pytest.mark.parametrize("text", FIRST_PERSON)
def test_repair_first_person_binds_speaker_to_user(text: str) -> None:
    block = gep._relationless_repair_instructions(None, text)
    assert "bind a human first-person speaker to `user`" in block
    assert "Do not invent facts." in block


def test_repair_endpoint_variant_gated_and_bound_to_marker() -> None:
    assert _ENDPOINT.marker not in gep._relationless_repair_instructions(_ENDPOINT, "Alice owns coins.")
    block = gep._relationless_repair_instructions(_ENDPOINT, "I own coins.")
    assert _ENDPOINT.marker in block
    assert "`user`" not in block


# --------------------------------------------------------------------------------------
# Single source of truth: the prompt gate and the post-processing gate must agree
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", THIRD_PERSON + FIRST_PERSON)
def test_prompt_gate_and_author_alias_gate_agree(text: str) -> None:
    """`_unresolved_author_aliases` refuses to treat `user` as the author unless the text is
    first-person. The prompt must produce `user` under exactly the same condition, or the two
    halves of the pipeline disagree about what an author is -- which is how #90 happened."""
    prompt_says_user = "`user`" in gep._relation_completeness_instructions(None, text)
    receipt = SimpleNamespace(self_subject_endpoint=_ENDPOINT, episode_text=text)
    nodes = [SimpleNamespace(uuid="u1", name="user")]
    alias_gate_treats_user_as_author = bool(gep._unresolved_author_aliases(nodes, receipt))
    assert prompt_says_user == alias_gate_treats_user_as_author
