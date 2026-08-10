"""Event History Phase 2B.2a: ViewRepository event-timeline mixin wrappers.

No live Neo4j. A route-based FakeNeo4j feeds canned rows and records every call; a tiny
`_CaptureRepo` subclass overrides only generic `record` / `_fetch_current` so the wrappers'
keying/discriminator/normalization/authoritative delegation can be asserted without a write core.

Covers: fail-closed blank subject_uuid/predicate on record+fetch; entity-anchored key + event lane
discriminator; single normalization of the event payload and the authoritative write; count from
view_value; legacy record_timeline/fetch_timeline byte behavior unchanged; list filter/projection/
deterministic order; retirement safety predicate (never a legacy subject-only timeline); event-lane
guard on the edge-redraw queries (a wrong view UUID cannot mutate legacy edges); normalization
BEFORE delete (an invalid required entry causes no execute); deterministic edge params/ordinals/
valid_at/time_basis/evidence_tier; empty clear; partial draw surfaced (drawn<expected never
concealed); pagination bounds/order/next_offset; and no learned_at in event-edge/order queries.
Generic synthetic content only — no benchmark IDs/answers/phrases.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.view_models import _normalize_entries
from menhir.infrastructure.view_repository import ViewRepository

_EP = "ep-1"
_SRC = "ep-1:0:5:0"


def _entry(**overrides):
    """A valid synthetic event-lane entry; override any field for a scenario (None drops it)."""
    base = {
        "assertion_key": "ak-1",
        "source_key": _SRC,
        "when": "2026-05-10T12:00:00Z",
        "what": "acquired a tripod",
        "object_key": "obj:tripod",
        "object_uuid": "obj-uuid-1",
        "quote": "I picked up a new tripod last week",
        "episode_uuid": _EP,
        "turn_evidence_uuid": "turn-1",
        "time_basis": "explicit",
        "evidence_tier": "agent",
    }
    out = dict(base)
    for k, v in overrides.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


class FakeNeo4j:
    """Routes an executed cypher string to canned rows by substring match; records every call."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or []
        self.default = [] if default is None else default
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        for sub, rows in self.routes:
            if sub in query:
                return rows
        return self.default

    def last(self):
        return self.executed[-1] if self.executed else (None, None)


class _CaptureRepo(ViewRepository):
    """Full ViewRepository, but generic `record` / `_fetch_current` are stubbed to capture their
    arguments — the wrappers' delegation is asserted against these, not a real write core."""

    def __init__(self, fake):
        super().__init__(fake)
        self.record_calls: list[tuple[str, dict]] = []
        self.fetch_calls: list[tuple[str, str]] = []

    def record(self, kind_name, **kwargs):
        self.record_calls.append((kind_name, kwargs))
        return {"view_key": "k", "view_value": float(len(kwargs.get("entries") or []))}

    def _fetch_current(self, kind_name, key, *, view_class=None):
        self.fetch_calls.append((kind_name, key))
        return {"uuid": "v1", "subject": "Alpha", "count": 2, "valid_at": "2026-05-10T12:00:00Z",
                "entries": [], "predicate": "purchased", "domain": None}


# --- fail-closed blanks ------------------------------------------------------------------

@pytest.mark.unit
def test_record_event_timeline_rejects_blank_subject_uuid_and_predicate():
    repo = _CaptureRepo(FakeNeo4j())
    with pytest.raises(ValueError):
        repo.record_event_timeline(subject="Alpha", subject_uuid="   ", predicate="purchased",
                                   entries=[_entry()])
    with pytest.raises(ValueError):
        repo.record_event_timeline(subject="Alpha", subject_uuid="ent-a", predicate="  ",
                                   entries=[_entry()])
    assert repo.record_calls == []


@pytest.mark.unit
def test_fetch_event_timeline_rejects_blank_subject_uuid_and_predicate():
    repo = _CaptureRepo(FakeNeo4j())
    with pytest.raises(ValueError):
        repo.fetch_event_timeline(subject_uuid="", predicate="purchased")
    with pytest.raises(ValueError):
        repo.fetch_event_timeline(subject_uuid="ent-a", predicate="  ")
    assert repo.fetch_calls == []


# --- record: normalize-once + authoritative + count --------------------------------------

