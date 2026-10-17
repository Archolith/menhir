"""Verb-centric question rule: pure, no I/O."""

from __future__ import annotations

import pytest

from menhir.domain.verb_facets import VerbFacet, _forms, _lemma, is_verb_centric, verb_facets

VERB_QUESTIONS = [
    "How many projects have I completed since starting painting classes?",
    "How many items of clothing do I need to pick up or return from a store?",
    "How many different art-related events did I attend in the past month?",
    "How many antique items did I inherit or acquire from my family members?",
    "How many projects have I been working on simultaneously, excluding my thesis?",
    "How many projects have I led or am currently leading?",
    "How many music albums or EPs have I purchased or downloaded?",
    "Did I mostly recently increase or decrease the limit on the number of cups of coffee in the morning?",
    "How many different cuisines have I learned to cook or tried out in the past few months?",
    "How many pieces of furniture did I buy, assemble, sell, or fix in the past few months?",
    "Which event did I participate in first, the volleyball league or the charity 5K run to raise money for a local children's hospital?",
    "Which gift did I buy first, the necklace for my sister or the photo album for my mom?",
    "How many model kits have I worked on or bought?",
    "Which task did I complete first, fixing the fence or trimming the goats' hooves?",
    "Which task did I complete first, fixing the fence or purchasing three cows from Peter?",
    "Which bike did I fixed or serviced the past weekend?",
    "How many bikes did I service or plan to service in March?",
    "Which event did I participate in first, the charity gala or the charity bake sale?",
    "How many kitchen items did I replace or fix?",
]

NOUN_QUESTIONS = [
    "How many days did I spend on camping trips in the United States this year?",
    "How many weeks did it take me to watch all the Marvel Cinematic Universe movies and the main Star Wars films?",
    "Which book did I finish reading first, 'The Hate U Give' or 'The Nightingale'?",
    "How many tanks do I currently have, including the one I set up for my friend's kid?",
    "I was going through our previous conversation and I was wondering if you could remind me of the two companies you mentioned that prioritize employee safety and well-being like Triumvirate?",
    "I was going through our previous conversation about making a classic French omelette, and I wanted to confirm - how many eggs did you say we need for the recipe?",
]


@pytest.mark.unit
@pytest.mark.parametrize("question", VERB_QUESTIONS)
def test_verb_centric_questions_are_flagged(question):
    assert is_verb_centric(question) is True
    assert verb_facets(question)


@pytest.mark.unit
@pytest.mark.parametrize("question", NOUN_QUESTIONS)
def test_noun_questions_are_not_flagged(question):
    assert is_verb_centric(question) is False
    assert verb_facets(question) == ()


@pytest.mark.unit
def test_clothing_question_facets():
    assert verb_facets("How many items of clothing do I need to pick up or return from a store?") == (
        VerbFacet("pick up", ("pick up", "picked up", "picking up", "picks up")),
        VerbFacet("return", ("return", "returned", "returning", "returns")),
    )


@pytest.mark.unit
def test_irregular_past_facet_forms():
    facets = verb_facets("How many projects have I led or am currently leading?")
    assert [f.label for f in facets] == ["lead"]
    assert facets[0].forms == ("lead", "leaded", "leading", "leads", "led")


@pytest.mark.unit
def test_particle_and_irregular_facets():
    assert verb_facets("How many model kits have I worked on or bought?") == (
        VerbFacet("work on", ("work on", "worked on", "working on", "works on")),
        VerbFacet("buy", ("bought", "buy", "buyed", "buying", "buys")),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("word", "base"),
    [
        ("bought", "buy"),
        ("tried", "try"),
        ("leading", "lead"),
        ("stopped", "stop"),
        ("fixed", "fix"),
        ("returned", "return"),
    ],
)
def test_lemma(word, base):
    assert _lemma(word) == base


# The fulltext index does not stem, so the base and 3rd-person forms must be searched explicitly.
@pytest.mark.unit
@pytest.mark.parametrize(
    ("word", "needed"),
    [
        ("completed", {"complete", "completes", "completed", "completing"}),
        ("completing", {"complete", "completes", "completed"}),
        ("donated", {"donate", "donates"}),
        ("baked", {"bake", "bakes", "baking"}),
        ("dancing", {"dance", "dances", "danced"}),
        ("focused", {"focus", "focuses", "focusing"}),
        ("stop", {"stopped", "stopping", "stops"}),
        ("stopped", {"stop", "stops", "stopping"}),
        ("fix", {"fixes", "fixed", "fixing"}),
        ("try", {"tries", "tried", "trying"}),
        ("returned", {"return", "returns", "returning"}),
    ],
)
def test_forms_cover_base_and_inflections(word, needed):
    assert needed <= set(_forms(word, ""))


@pytest.mark.unit
@pytest.mark.parametrize("word", ["returned", "worked", "called", "attended"])
def test_forms_add_no_silent_e_after_consonant_clusters(word):
    assert not any(f.endswith("e") or f.endswith("es") for f in _forms(word, "") if f != word)
