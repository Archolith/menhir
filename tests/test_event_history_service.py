"""Event History Phase 2B.2b: EventHistoryService rebuild of one event-lane timeline.

Pure-fake tests — no live Neo4j, no LLM. A ``FakeSource`` serves canned
``TypedEventAssertion`` values and records the exact read arguments; a ``FakeSink`` records every
event-lane call and returns configurable outcomes. Covers: entry fidelity/exact quote; canonical UTC
conversion; out-of-order and replay determinism; invalid-time exclusion without ``learned_at``
fallback; exact source read args; successful write + edge proof; edge mismatch incomplete; missing
uuid incomplete; empty/all-invalid exact-lane retirement; sibling lanes untouched; stale duplicate
exact-lane view retirement; exception fail-closed with no retirement; and ``rebuild_lanes``
dedup/order/aggregate completeness. Generic synthetic content only — no benchmark IDs/answers.

Note on canonical namespace: ``EventLane.namespace is None`` normalizes to the empty string ``''``
(never back to ``None``), so reconciliation is exact-tenant and a blank namespace cannot be confused
with the unscoped ``None`` filter.
"""

from __future__ import annotations

import pytest

from menhir.domain.event_history import EventLane, TypedEventAssertion
from menhir.services.event_history_service import EventHistoryService

_EP = "ep-1"


def _assertion(**overrides) -> TypedEventAssertion:
    """A valid generic synthetic event assertion; override any field for a scenario."""
    base = dict(
        subject_uuid="ent-a", subject_display="Alpha", predicate="purchased",
        object_key="obj:tripod", object_display="a tripod",
        valid_at="2026-05-10T12:00:00Z", learned_at="2026-08-01T00:00:00Z",
        stated_span="I picked up a new tripod last week", episode_uuid=_EP,
        object_uuid="obj-uuid-1", domain="camera_lens", namespace="ns",
        turn_evidence_uuid="turn-1", time_basis="explicit", evidence_tier="agent",
    )
    base.update(overrides)
    return TypedEventAssertion(**base)


class FakeSource:
    def __init__(self, assertions=None):
        self.assertions = list(assertions or [])
        self.reads: list[tuple] = []

    def assertions_for_lane(self, lane, *, include_superseded=False, materializable_only=True):
        self.reads.append((lane, include_superseded, materializable_only))
        return list(self.assertions)


class FakeSink:
    def __init__(self, record_result=None, draw_result=None, views=None):
        self.record_result = record_result if record_result is not None else {
            "uuid": "view-1", "view_key": "ns::ent-a::timeline:event:x"}
        self.draw_result = draw_result
        self.views = list(views or [])
        self.records: list[dict] = []
        self.draws: list[dict] = []
        self.lists: list[dict] = []
        self.retired: list[str] = []

    def record_event_timeline(self, **kwargs):
        self.records.append(kwargs)
        return dict(self.record_result)

    def list_event_timeline_views(self, **kwargs):
        self.lists.append(kwargs)
        return [dict(v) for v in self.views]

    def retire_event_timeline(self, **kwargs):
        self.retired.append(kwargs["view_key"])
        return True

    def draw_event_timeline_entries(self, **kwargs):
        self.draws.append(kwargs)
        if self.draw_result is not None:
            return dict(self.draw_result)
        n = len(kwargs["entries"])
        return {"drawn": n, "expected": n}


def _lane(**over) -> EventLane:
    base = dict(subject_uuid="ent-a", predicate="purchased", namespace="ns", domain="camera_lens")
    base.update(over)
    return EventLane(**base)


def _service(source_assertions, sink=None, *, lane=None) -> tuple[EventHistoryService, FakeSource, FakeSink]:
    src = FakeSource(source_assertions)
    snk = sink if sink is not None else FakeSink()
    return EventHistoryService(src, snk), src, snk


# --- entry fidelity / exact quote / canonical UTC ---------------------------------------