@pytest.mark.unit
def test_record_event_timeline_normalizes_once_and_writes_authoritative():
    repo = _CaptureRepo(FakeNeo4j())
    late = _entry(assertion_key="ak-late", when="2026-07-01T00:00:00Z")
    early = _entry(assertion_key="ak-early", when="2026-01-01T00:00:00Z")
    res = repo.record_event_timeline(
        subject="Alpha", subject_uuid="ent-a", predicate="purchased", domain="camera_lens",
        namespace="ns", entries=[late, early], source="event-history", source_confidence=0.7,
    )
    (kind, kwargs), = repo.record_calls
    assert kind == "timeline"
    # entity-anchored identity + lane + authoritative + source delegation.
    assert kwargs["subject_uuid"] == "ent-a"
    assert kwargs["subject"] == "Alpha"
    assert kwargs["predicate"] == "purchased" and kwargs["domain"] == "camera_lens"
    assert kwargs["namespace"] == "ns"
    assert kwargs["authoritative"] is True
    assert kwargs["source"] == "event-history" and kwargs["source_confidence"] == 0.7
    # entries normalized exactly once, sorted ascending by world time (input order ignored).
    assert [e["assertion_key"] for e in kwargs["entries"]] == ["ak-early", "ak-late"]
    # count is taken from view_value.
    assert res["count"] == 2 and res["view_value"] == 2.0


@pytest.mark.unit
def test_record_event_timeline_never_uses_legacy_normalizer():
    repo = _CaptureRepo(FakeNeo4j())
    repo.record_event_timeline(subject="Alpha", subject_uuid="ent-a", predicate="purchased",
                               entries=[_entry()])
    (_, kwargs), = repo.record_calls
    # the entry is the full event-lane shape (allowlist), not the legacy {when,what,episode_uuid}.
    assert "source_key" in kwargs["entries"][0] and "object_key" in kwargs["entries"][0]
    assert "quote" in kwargs["entries"][0] and "evidence_tier" in kwargs["entries"][0]


# --- fetch: entity-anchored key + event lane discriminator -------------------------------

@pytest.mark.unit
def test_fetch_event_timeline_uses_entity_anchor_and_event_lane_key():
    repo = _CaptureRepo(FakeNeo4j())
    repo.fetch_event_timeline(subject_uuid="Ent-UUID-1", predicate="Purchased",
                              domain="Camera_Lens", namespace="ns")
    (kind, key), = repo.fetch_calls
    assert kind == "timeline"
    # entity-anchored: the UUID (lowercased) is the identity segment, NOT the text subject.
    assert "ent-uuid-1" in key
    assert "::ent-uuid-1::" in key
    # the lane discriminator is the collision-safe timeline:event: key.
    assert key.startswith("ns::ent-uuid-1::timeline:event:")
    # a text-subject legacy key must differ (never an accidental legacy lookup).
    assert key != ViewRepository._key("ns", "ent-uuid-1", "timeline")


# --- legacy methods unchanged ------------------------------------------------------------

@pytest.mark.unit
def test_legacy_record_timeline_and_fetch_timeline_unchanged():
    repo = _CaptureRepo(FakeNeo4j())
    res = repo.record_timeline(subject="The User", namespace="ns",
                               entries=[{"when": "2026-01-01", "what": "b", "extra": "x"},
                                        {"when": "2026-01-02", "what": "a"}])
    (kind, kwargs), = repo.record_calls
    assert kind == "timeline"
    # legacy path passes NO subject_uuid and never sets authoritative / predicate / domain.
    assert "subject_uuid" not in kwargs
    assert kwargs.get("authoritative") is not True
    assert "predicate" not in kwargs and "domain" not in kwargs
    # legacy normalize keeps only when/what/episode_uuid and sorts ascending by when.
    assert kwargs["entries"] == _normalize_entries(
        [{"when": "2026-01-01", "what": "b"}, {"when": "2026-01-02", "what": "a"}])
    assert all(set(e) == {"when", "what", "episode_uuid"} for e in kwargs["entries"])
    assert res["count"] == 2
    # fetch_timeline looks up the plain text subject-only key.
    repo.fetch_timeline(subject="The User", namespace="ns")
    assert repo.fetch_calls[-1] == ("timeline", "ns::the user::timeline")


# --- list: filter / projection / deterministic order -------------------------------------

def _view_row(uuid, predicate, domain, subject_uuid="ent-a", count=1, valid_at="2026-05-10"):
    return {"uuid": uuid, "view_key": f"ns::{subject_uuid}::{predicate}",
            "subject": "Alpha", "subject_uuid": subject_uuid, "predicate": predicate,
            "domain": domain, "count": count, "valid_at": valid_at}


