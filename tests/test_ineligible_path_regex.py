"""#888: the path-name clause of the merge-ineligibility predicate must reach Neo4j intact."""

import re

import pytest

from menhir.infrastructure.correlation_queries import CorrelationRepository

# Passed as a query parameter, so the regex engine sees this string verbatim (no Cypher unescape).
# Java and Python agree on this pattern; Neo4j's `=~` is a whole-string match, i.e. fullmatch.
_PATTERN = re.compile(CorrelationRepository._INELIGIBLE_PATH_REGEX)


@pytest.mark.parametrize(
    "name",
    [
        "src/menhir/foo.py",
        "src/menhir/infrastructure",
        "docs/plans",
        r"C:\Users\x",
        "main.py:42",
        "config.yaml settings",
    ],
)
def test_path_shaped_names_match(name):
    assert _PATTERN.fullmatch(name)


@pytest.mark.parametrize("name", ["Charles", "Menhir release", "python", "my.pyramid"])
def test_plain_names_do_not_match(name):
    assert not _PATTERN.fullmatch(name)


def test_predicate_uses_the_parameter_not_a_literal():
    predicate = CorrelationRepository._INELIGIBLE_ROLE_PREDICATE
    assert "$ineligible_path_regex" in predicate
    assert "\\b" not in predicate
