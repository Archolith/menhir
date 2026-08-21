"""CF-58: the past-vs-present lexicon core is shared, not duplicated.

The text classifier (``facet_derivation``) and the query classifier
(``temporal_intent``) each carry a temporal keyword list that overlaps. The fix makes
the overlap a single source (``temporal_lexicon``) and defines each tuple as shared core
+ audience-specific extras, WITHOUT merging the two audiences and WITHOUT changing the
contents or order of either tuple.

The load-bearing assertions are the ones that would FAIL if the fix regressed: the exact
tuple contents (element + order) catch an accidental merge, and the substitution test
proves each classifier reads the shared core at call time, not a private copy.
"""

from __future__ import annotations

import pytest

from menhir.domain import facet_derivation, temporal_intent, temporal_lexicon
from menhir.domain.temporal import TemporalQuery

pytestmark = pytest.mark.unit


def test_lexicon_tuples_unchanged_element_and_order() -> None:
    """Behaviour preserved: the four tuples keep their exact pre-CF-58 contents/order."""
    assert facet_derivation._HISTORICAL_MARKERS == (
        "used to", "previously", "no longer", "deprecated", "formerly",
        "back then", "old approach", "we removed",
    )
    assert facet_derivation._CURRENT_MARKERS == (
        "now", "currently", "current", "as of today", "today we",
    )
    assert temporal_intent._HISTORY_CUES == (
        "used to", "previously", "originally", "before", "what broke",
        "what did we believe", "what did i believe", "regress", "history",
        "historically", "earlier", "back when", "at the time", "former",
        "no longer", "stopped working", "since when",
    )
    assert temporal_intent._CURRENT_CUES == (
        "now", "currently", "current", "today", "latest", "at present", "these days",
    )


def test_shared_core_is_a_single_source() -> None:
    """The overlap lives in one module, and both consumers build from it."""
    assert temporal_lexicon._SHARED_HISTORICAL_MARKERS == ("used to", "previously", "no longer")
    assert temporal_lexicon._SHARED_CURRENT_MARKERS == ("now", "currently", "current")


def test_historical_substitution_reaches_both_classifiers(monkeypatch) -> None:
    """Load-bearing: patching the shared core makes BOTH classifiers follow.

    The TEXT classifier (facet_derivation) and the QUERY classifier (temporal_intent)
    must both recognise a string containing the sentinel, proving each reads the shared
    core rather than holding a private copy.
    """
    sentinel = "quadruplicate"
    monkeypatch.setattr(
        temporal_lexicon, "_SHARED_HISTORICAL_MARKERS",
        temporal_lexicon._SHARED_HISTORICAL_MARKERS + (sentinel,),
    )

    assert facet_derivation._extract_bucket(f"we {sentinel} the build") == "historical"
    assert (
        temporal_intent.classify_temporal_intent(f"{sentinel} the plan").query
        is TemporalQuery.AS_KNOWN_AT
    )


def test_current_substitution_reaches_both_classifiers(monkeypatch) -> None:
    sentinel = "progazine"
    monkeypatch.setattr(
        temporal_lexicon, "_SHARED_CURRENT_MARKERS",
        temporal_lexicon._SHARED_CURRENT_MARKERS + (sentinel,),
    )

    assert facet_derivation._extract_bucket(f"{sentinel} works") == "current"
    assert (
        temporal_intent.classify_temporal_intent(f"is it {sentinel} running").query
        is TemporalQuery.CURRENT_BELIEF
    )


def test_audience_specific_extras_still_work_and_do_not_leak() -> None:
    """POSITIVE CONTROL: extras work, and neither leaks into the other classifier."""
    assert facet_derivation._extract_bucket("we deprecated that API") == "historical"
    assert (
        temporal_intent.classify_temporal_intent("is it still working these days").query
        is TemporalQuery.CURRENT_BELIEF
    )

    # "these days" is a query-only cue: the TEXT classifier must not match it.
    assert facet_derivation._extract_bucket("these days") is None
    # "deprecated" is a text-only marker: the QUERY classifier must not match it.
    assert temporal_intent.classify_temporal_intent("deprecated").query is TemporalQuery.CURRENT_BELIEF
    assert temporal_intent.classify_temporal_intent("deprecated").cue is None
