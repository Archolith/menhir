"""Issue #96: the typed-scalar prompt must tell the model a named third party keeps their name.

The prompt's subject rule had two branches -- speaker-self -> `user`, owned object -> the object
-- and listed `'my wake time'` and ownership as SELF exemplars. With no branch for a named third
party, gpt-4o-mini (k=3, unanimous) produced `user's wake time: 07:30` for "Alice wakes up at
7:30 AM." and `user's owned: 37` for "Alice owns 37 coins.", while "Alice has read 12 books."
(no exemplar) correctly became `Alice's read count`. That is the prompt being obeyed, not noise.

An offline test cannot verify the model; it pins what the model is TOLD. The live three-call
reproducer verifies the behaviour.
"""

from __future__ import annotations

import re

import pytest

from menhir.services.typed_scalar_rules import TYPED_SCALAR_SYSTEM_PROMPT as PROMPT

pytestmark = pytest.mark.unit


def test_prompt_has_an_explicit_third_party_rule() -> None:
    assert "NAMED THIRD PARTY" in PROMPT


def test_third_party_rule_covers_the_self_exemplar_attributes() -> None:
    """The regression was attribute-keyed: the exemplars 'wake time' and ownership pulled a named
    subject onto `user`. The rule must name those attributes explicitly as still third-party."""
    rule = PROMPT[PROMPT.index("NAMED THIRD PARTY"):]
    assert "wake time" in rule
    assert "ownership" in rule
    assert "EVERY attribute" in rule


def test_user_is_gated_on_first_person_grammar_not_attribute() -> None:
    rule = PROMPT[PROMPT.index("NAMED THIRD PARTY"):]
    assert "ONLY for a sentence that is itself first-person" in rule
    assert "the attribute alone never makes the speaker the subject" in rule


def test_third_party_rule_sits_inside_the_subject_field() -> None:
    """It must be part of the `subject` field's instruction, before the field closes, so it is read
    as a subject rule and not as a stray sentence after the schema."""
    subject_start = PROMPT.index('"subject": <')
    subject_end = PROMPT.index(">, ", subject_start)
    rule_pos = PROMPT.index("NAMED THIRD PARTY")
    assert subject_start < rule_pos < subject_end


def test_existing_self_and_owned_object_rules_are_untouched() -> None:
    """The tuned branches stay exactly as they were; this change only adds the missing one."""
    assert "'my height', 'my wake time', 'my day off', 'my commute' — use exactly 'user'" in PROMPT
    assert "subject='my car', attribute='color', NOT subject='user'" in PROMPT
    assert "Never move an owned object's property onto the user" in PROMPT


def test_prompt_is_still_one_well_formed_instruction() -> None:
    """Concatenated string literals: a missing space or a broken quote would silently change what
    the model reads. Check the joins around the inserted rule."""
    assert re.search(r"onto the user\. And if the fact is about a NAMED THIRD PARTY", PROMPT)
    assert re.search(r"the speaker the subject>, \"attribute\":", PROMPT)