@pytest.mark.unit
def test_list_event_timeline_views_filters_projects_and_orders():
    # FakeNeo4j returns rows verbatim (it does not execute ORDER BY), so feed the rows already in
    # the deterministic predicate/domain/view_key order the production query emits; the ORDER BY
    # clause itself is asserted separately.
    rows = [
        _view_row("v-pur", "purchased", None),
        _view_row("v-pur-dom", "purchased", "camera_lens"),
        _view_row("v-sold", "sold", "camera_lens"),
    ]
    fake = FakeNeo4j(routes=[("n.view_predicate AS predicate", rows)])
    out = ViewRepository(fake).list_event_timeline_views(subject_uuid="ent-a", namespace="ns")
    q, params = fake.last()
    # projection is the full documented set, ordered deterministically.
    assert set(out[0]) == {"uuid", "view_key", "subject", "subject_uuid", "predicate",
                           "domain", "count", "valid_at"}
    assert [r["predicate"] for r in out] == ["purchased", "purchased", "sold"]
    assert [r["domain"] for r in out] == [None, "camera_lens", "camera_lens"]
    # current-only + entity-anchored + nonblank-predicate + exact namespace filters.
    assert "coalesce(n.view_current, true)" in q
    assert "view_subject_uuid:$u" in q
    assert "trim(coalesce(n.view_predicate, '')) <> ''" in q
    assert "n.group_id = $ns" in q
    assert params["u"] == "ent-a" and params["ns"] == "ns"
    assert "ORDER BY n.view_predicate, n.view_domain, n.view_key" in q


@pytest.mark.unit
def test_list_event_timeline_views_rejects_blank_subject_uuid():
    repo = ViewRepository(FakeNeo4j())
    with pytest.raises(ValueError):
        repo.list_event_timeline_views(subject_uuid="   ")


# --- retire: safety predicate ------------------------------------------------------------

@pytest.mark.unit
def test_retire_event_timeline_requires_nonblank_predicate_and_current():
    fake = FakeNeo4j(routes=[("view_kind:'timeline', view_key:$k", [{"retired": 1}])])
    repo = ViewRepository(fake)
    assert repo.retire_event_timeline(view_key="ns::ent-a::timeline:event:x") is True
    q, params = fake.last()
    assert params["k"] == "ns::ent-a::timeline:event:x"
    # a legacy subject-only timeline (blank view_predicate) must NEVER be retired by this op.
    assert "trim(coalesce(n.view_predicate, '')) <> ''" in q
    assert "view_kind:'timeline', view_key:$k" in q
    assert "coalesce(n.view_current, true)" in q
    for prop in ("view_current = false", "expired_at", "retired = true"):
        assert prop in q
    # a row that matches no current -> False.
    fake2 = FakeNeo4j(routes=[("view_kind:'timeline', view_key:$k", [])])
    assert ViewRepository(fake2).retire_event_timeline(view_key="k") is False


# --- draw: normalization-before-delete ---------------------------------------------------

@pytest.mark.unit
def test_draw_invalid_required_entry_raises_before_any_execute():
    fake = FakeNeo4j()
    repo = ViewRepository(fake)
    with pytest.raises(ValueError):
        repo.draw_event_timeline_entries(view_uuid="v1",
                                         entries=[_entry(), _entry(assertion_key=None, when=None)])
    # the normalize-everything-first guard means NO delete/rewrite query was issued.
    assert fake.executed == []


@pytest.mark.unit
def test_draw_deterministic_edge_params_and_ordinals_and_no_learned_at():
    fake = FakeNeo4j(routes=[("RETURN count(*) AS drawn", [{"drawn": 2}])])
    repo = ViewRepository(fake)
    late = _entry(assertion_key="ak-late", when="2026-07-01T00:00:00Z",
                  time_basis="explicit", evidence_tier="user")
    early = _entry(assertion_key="ak-early", when="2026-01-01T00:00:00Z",
                   time_basis="inferred", evidence_tier="agent")
    res = repo.draw_event_timeline_entries(view_uuid="v1", entries=[late, early])
    q, params = fake.last()
    # ordinal is the NORMALIZED order index (sorted by world time), not input order.
    assert params["entries"] == [
        {"assertion_key": "ak-early", "ordinal": 0, "valid_at": "2026-01-01T00:00:00Z",
         "time_basis": "inferred", "evidence_tier": "agent"},
        {"assertion_key": "ak-late", "ordinal": 1, "valid_at": "2026-07-01T00:00:00Z",
         "time_basis": "explicit", "evidence_tier": "user"},
    ]
    # the edge rewrite matches on assertion_key and carries valid_at/time_basis/evidence_tier.
    assert "TypedEventAssertion {assertion_key: entry.assertion_key}" in q
    assert "EVENT_HISTORY_ENTRY {ordinal: entry.ordinal}" in q
    for prop in ("he.valid_at = entry.valid_at", "he.time_basis = entry.time_basis",
                 "he.evidence_tier = entry.evidence_tier"):
        assert prop in q
    # the View is pinned to an EVENT timeline (kind + nonblank predicate) so a wrong UUID cannot
    # mutate a legacy subject-only timeline's edges.
    assert "view_kind:'timeline'" in q
    assert "trim(coalesce(v.view_predicate, '')) <> ''" in q
    assert "learned_at" not in q
    assert res == {"drawn": 2, "expected": 2}


