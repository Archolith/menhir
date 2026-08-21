"""CF-59: the temporal-lens vocabulary (current | historical | any) has one source.

The three lens values were carried as bare strings with the allowed values only in a
trailing comment and branched on as literals in ``oracle_combiner._ranking_score``. The
fix introduces ``TemporalLens`` (a ``str`` enum, so ``==`` against plain strings keeps
working across the serialization boundary) and uses it at every branch/comparison without
changing the string values.

A ``str`` enum's members are immutable, so the strong substitution form (monkeypatch the
constant and watch a branch follow) is impossible; the load-bearing assertion below is the
weak form: the branch no longer contains the bare literals and reads the enum members.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.domain.facets import FacetedQuery
from menhir.domain.intent_affinity import task_intents_to_lens
from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
from menhir.domain.oracles import QueryContext
from menhir.domain.query_intent import TaskIntent
from menhir.domain.temporal_lens import TemporalLens

pytestmark = pytest.mark.unit


def test_string_values_unchanged() -> None:
    """The three constants equal exactly the wire/storage strings."""
    assert TemporalLens.CURRENT == "current"
    assert TemporalLens.HISTORICAL == "historical"
    assert TemporalLens.ANY == "any"


def test_plain_strings_still_work_at_entry_points() -> None:
    """Compatibility: callers passing the plain strings keep working everywhere."""
    qc = QueryContext(text="x", intent="historical")
    assert qc.intent == "historical"
    fq = FacetedQuery(intent="any")
    assert fq.intent == "any"

    z = {"relevant": 1.0, "current": 2.0, "historical": 3.0, "conflict": 0.5, "blocked": 0.25}
    rs = LogSpaceOracleCombiner._ranking_score
    assert rs(QueryContext(text="", intent="historical"), z) == 3.75
    assert rs(QueryContext(text="", intent="any"), z) == 0.75

    assert task_intents_to_lens([TaskIntent.DEBUG_FAILURE]) == "current"
    assert task_intents_to_lens([TaskIntent.AVOID_REPEAT]) == "historical"
    assert task_intents_to_lens([TaskIntent.VERIFY_CURRENTNESS]) == "any"


def test_branch_reads_enum_not_literals() -> None:
    """Weak-form substitution: the ranking branch no longer holds bare literals.

    ``TemporalLens`` is a ``str`` enum whose members are immutable, so monkeypatching a
    member's value is impossible. Instead assert the branch in ``_ranking_score`` reads
    the enum members, not the string literals.
    """
    from menhir.domain import oracle_combiner

    src = inspect.getsource(oracle_combiner)
    assert 'query.intent == "historical"' not in src
    assert 'query.intent == "any"' not in src
    assert "query.intent == TemporalLens.HISTORICAL" in src
    assert "query.intent == TemporalLens.ANY" in src


def test_ranking_score_positive_control_all_lenses() -> None:
    """POSITIVE CONTROL: today's numbers for all three lenses, pinned from the formula."""
    z = {"relevant": 1.0, "current": 2.0, "historical": 3.0, "conflict": 0.5, "blocked": 0.25}
    rs = LogSpaceOracleCombiner._ranking_score

    # historical -> relevant + historical - blocked = 1.0 + 3.0 - 0.25
    assert rs(QueryContext(text="", intent="historical"), z) == pytest.approx(3.75)
    # any        -> relevant - blocked = 1.0 - 0.25
    assert rs(QueryContext(text="", intent="any"), z) == pytest.approx(0.75)
    # current    -> relevant + current - blocked - conflict = 1.0 + 2.0 - 0.25 - 0.5
    assert rs(QueryContext(text="", intent="current"), z) == pytest.approx(2.25)


def test_the_lens_formats_as_its_value_not_as_its_member_name() -> None:
    """THE TRAP THIS ENUM ALMOST WALKED INTO, and the reason it is StrEnum.

    `class TemporalLens(str, Enum)` keeps `==` against a plain string working and serializes
    through `json.dumps` as the value -- so every equality assertion in this file passes under it.
    What it does NOT keep is formatting:

        str(X.HISTORICAL)  -> 'TemporalLens.HISTORICAL'   # str, Enum
        f"{X.HISTORICAL}"  -> 'TemporalLens.HISTORICAL'   # str, Enum

    Any log line, operator message or f-string-built payload carrying the lens would have silently
    changed text with nothing failing. `StrEnum` formats as the value, which makes these members a
    true drop-in for the bare strings they replaced.
    """
    import json

    for member, value in (
        (TemporalLens.CURRENT, "current"),
        (TemporalLens.HISTORICAL, "historical"),
        (TemporalLens.ANY, "any"),
    ):
        assert str(member) == value
        assert f"{member}" == value
        assert "%s" % member == value
        assert json.dumps({"intent": member}) == json.dumps({"intent": value})
        assert {value: "hit"}.get(member) == "hit"
