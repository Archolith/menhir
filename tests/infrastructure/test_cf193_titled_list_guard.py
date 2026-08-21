"""CF-193: `parse_titled_list`'s claimed "verbs disqualify the whole block" guard now exists.

The comment claimed verbs disqualify an item (and the whole block) -- one prose line is enough to
refuse, because a half-parsed list is worse than none -- but the implemented guards were only word
count and sentence punctuation. A to-do turn like "Reminder:\\nbuy milk\\nwalk dog\\ncall mom"
minted MEMBER_OF edges into an entity named "Reminder", which is exactly the silent-wrongness the
docstring's cost model forbids.

Chosen fix: **(a) implement the asserted guard.** A closed, conservative verb allowlist
(`_LIST_VERBS`) matched at word boundaries, the same style as `_ACQUISITION_VERBS` in
services/event_history_recall.py. No stemming, no synonyms, no part-of-speech call. One verb-bearing
item refuses the WHOLE block. (b) was not chosen because the positive controls -- genuine NAMED
lists -- pass without any exceptions, so a conservative allowlist is safe here.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.infrastructure.graphiti_extraction_patches import (
    _LIST_VERBS,
    parse_titled_list,
)


# ---------------------------------------------------- the two executed cases from the entry


@pytest.mark.parametrize(
    "turn",
    [
        "user: Reminder:\nbuy milk\nwalk dog\ncall mom",
        "user: Today I did:\nfixed the bug\nshipped release\nate lunch",
    ],
)
def test_the_executed_prose_blocks_are_refused(turn: str) -> None:
    assert parse_titled_list(turn) is None


# ----------------------------------------------------------------- the whole-block rule


@pytest.mark.unit
def test_one_prose_line_refuses_the_whole_block() -> None:
    """Three good names plus ONE verb-bearing prose line -> the whole block is refused, not
    partially accepted."""
    assert (
        parse_titled_list(
            "user: agents:\nAdmon\nMagdy\nEhab\nwe are working today"
        )
        is None
    )


# --------------------------------------------------- positive control: a genuine named list


@pytest.mark.unit
def test_a_genuine_list_of_names_still_parses() -> None:
    parsed = parse_titled_list(
        "user: Favourite bands:\nRadiohead\nPortishead\nMassive Attack"
    )
    assert parsed == (
        "Favourite bands",
        ["Radiohead", "Portishead", "Massive Attack"],
    )


# ------------------------------- positive control: the two existing guards still fire


@pytest.mark.unit
def test_an_overlong_item_still_refuses() -> None:
    assert (
        parse_titled_list(
            "user: agents:\nAdmon\nMagdy\n"
            "the rest of the team will be assigned later this week somehow"
        )
        is None
    )


@pytest.mark.unit
def test_sentence_punctuation_still_refuses() -> None:
    assert (
        parse_titled_list("user: agents:\nAdmon\nMagdy\nEhab.\nSara") is None
    )


# -------------------------------------------------------- drift guard: comment == implementation


@pytest.mark.unit
def test_comment_and_implementation_agree_on_the_verb_guard() -> None:
    """The original finding was "the comment describes a control that does not exist". Pin the
    comment text, the applied guard, and the behavior so that cannot silently recur."""
    from menhir.infrastructure import graphiti_extraction_patches as mod

    src = inspect.getsource(mod)
    assert "Verbs and sentence punctuation disqualify the whole" in src
    assert "_LIST_VERB_RE.match(item)" in src

    # The allowlist must carry the inflections from the executed probes.
    for verb in ("buy", "walk", "call", "fixed", "shipped", "ate"):
        assert verb in _LIST_VERBS

    # Behavioral half: a verb item really is refused.
    assert parse_titled_list("user: Reminder:\nbuy milk\nwalk dog\ncall mom") is None


# ---------------------------------------------------------------------------
# Precision: the guard must not eat the NAME lists this parser exists to accept.
#
# The first implementation matched an allowlisted verb ANYWHERE in an item. That refused
# "Tools:/saw/hammer/drill", "Races:/fun run/..." and an album named "Work", because most of these
# words are also common nouns. Requiring the verb to be the FIRST word of a MULTI-WORD item -- the
# shape of an imperative or past-tense clause -- keeps every CF-193 probe refused while letting
# these through. These cases are the reason for that shape, so they are pinned.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "episode", "expected"),
    [
        ("noun that is also a verb", "user: Tools:\nsaw\nhammer\ndrill",
         ("Tools", ["saw", "hammer", "drill"])),
        ("verb as a non-leading word", "user: Races:\nfun run\nnight run\ntrail run",
         ("Races", ["fun run", "night run", "trail run"])),
        ("single-word title case", "user: Albums:\nWork\nRumours\nKid A",
         ("Albums", ["Work", "Rumours", "Kid A"])),
        ("plain names", "user: Team:\nAlice\nBob\nCarol",
         ("Team", ["Alice", "Bob", "Carol"])),
    ],
)
def test_name_lists_are_still_accepted(label, episode, expected):
    assert parse_titled_list(episode) == expected, label


def test_one_clause_among_names_still_refuses_the_whole_block():
    """The whole-block rule, with the clause NOT first -- a partial parse would be worse than none,
    per the module's own cost model."""
    assert parse_titled_list("user: Stuff:\nRadiohead\nbuy milk\nPortishead") is None
