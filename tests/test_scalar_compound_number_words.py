"""#200: a number written as more than one word must never be read from one of its pieces."""

from __future__ import annotations

import pytest

from menhir.services.typed_scalar_rules import (
    _count_value_from_source,
    _normalize_interval_frequency,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ("I have twenty-five postcards", 25),
        ("I have twenty five postcards", 25),
        ("I have Forty-Two stamps", 42),
        ("I have twenty postcards", 20),
        ("I have fifteen postcards", 15),
        ("I have 3 cats", 3),
        ("I have five cats", 5),
        ("I have a five-year-old son", 5),
    ],
)
def test_count_reads_whole_number_words(span: str, expected: int) -> None:
    assert _count_value_from_source(span, "absolute", None) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "span",
    [
        "I have two hundred postcards",
        "I have 2 thousand coins",
        "I have two dozen eggs",
        "I have one hundred twenty-five postcards",
        "I have 3-4 cats",
    ],
)
def test_count_gives_no_value_for_part_of_a_longer_number(span: str) -> None:
    assert _count_value_from_source(span, "absolute", None) is None


@pytest.mark.unit
def test_count_range_reads_compound_words() -> None:
    assert _count_value_from_source("between twenty-five and thirty cards", "absolute", None) == [25, 30]


@pytest.mark.unit
def test_count_range_gives_no_value_when_a_bound_is_part_of_a_longer_number() -> None:
    assert _count_value_from_source("between 2 and 3 thousand coins", "absolute", None) is None


@pytest.mark.unit
def test_count_delta_reads_compound_words() -> None:
    assert _count_value_from_source("I sold twenty-five cards", "delta", None) == -25


@pytest.mark.unit
@pytest.mark.parametrize(
    "span",
    [
        "I swim twenty-five times every week",
        "I swim twenty five times every week",
        "I swim twenty times every week",
        "I swim a hundred times every year",
        "I swim 2 thousand times every year",
    ],
)
def test_frequency_gives_no_value_for_multi_word_counts(span: str) -> None:
    assert _normalize_interval_frequency(span) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ("I swim five times every week", (5, "week")),
        ("I swim twice every week", (2, "week")),
        ("I swim every other week", (0.5, "week")),
        ("I swim every week", (1, "week")),
    ],
)
def test_frequency_single_word_counts_are_unchanged(span: str, expected) -> None:
    assert _normalize_interval_frequency(span) == expected
