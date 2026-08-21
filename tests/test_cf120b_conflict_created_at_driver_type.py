"""CF-120b: the stale-conflict resolver was a total no-op on the live driver.

CF-120 recorded that `auto_resolve_stale_conflicts` fetched newest-first and filtered oldest, so
it never reached its backlog. It also carried an open question, marked NOT RUN for want of a live
Neo4j: whether the Python age computation raised on the driver's temporal type and skipped
*every* group, which would be a worse finding than the sort order.

Settled 2026-08-20 against Neo4j 5.26.21 on the throwaway test instance. It did:

    created_at type : neo4j.time.DateTime
    has tzinfo attr : True
    tzinfo value    : UTC
    age computation : RAISED TypeError
                      unsupported operand type(s) for -: 'datetime.datetime' and 'DateTime'

The guard was `if not hasattr(created_at, "tzinfo"): created_at = datetime.fromisoformat(...)`.
A `neo4j.time.DateTime` HAS `tzinfo`, so the parse was skipped, `now - created_at` raised, and the
bare `except (ValueError, TypeError, AttributeError, OverflowError): continue` swallowed it. Every
group was skipped. The job had never resolved a single conflict, and reported `0` either way.

The general invariant, which is why this is worth pinning: `hasattr(x, "tzinfo")` is not a test for
"x is a stdlib datetime". `neo4j.time.DateTime` quacks like one and does not interoperate with
`datetime` arithmetic. `parse_iso8601` is the canonical read-back parser and its own docstring says
anything parsing a Neo4j timestamp must use it.

These tests are OFFLINE: `neo4j.time.DateTime` is pure Python and constructible with no server, so
the regression stays covered in the default lane. The live reproduction lives in
`tests/test_cf120b_conflict_created_at_live.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from neo4j.time import DateTime

from menhir.services.lifecycle_conflicts import LifecycleConflictMixin

pytestmark = pytest.mark.unit


class _Service(LifecycleConflictMixin):
    def __init__(self, adapter) -> None:
        self.graph_adapter = adapter


class _Adapter:
    """Returns whatever created_at shape the test hands it, ignoring the cutoff -- so the Python
    filter is the only thing standing between the group and resolution."""

    def __init__(self, created_at) -> None:
        self._created_at = created_at
        self.resolved: list[str] = []

    def list_conflict_groups(self, **kwargs):
        return [{"group_id": "g1", "status": "unresolved", "created_at": self._created_at}]

    def resolve_conflict_group(self, group_id, action, **kwargs):
        self.resolved.append(group_id)
        return {"member_uuids": []}


def _old(days: int = 90):
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_the_driver_type_really_does_break_naive_datetime_arithmetic() -> None:
    """The premise, asserted rather than assumed. If a future driver makes this work, the guard
    below stops being load-bearing and this test says so."""
    value = DateTime.from_native(_old())

    assert hasattr(value, "tzinfo"), "the old guard branched on this and it is True"
    assert not isinstance(value, datetime), "...but it is not a stdlib datetime"
    with pytest.raises(TypeError):
        datetime.now(timezone.utc) - value


def test_a_neo4j_datetime_group_is_resolved() -> None:
    """The finding. Before the fix this raised TypeError into the bare except and resolved
    nothing, for every group, forever."""
    adapter = _Adapter(DateTime.from_native(_old()))

    assert _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=50) == 1
    assert adapter.resolved == ["g1"]


@pytest.mark.parametrize(
    ("label", "make"),
    [
        ("neo4j DateTime", lambda: DateTime.from_native(_old())),
        ("stdlib aware", _old),
        ("stdlib naive", lambda: (datetime.now() - timedelta(days=90))),
        ("iso string", lambda: _old().isoformat()),
        ("zone-id string", lambda: _old().isoformat().replace("+00:00", "Z") + "[UTC]"),
    ],
)
def test_every_shape_this_field_actually_arrives_in_is_aged_correctly(label, make) -> None:
    """The live driver returns a neo4j.time.DateTime; the repo fakes return ISO strings; a
    stdlib datetime is the obvious third. All three must age, or the job silently skips a
    population depending only on who supplied the row."""
    adapter = _Adapter(make())

    assert _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=50) == 1, label


def test_a_young_group_is_still_skipped_in_every_shape() -> None:
    """POSITIVE CONTROL: the fix must not make the filter unconditional. Without this, a
    parse that returned 'very old' for everything would pass every test above."""
    for make in (
        lambda: DateTime.from_native(datetime.now(timezone.utc) - timedelta(days=1)),
        lambda: datetime.now(timezone.utc) - timedelta(days=1),
        lambda: (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    ):
        adapter = _Adapter(make())
        assert _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=50) == 0
        assert adapter.resolved == []


@pytest.mark.parametrize("value", ["not a date", "", 12345, object()])
def test_an_unageable_timestamp_fails_closed(value) -> None:
    """A timestamp we cannot age is LEFT ALONE, never auto-resolved. Resolution is a mutation;
    guessing an age in order to perform one is the wrong direction to fail."""
    adapter = _Adapter(value)

    assert _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=50) == 0
    assert adapter.resolved == []


def test_a_missing_timestamp_is_skipped() -> None:
    adapter = _Adapter(None)

    assert _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=50) == 0
    assert adapter.resolved == []