@pytest.mark.unit
def test_entry_fidelity_exact_quote_and_canonical_utc():
    a = _assertion(
        object_display="an 85mm lens", stated_span="  I bought an 85mm lens last month  ",
        valid_at="2026-05-10T12:00:00+02:00",
    )
    svc, src, snk = _service([a])
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is True and out["written"] == 1
    entry = snk.records[0]["entries"][0]
    # exact quote from stated_span, verbatim (not trimmed).
    assert entry["quote"] == "  I bought an 85mm lens last month  "
    # occurrence wording from predicate + object_display, no ownership/supersession language.
    assert entry["what"] == "purchased an 85mm lens"
    assert "current" not in entry["what"] and "supersed" not in entry["what"].lower()
    # canonical UTC 'Z' conversion (offset normalized to UTC).
    assert entry["when"] == "2026-05-10T10:00:00Z"
    # the fixed entry schema is preserved end-to-end.
    assert set(entry) == {
        "assertion_key", "source_key", "when", "what", "object_key", "object_uuid",
        "quote", "episode_uuid", "turn_evidence_uuid", "time_basis", "evidence_tier",
    }
    assert entry["source_key"] == a.source_key
    assert entry["object_key"] == "obj:tripod" and entry["object_uuid"] == "obj-uuid-1"
    assert entry["episode_uuid"] == _EP and entry["turn_evidence_uuid"] == "turn-1"
    assert entry["time_basis"] == "explicit" and entry["evidence_tier"] == "agent"


# --- exact source read args + canonical lane -------------------------------------------

@pytest.mark.unit
def test_exact_source_read_args_and_canonical_components():
    a = _assertion()
    svc, src, snk = _service([a])
    svc.rebuild_lane(_lane(subject_uuid="ENT-A", predicate="Purchased", domain="Camera_Lens",
                           namespace="  NS  "))
    (lane, incl, mat), = src.reads
    # read exactly one lane, superseded excluded, materializable only.
    assert incl is False and mat is True
    # canonical lane normalized from lane.key (lowercased/trimmed), forwarded to every sink call.
    assert (lane.namespace, lane.subject_uuid, lane.predicate, lane.domain) == (
        "ns", "ent-a", "purchased", "camera_lens")
    rec = snk.records[0]
    assert rec["subject_uuid"] == "ent-a" and rec["predicate"] == "purchased"
    assert rec["domain"] == "camera_lens" and rec["namespace"] == "ns"
    lst = snk.lists[0]
    assert lst["subject_uuid"] == "ent-a" and lst["namespace"] == "ns"


@pytest.mark.unit
def test_canonical_namespace_stays_empty_string_not_none():
    a = _assertion(namespace=None, domain=None)
    svc, src, snk = _service([a])
    svc.rebuild_lane(_lane(namespace=None, domain=None))
    (lane, _, _), = src.reads
    # None -> '' (never back to None), so blank-namespace lanes reconcile to the exact default silo
    # rather than the unscoped None filter.
    assert lane.namespace == "" and lane.domain == ""
    assert snk.records[0]["namespace"] == ""
    assert snk.lists[0]["namespace"] == ""


# --- out-of-order + replay determinism -------------------------------------------------

@pytest.mark.unit
def test_out_of_order_sorted_by_world_time_then_assertion_key():
    # Distinct assertions share identity inputs except world time; assertion_key therefore differs
    # and ordering is world time then assertion_key (input order ignored).
    late = _assertion(valid_at="2026-07-01T00:00:00Z", object_display="a late object")
    early = _assertion(valid_at="2026-01-01T00:00:00Z", object_display="an early object")
    assert late.assertion_key != early.assertion_key
    svc, src, snk = _service([late, early])
    svc.rebuild_lane(_lane())
    entries = snk.records[0]["entries"]
    assert [e["assertion_key"] for e in entries] == [early.assertion_key, late.assertion_key]
    assert [e["when"] for e in entries] == ["2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z"]


def _replay(span: str, learned_at: str) -> TypedEventAssertion:
    """An exact replay of the SAME occurrence: identical identity inputs (same episode/span/ordinal,
    predicate, object_key, domain, namespace, valid_at, time_basis, perceiver_version) so the computed
    assertion_key is identical, but a distinct display surface (stated_span) and ingest time."""
    return _assertion(
        valid_at="2026-05-10T12:00:00Z", object_key="obj:tripod", object_display="a tripod",
        stated_span=span, learned_at=learned_at,
    )