@pytest.mark.unit
def test_draw_empty_clears_edges_and_returns_zero_counts():
    fake = FakeNeo4j()
    repo = ViewRepository(fake)
    res = repo.draw_event_timeline_entries(view_uuid="v1", entries=[])
    assert res == {"drawn": 0, "expected": 0}
    q, params = fake.last()
    assert "DELETE r" in q and "EVENT_HISTORY_ENTRY" in q
    assert params["view_uuid"] == "v1"
    # even the clear is guarded to an event timeline View.
    assert "view_kind:'timeline'" in q
    assert "trim(coalesce(v.view_predicate, '')) <> ''" in q


@pytest.mark.unit
def test_draw_partial_draw_is_surfaced_never_concealed():
    fake = FakeNeo4j(routes=[("RETURN count(*) AS drawn", [{"drawn": 1}])])
    repo = ViewRepository(fake)
    res = repo.draw_event_timeline_entries(
        view_uuid="v1", entries=[_entry(), _entry(assertion_key="ak-2", when="2026-06-01T00:00:00Z"),
                                 _entry(assertion_key="ak-3", when="2026-07-01T00:00:00Z")])
    # 3 expected, only 1 drawn (2 assertions missing) — surfaced, so callers can fail closed.
    assert res == {"drawn": 1, "expected": 3}
    assert res["drawn"] != res["expected"]


# --- list entries: pagination bounds/order/next offset -----------------------------------

def _entries_page(n):
    return [{"assertion_key": f"ak-{i}", "source_key": f"sk-{i}", "predicate": "purchased",
             "domain": None, "object_key": f"obj-{i}", "object_display": f"Object {i}",
             "quote": f"quote {i}", "valid_at": f"2026-01-{i:02d}T00:00:00Z",
             "episode_uuid": f"ep-{i}", "turn_evidence_uuid": f"turn-{i}",
             "time_basis": "explicit", "evidence_tier": "agent"} for i in range(n)]


@pytest.mark.unit
def test_list_event_timeline_entries_projects_ordered_and_paginated():
    all_entries = _entries_page(5)
    fake = FakeNeo4j(routes=[("all_rows[$offset", [{"entries": all_entries[0:2], "total": 5}])])
    out = ViewRepository(fake).list_event_timeline_entries(view_uuid="v1", offset=0, limit=2,
                                                           namespace="ns")
    q, params = fake.last()
    # exact namespace filter on the View + ordered by ordinal.
    assert "v.group_id = $namespace" in q
    assert "ORDER BY he.ordinal ASC" in q
    assert params["namespace"] == "ns"
    # constrained to an EVENT timeline View (kind + nonblank predicate) like draw/retire.
    assert "view_kind:'timeline'" in q
    assert "trim(coalesce(v.view_predicate, '')) <> ''" in q
    # projection is complete and quote comes from stated_span.
    assert set(out["entries"][0]) == {
        "assertion_key", "source_key", "predicate", "domain", "object_key",
        "object_display", "quote", "valid_at", "episode_uuid", "turn_evidence_uuid",
        "time_basis", "evidence_tier"}
    assert out["total"] == 5
    # next_offset advances to end when more pages remain.
    assert out["next_offset"] == 2
    # never reads learned_at in the read query.
    assert "learned_at" not in q


@pytest.mark.unit
def test_list_event_timeline_entries_next_offset_none_on_last_page():
    all_entries = _entries_page(3)
    fake = FakeNeo4j(routes=[("all_rows[$offset", [{"entries": all_entries[2:3], "total": 3}])])
    out = ViewRepository(fake).list_event_timeline_entries(view_uuid="v1", offset=2, limit=1)
    assert len(out["entries"]) == 1 and out["total"] == 3 and out["next_offset"] is None


@pytest.mark.unit
def test_list_event_timeline_entries_clamps_limit_and_offset():
    # clamp bounds are observable in the params passed to the query.
    fake = FakeNeo4j(routes=[("all_rows[$offset", [{"entries": [], "total": 150}])])
    repo = ViewRepository(fake)
    repo.list_event_timeline_entries(view_uuid="v1", limit=0, offset=-5)
    _, p1 = fake.last()
    assert p1["limit"] == 1 and p1["offset"] == 0
    repo.list_event_timeline_entries(view_uuid="v1", limit=1000, offset=99999)
    _, p2 = fake.last()
    assert p2["limit"] == 100 and p2["offset"] == 99999
