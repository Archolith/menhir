"""Unit tests for the erasure read-suppression veto (CF-165 Phase F).

Pure predicate module; uses a hand-written fake implementing :class:`LiveErasureLookup`.
No SQLite and no real store -- the fake is a dict of suppressed (subject_type, subject_value)
pairs plus a call counter.
"""

from __future__ import annotations

import pytest

from menhir.services.erasure_veto import ErasedSubjectError, ErasureVeto, LiveErasureLookup


class FakeLookup:
    def __init__(self, suppressed: set[tuple[str, str]] | None = None) -> None:
        self.suppressed = set(suppressed or ())
        self.calls: list[tuple[str, str]] = []

    def has_live_erasure(self, *, subject_type: str, subject_value: str) -> bool:
        self.calls.append((subject_type, subject_value))
        return (subject_type, subject_value) in self.suppressed


class ExplodingLookup:
    def has_live_erasure(self, *, subject_type: str, subject_value: str) -> bool:
        raise RuntimeError("store unavailable")


def make_veto(suppressed: set[tuple[str, str]], cache_enabled: bool = True) -> tuple[ErasureVeto, FakeLookup]:
    lookup = FakeLookup(suppressed)
    return ErasureVeto(lookup=lookup, cache_enabled=cache_enabled), lookup


def row(uuid: str | None = None, group_id: str | None = None, **extra: object) -> dict[str, object]:
    out: dict[str, object] = dict(extra)
    if uuid is not None:
        out["uuid"] = uuid
    if group_id is not None:
        out["group_id"] = group_id
    return out


@pytest.mark.unit
def test_is_suppressed_by_node_uuid() -> None:
    veto, _ = make_veto({("NODE_UUID", "n-1")})
    assert veto.is_suppressed(node_uuid="n-1") is True


@pytest.mark.unit
def test_is_suppressed_by_namespace() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1")})
    assert veto.is_suppressed(namespace="ns-1") is True


@pytest.mark.unit
def test_is_suppressed_false_when_neither_matches() -> None:
    veto, _ = make_veto({("NODE_UUID", "n-1"), ("NAMESPACE", "ns-1")})
    assert veto.is_suppressed(node_uuid="n-other", namespace="ns-other") is False


@pytest.mark.unit
def test_node_not_individually_named_is_suppressed_by_namespace_erasure() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1")})
    assert veto.is_suppressed(node_uuid="n-not-named", namespace="ns-1") is True


@pytest.mark.unit
def test_blank_and_none_are_not_matches_and_issue_no_lookup() -> None:
    veto, lookup = make_veto({("NAMESPACE", "")})
    assert veto.is_suppressed(node_uuid=None, namespace=None) is False
    assert veto.is_suppressed(node_uuid="", namespace="") is False
    assert veto.is_suppressed(node_uuid="n-1", namespace=None) is False
    assert ("NODE_UUID", "n-1") in lookup.calls
    assert ("NAMESPACE", "") not in lookup.calls
    assert not any(t == "namespace" and v in ("", None) for t, v in lookup.calls)


@pytest.mark.unit
def test_filter_rows_drops_suppressed_and_keeps_others() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1"), ("NODE_UUID", "n-3")})
    rows = [
        row(uuid="n-1", group_id="ns-1"),  # suppressed by namespace
        row(uuid="n-3", group_id="ns-9"),  # suppressed by node_uuid
        row(uuid="n-2", group_id="ns-2"),  # kept
        row(uuid="n-4"),  # kept, no group
    ]
    kept = veto.filter_rows(rows)
    assert [r["uuid"] for r in kept] == ["n-2", "n-4"]
    assert all(isinstance(r, dict) for r in kept)


@pytest.mark.unit
def test_filter_rows_passes_through_row_with_neither_key() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1")})
    rows = [row(name="foo", value=1), row(name="bar", value=2)]
    kept = veto.filter_rows(rows)
    assert kept == [{"name": "foo", "value": 1}, {"name": "bar", "value": 2}]


@pytest.mark.unit
def test_filter_rows_missing_keys_do_not_raise() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1")})
    keyless = {"name": "only-name"}
    unrelated = {"uuid": "n-1"}
    suppressed = {"group_id": "ns-1"}
    kept = veto.filter_rows([keyless, unrelated, suppressed])

    # A row carrying neither key passes through untouched: this filter must never become a way
    # to silently drop data it cannot classify. The unrelated uuid is kept because nothing
    # suppresses it; only the namespace-matched row is dropped.
    assert kept == [keyless, unrelated]
    assert all(isinstance(r, dict) for r in kept)


@pytest.mark.unit
def test_caching_limits_lookups_for_shared_namespace() -> None:
    veto, lookup = make_veto({("NAMESPACE", "ns-1")})
    rows = [row(uuid=None, group_id="ns-1") for _ in range(50)]
    veto.filter_rows(rows)
    assert len(lookup.calls) == 1


@pytest.mark.unit
def test_cache_disabled_issues_more_lookups() -> None:
    veto, lookup = make_veto({("NAMESPACE", "ns-1")}, cache_enabled=False)
    rows = [row(uuid=None, group_id="ns-1") for _ in range(50)]
    veto.filter_rows(rows)
    assert len(lookup.calls) == 50


@pytest.mark.unit
def test_cache_keyed_by_subject_so_repeated_distinct_subjects_still_lookup() -> None:
    veto, lookup = make_veto(set())
    for i in range(20):
        veto.is_suppressed(namespace=f"ns-{i}")
    assert len(lookup.calls) == 20


@pytest.mark.unit
def test_assert_readable_raises_when_suppressed() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1")})
    with pytest.raises(ErasedSubjectError):
        veto.assert_readable(node_uuid="n-1", namespace="ns-1")


@pytest.mark.unit
def test_assert_readable_returns_none_when_readable() -> None:
    veto, _ = make_veto({("NAMESPACE", "ns-1")})
    assert veto.assert_readable(node_uuid="n-2", namespace="ns-2") is None


@pytest.mark.unit
def test_fail_closed_lookup_raise_treats_subject_as_suppressed() -> None:
    veto = ErasureVeto(lookup=ExplodingLookup())
    assert veto.is_suppressed(node_uuid="n-1") is True


@pytest.mark.unit
def test_fail_closed_lookup_raise_drops_row_in_filter() -> None:
    veto = ErasureVeto(lookup=ExplodingLookup())
    rows = [row(uuid="n-1", group_id="ns-1"), row(uuid="n-2", group_id="ns-2")]
    kept = veto.filter_rows(rows)
    assert kept == []