@pytest.mark.unit
def test_replay_dedup_uses_total_representative_without_learned_at():
    # Two replays share assertion_key (proven identical) but differ in stated_span and ingest time.
    # The representative is chosen by the stable display tie-break (minimal stated_span), NEVER by
    # learned_at: the earlier-learned copy (quote-b, learned 2026-01-01) must NOT win.
    r1 = _replay(span="quote-a", learned_at="2026-09-01T00:00:00Z")
    r2 = _replay(span="quote-b", learned_at="2026-01-01T00:00:00Z")
    assert r1.assertion_key == r2.assertion_key  # gitleaks:allow — identifier comparison

    svc1, _, snk1 = _service([r1, r2])
    svc2, _, snk2 = _service([r2, r1])
    svc1.rebuild_lane(_lane())
    svc2.rebuild_lane(_lane())
    e1 = snk1.records[0]["entries"]
    e2 = snk2.records[0]["entries"]
    # one retained entry, byte-identical regardless of input order.
    assert len(e1) == 1 and len(e2) == 1
    assert e1 == e2
    assert e1[0]["assertion_key"] == r1.assertion_key
    # minimal stated_span wins (quote-a), even though r1 has the LATER learned_at. If learned_at were
    # used it would pick r2 (earlier ingest) -> quote-b.
    assert e1[0]["quote"] == "quote-a"


# --- invalid-time exclusion without learned fallback ------------------------------------

@pytest.mark.unit
def test_invalid_valid_at_excluded_counted_no_learned_fallback():
    bad = _assertion(valid_at="not-a-time", learned_at="2026-05-10T12:00:00Z")
    good = _assertion(valid_at="2026-05-10T12:00:00Z")
    svc, src, snk = _service([bad, good])
    out = svc.rebuild_lane(_lane())
    # the unparseable-world-time assertion is excluded from the View and counted; the parseable one
    # is retained. learned_at is never used to repair the missing world time.
    assert out["excluded_invalid_time"] == 1
    assert out["entry_count"] == 1
    assert len(snk.records[0]["entries"]) == 1
    assert snk.records[0]["entries"][0]["assertion_key"] == good.assertion_key


# --- successful write + edge proof -----------------------------------------------------

@pytest.mark.unit
def test_successful_write_and_edge_proof_complete():
    a = _assertion()
    svc, src, snk = _service([a])
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is True
    assert out["written"] == 1 and out["entry_count"] == 1
    assert out["drawn"] == 1 and out["expected"] == 1
    assert out["view_key"] == "ns::ent-a::timeline:event:x"
    assert out["retired"] == 0 and out["retired_views"] == []
    # the draw is pinned to the written view uuid with the same entries.
    draw = snk.draws[0]
    assert draw["view_uuid"] == "view-1"
    assert draw["entries"] == snk.records[0]["entries"]
    assert out["subject_uuid"] == "ent-a" and out["namespace"] == "ns"
    assert out["predicate"] == "purchased" and out["domain"] == "camera_lens"
    # reconciliation listed this lane's views and kept the desired one.
    assert snk.lists[0]["subject_uuid"] == "ent-a"


# --- edge mismatch incomplete, no retirement -------------------------------------------

