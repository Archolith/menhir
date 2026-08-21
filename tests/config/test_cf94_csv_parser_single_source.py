"""CF-94: the config CSV parser has a single source of truth.

The same comma-delimited tuple parser was written twice inside ``config/``:
``oauth._split_csv`` and ``settings_helpers.parse_csv_env``. Consolidation
deletes the local copy in ``oauth`` and points its call site at the survivor
in the settings-helpers module.

The regression to prevent is a SECOND parser reappearing, so these tests are
structural: ``oauth`` must not re-declare ``_split_csv``, the oauth call site
must behave identically to the shared helper, and the survivor must not have
changed to match the deleted copy.
"""

from __future__ import annotations

import pytest

from menhir.config import oauth
from menhir.config.settings_helpers import parse_csv_env

pytestmark = pytest.mark.unit


def test_oauth_no_longer_defines_split_csv() -> None:
    """The oauth module must not re-declare its own parser."""
    assert "_split_csv" not in vars(oauth)


def test_oauth_call_site_behaves_like_shared_helper() -> None:
    """The oauth call site (``_as_tuple`` string branch) matches parse_csv_env.

    ``oauth._as_tuple`` with a string value delegates to the shared parser; with
    ``None`` it returns the provided default (distinct code path).
    """
    for raw in (
        " a , b , c ",
        "a,,b",
        "a,b,",
        "",
        "  only  ",
    ):
        assert oauth._as_tuple(raw) == parse_csv_env(raw)


def test_oauth_edge_cases_parsed_identically() -> None:
    """Explicit assertions for the shared helper's edge cases through oauth."""
    assert oauth._as_tuple(" a , b , c ") == ("a", "b", "c")
    assert oauth._as_tuple("a,,b") == ("a", "b")
    assert oauth._as_tuple("a,b,") == ("a", "b")
    assert oauth._as_tuple("") == ()
    assert oauth._as_tuple("  only  ") == ("only",)
    assert oauth._as_tuple(None, default=("d",)) == ("d",)


def test_parse_csv_env_itself_unchanged() -> None:
    """POSITIVE CONTROL: the survivor's behaviour is unchanged.

    A consolidation must not "fix" the survivor to match the deleted copy; these
    assertions pin the helper's actual behaviour so changing it would fail.
    """
    assert parse_csv_env(" a , b , c ") == ("a", "b", "c")
    assert parse_csv_env("a,,b") == ("a", "b")
    assert parse_csv_env("a,b,") == ("a", "b")
    assert parse_csv_env("") == ()
    assert parse_csv_env("  only  ") == ("only",)


def test_the_call_site_actually_READS_the_survivor() -> None:
    """THE LOAD-BEARING TEST, added after the structural ones failed to bite.

    `test_oauth_no_longer_defines_split_csv` only catches a duplicate that reuses the deleted
    NAME. Verified by mutation: replacing the call site with an equivalent inline expression --
    `tuple(p.strip() for p in value.split(",") if p.strip())` -- left all of the tests above
    passing, because the behaviour is identical and no symbol named `_split_csv` reappeared.

    Substituting the survivor and observing the call site change is what proves delegation
    rather than coincidence. Same shape as CF-91's `test_call_site_reads_surviving_constant`.
    """
    sentinel = ("SUBSTITUTED",)
    original = oauth.parse_csv_env
    try:
        oauth.parse_csv_env = lambda raw: sentinel  # type: ignore[assignment]
        assert oauth._as_tuple("a,b,c") == sentinel
    finally:
        oauth.parse_csv_env = original  # type: ignore[assignment]

    # POSITIVE CONTROL: the substitution really was undone.
    assert oauth._as_tuple("a,b,c") == ("a", "b", "c")