@pytest.mark.unit
def test_edge_mismatch_incomplete_no_retirement():
    a = _assertion()
    snk = FakeSink(draw_result={"drawn": 1, "expected": 2})
    svc, src, _ = _service([a], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert out["written"] == 1
    assert out["drawn"] == 1 and out["expected"] == 2
    assert "edge draw mismatch" in out["reason"]
    # no speculative retirement on an incomplete write.
    assert snk.retired == []
    assert out["retired"] == 0 and out["retired_views"] == []


@pytest.mark.unit
def test_missing_view_uuid_incomplete_no_retirement():
    a = _assertion()
    snk = FakeSink(record_result={"uuid": "  ", "view_key": "k"})
    svc, src, _ = _service([a], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "no view uuid" in out["reason"]
    assert snk.retired == []


@pytest.mark.unit
def test_missing_view_key_incomplete_no_retirement():
    a = _assertion()
    snk = FakeSink(record_result={"uuid": "view-1", "view_key": "  "})
    svc, src, _ = _service([a], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "no view_key" in out["reason"]
    assert snk.retired == []


# --- empty / all-invalid exact-lane retirement -----------------------------------------

@pytest.mark.unit
def test_empty_lane_retires_exact_lane_views_no_empty_view():
    views = [
        {"view_key": "ns::ent-a::timeline:event:x", "predicate": "purchased", "domain": "camera_lens"},
    ]
    snk = FakeSink(views=views)
    svc, src, _ = _service([], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is True
    assert out["written"] == 0 and out["entry_count"] == 0
    assert snk.records == []  # no empty View written
    assert out["retired"] == 1 and out["retired_views"] == ["ns::ent-a::timeline:event:x"]
    assert snk.retired == ["ns::ent-a::timeline:event:x"]


@pytest.mark.unit
def test_all_invalid_lane_retires_exact_lane_views():
    bad = _assertion(valid_at="garbage")
    views = [
        {"view_key": "k1", "predicate": "purchased", "domain": "camera_lens"},
        {"view_key": "k2", "predicate": "purchased", "domain": "camera_lens"},
    ]
    snk = FakeSink(views=views)
    svc, src, _ = _service([bad], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is True
    assert out["entry_count"] == 0 and out["excluded_invalid_time"] == 1
    assert out["written"] == 0
    assert out["retired"] == 2
    assert snk.records == []  # all invalid -> no empty View written


# --- sibling lanes untouched -----------------------------------------------------------

@pytest.mark.unit
def test_sibling_lanes_never_touched_on_empty_retirement():
    # Only exact-lane views are retired; a sibling lane (different predicate) is left alone.
    views = [
        {"view_key": "exact", "predicate": "purchased", "domain": "camera_lens"},
        {"view_key": "sibling-pred", "predicate": "sold", "domain": "camera_lens"},
        {"view_key": "sibling-dom", "predicate": "purchased", "domain": "tripod"},
    ]
    snk = FakeSink(views=views)
    svc, src, _ = _service([], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["retired"] == 1 and out["retired_views"] == ["exact"]
    assert snk.retired == ["exact"]


@pytest.mark.unit
def test_sibling_lanes_never_touched_on_retained_reconcile():
    a = _assertion()
    views = [
        {"view_key": "ns::ent-a::timeline:event:x", "predicate": "purchased", "domain": "camera_lens"},
        {"view_key": "sibling", "predicate": "sold", "domain": "camera_lens"},
    ]
    snk = FakeSink(views=views)
    svc, src, _ = _service([a], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is True
    # desired view kept; sibling never retired.
    assert out["retired"] == 0 and out["retired_views"] == []
    assert snk.retired == []


# --- stale duplicate exact-lane view retirement ----------------------------------------

@pytest.mark.unit
def test_stale_duplicate_exact_lane_view_retired_desired_kept():
    a = _assertion()
    stale = {"view_key": "ns::ent-a::timeline:event:OLD", "predicate": "purchased", "domain": "camera_lens"}
    desired = {"view_key": "ns::ent-a::timeline:event:x", "predicate": "purchased", "domain": "camera_lens"}
    snk = FakeSink(views=[stale, desired])
    svc, src, _ = _service([a], sink=snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is True
    assert out["retired"] == 1 and out["retired_views"] == ["ns::ent-a::timeline:event:OLD"]
    assert snk.retired == ["ns::ent-a::timeline:event:OLD"]


# --- exception fail-closed, no retirement ----------------------------------------------

@pytest.mark.unit
def test_source_exception_fail_closed_no_retirement():
    class BoomSource(FakeSource):
        def assertions_for_lane(self, lane, *, include_superseded=False, materializable_only=True):
            raise RuntimeError("read exploded")

    snk = FakeSink()
    svc = EventHistoryService(BoomSource(), snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "read exploded" in out["reason"]
    assert snk.records == [] and snk.retired == [] and snk.draws == []


@pytest.mark.unit
def test_sink_record_exception_fail_closed_no_retirement():
    class BoomSink(FakeSink):
        def record_event_timeline(self, **kwargs):
            raise RuntimeError("write exploded")

    a = _assertion()
    snk = BoomSink()
    svc = EventHistoryService(FakeSource([a]), snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "write exploded" in out["reason"]
    assert snk.retired == []


@pytest.mark.unit
def test_draw_exception_fail_closed_no_retirement():
    class BoomSink(FakeSink):
        def draw_event_timeline_entries(self, **kwargs):
            raise RuntimeError("edge exploded")

    a = _assertion()
    snk = BoomSink()
    svc = EventHistoryService(FakeSource([a]), snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "edge exploded" in out["reason"]
    assert snk.retired == []


@pytest.mark.unit
def test_list_exception_after_edge_proof_fail_closed_no_retirement():
    # Edge proof completes, but reconciliation's list raises: the result must be complete=False (NOT
    # a stale complete with a reason), and nothing may have been retired.
    a = _assertion()

    class BoomListSink(FakeSink):
        def list_event_timeline_views(self, **kwargs):
            raise RuntimeError("reconcile list exploded")

    snk = BoomListSink()
    svc = EventHistoryService(FakeSource([a]), snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "reconcile list exploded" in out["reason"]
    assert out["drawn"] == 1 and out["expected"] == 1  # edge proof itself passed
    assert snk.retired == []


@pytest.mark.unit
def test_failed_stale_retire_fails_closed_reports_unreconciled():
    # A stale duplicate exact-lane View exists, but retire() refuses it: the rebuild must not claim
    # fully reconciled — complete=False with the unreconciled key, desired view left current.
    a = _assertion()
    stale = {"view_key": "ns::ent-a::timeline:event:OLD", "predicate": "purchased", "domain": "camera_lens"}
    snk = FakeSink(views=[stale])
    snk.retire_event_timeline = lambda **kw: False  # noqa: B010 - force a failed retirement
    svc = EventHistoryService(FakeSource([a]), snk)
    out = svc.rebuild_lane(_lane())
    assert out["complete"] is False
    assert "reconcile failed to retire stale view(s): ns::ent-a::timeline:event:OLD" in out["reason"]
    assert out["retired"] == 0 and out["retired_views"] == []


@pytest.mark.unit
def test_eventlane_rejects_invalid_construction():
    # "Fail closed naturally through EventLane validation": an invalid lane cannot even be built, so
    # the service never receives one (no blank-key lane can reach a rebuild).
    with pytest.raises(ValueError):
        EventLane(subject_uuid="", predicate="purchased")
    with pytest.raises(ValueError):
        EventLane(subject_uuid="ent-a", predicate="   ")


# --- rebuild_lanes dedup / order / aggregate completeness ------------------------------

@pytest.mark.unit
def test_rebuild_lanes_dedup_and_deterministic_order():
    a = _assertion()
    l1 = _lane()
    l1_dup = _lane()  # identical key
    l2 = _lane(subject_uuid="ent-b", predicate="sold", namespace="ns", domain="camera_lens")
    svc, src, snk = _service([a])
    out = svc.rebuild_lanes([l2, l1, l1_dup])
    assert out["lane_count"] == 2
    # deterministic lane-key order: (ns, ent-a,...) before (ns, ent-b,...).
    assert [r["subject_uuid"] for r in out["results"]] == ["ent-a", "ent-b"]
    assert out["complete"] is True
    # one read + one record per distinct lane.
    assert len(snk.records) == 2


@pytest.mark.unit
def test_rebuild_lanes_aggregate_complete_false_when_any_incomplete():
    a = _assertion()
    good = _lane()
    src = FakeSource([a])
    # force the second lane incomplete via a failing source for that lane's read is complex with the
    # shared FakeSource, so instead use a sink that fails on the second record.
    class FailSecondSink(FakeSink):
        def __init__(self):
            super().__init__()
            self.n = 0

        def record_event_timeline(self, **kwargs):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("boom on second")
            return dict(self.record_result)

    bad = _lane(subject_uuid="ent-b", predicate="sold", namespace="ns", domain="camera_lens")
    svc = EventHistoryService(src, FailSecondSink())
    out = svc.rebuild_lanes([bad, good])
    assert out["complete"] is False
    assert [r["complete"] for r in out["results"]] == [True, False]


@pytest.mark.unit
def test_rebuild_lanes_empty_aggregate():
    svc, src, snk = _service([])
    out = svc.rebuild_lanes([])
    assert out["lane_count"] == 0 and out["complete"] is False
    assert out["results"] == []
