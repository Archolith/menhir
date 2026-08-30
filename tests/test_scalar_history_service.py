"""Scalar history rebuild + projection coordinator (Slice 2).

Tests the service-level rebuild, reconciliation, HISTORY_ENTRY edge drawing,
and the coordinator that runs both projections.

Uses fake adapters — no Neo4j, no LLM.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import pytest

from menhir.services.scalar_state_service import ScalarStateService
from menhir.domain.scalar_history import HistoryEntry
from menhir.infrastructure.view_repository import ViewRepository


# ---- fake adapters -----------------------------------------------------------

def _assertion(**over):
    base = dict(
        assertion_id="a?", subject_uuid="ent-postcards", subject_display="my postcards",
        attribute="postcard_count", scope="collection", value_kind="count", unit="postcards",
        operation="delta", value=17, valid_at="2023-08-11T00:00:00+00:00",
        learned_at="2023-08-11T00:00:00+00:00", evidence_tier="user",
        episode_uuid="ep-1", turn_id="t1", stated_span="17 postcards",
        binding_pending=False,
    )
    base.update(over)
    return base


class FakeAssertionSource:
    """Minimal assertion source that returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self._rows = rows or []

    def materializable_assertions_for_entity(
        self, subject_uuid: str, *, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("subject_uuid") == subject_uuid]


class FakeViewSink:
    """Minimal view sink that records calls for verification."""

    def __init__(self):
        self.scalar_states: list[dict[str, Any]] = []
        self.scalar_histories: list[dict[str, Any]] = []
        self.retired_state_keys: list[str] = []
        self.retired_history_keys: list[str] = []
        self.drawn_state_edges: list[dict[str, Any]] = []
        self.drawn_history_entries: list[dict[str, Any]] = []
        self._state_views: list[dict[str, Any]] = []
        self._history_views: list[dict[str, Any]] = []
        self._uuid_counter = 0

    def _next_uuid(self) -> str:
        self._uuid_counter += 1
        return f"view-{self._uuid_counter}"

    def record_scalar_state(self, **kwargs: Any) -> dict[str, Any]:
        uuid = self._next_uuid()
        self.scalar_states.append(kwargs)
        return {"uuid": uuid, "view_key": f"state-{uuid}", "created": True, "superseded": False}

    def list_scalar_state_views(
        self, *, subject_uuid: str, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return [v for v in self._state_views if v.get("subject_uuid") == subject_uuid]

    def retire_scalar_state(self, *, view_key: str) -> bool:
        self.retired_state_keys.append(view_key)
        return True

    def draw_scalar_state_provenance_edges(self, **kwargs: Any) -> dict[str, int]:
        self.drawn_state_edges.append(kwargs)
        return {"current_anchor": 1, "contributed_to": 0, "superseded_anchor": 0}

    def record_scalar_history(self, **kwargs: Any) -> dict[str, Any]:
        uuid = self._next_uuid()
        self.scalar_histories.append(kwargs)
        return {"uuid": uuid, "view_key": f"history-{uuid}", "created": True, "superseded": False}

    def list_scalar_history_views(
        self, *, subject_uuid: str, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return [v for v in self._history_views if v.get("subject_uuid") == subject_uuid]

    def retire_scalar_history(self, *, view_key: str) -> bool:
        self.retired_history_keys.append(view_key)
        return True

    def draw_scalar_history_entries(
        self, *, view_uuid: str, entries: list[dict[str, Any]],
    ) -> dict[str, int]:
        self.drawn_history_entries.append({"view_uuid": view_uuid, "entries": entries})
        return {"history_entries": len(entries)}


def _make_service(rows=None, state_views=None, history_views=None):
    source = FakeAssertionSource(rows or [])
    sink = FakeViewSink()
    if state_views:
        sink._state_views = state_views
    if history_views:
        sink._history_views = history_views
    svc = ScalarStateService(source, sink)
    return svc, sink


class _RecordingNeo4j:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def execute(self, query, params=None):
        self.calls.append({"query": query, "params": params or {}})
        return [{"drawn": len((params or {}).get("entries") or [])}]


@pytest.mark.unit
def test_repository_normalizes_history_entry_and_mapping_before_redraw():
    neo4j = _RecordingNeo4j()
    repo = ViewRepository(neo4j)
    entry = HistoryEntry(
        assertion_id="a1", operation="delta", value="17", value_json=17,
        valid_at="2023-08-11T00:00:00+00:00", episode_uuid="ep-1", turn_id="turn-1",
        evidence_tier="user", stated_span="17 postcards",
    )
    result = repo.draw_scalar_history_entries(view_uuid="view-1", entries=[entry])
    assert result == {"history_entries": 1}
    sent = neo4j.calls[0]["params"]["entries"]
    assert sent == [{"assertion_id": "a1", "ordinal": 0, "operation": "delta",
                     "valid_at": "2023-08-11T00:00:00+00:00"}]

    mapping = MappingProxyType({
        "assertion_id": "a2", "operation": "absolute",
        "valid_at": "2023-11-30T00:00:00+00:00",
    })
    repo.draw_scalar_history_entries(view_uuid="view-1", entries=[mapping])
    assert neo4j.calls[1]["params"]["entries"][0]["assertion_id"] == "a2"


@pytest.mark.unit
def test_repository_rejects_blank_history_id_before_destructive_redraw():
    neo4j = _RecordingNeo4j()
    repo = ViewRepository(neo4j)
    with pytest.raises(ValueError, match="blank assertion_id"):
        repo.draw_scalar_history_entries(
            view_uuid="view-1",
            entries=[{"assertion_id": " ", "operation": "delta", "valid_at": "2023-01-01"}],
        )
    assert neo4j.calls == []


# ---- rebuild_scalar_history ---------------------------------------------------

@pytest.mark.unit
def test_rebuild_history_delta_only():
    """Delta-only postcard slot produces a scalar_history View."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00",
                   episode_uuid="ep-1"),
        _assertion(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00",
                   episode_uuid="ep-2"),
    ])
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["written"] == 1
    assert result["retired"] == 0
    assert len(sink.scalar_histories) == 1
    h = sink.scalar_histories[0]
    assert h["attribute"] == "postcard_count"
    assert h["entry_count"] == 2
    assert h["payload_entry_count"] == 2
    assert h["omitted_entry_count"] == 0
    assert isinstance(h["audit"]["scalar_history_ops"], str)
    assert json.loads(h["audit"]["scalar_history_ops"]) == {"delta": 2}


@pytest.mark.unit
def test_exact_postcard_fixture_is_advisory_and_replay_idempotent_shape():
    rows = [
        _assertion(
            assertion_id="lme-01493427-a1", subject_uuid="lme-01493427", subject_display="user",
            operation="delta", value=17, stated_span="17 postcards since I started",
            episode_uuid="lme-01493427-ep-1", turn_id="turn-postcard-1",
            valid_at="2023-08-11T00:00:00+00:00",
        ),
        _assertion(
            assertion_id="lme-01493427-a2", subject_uuid="lme-01493427", subject_display="user",
            operation="delta", value=25, stated_span="25 postcards since I started",
            episode_uuid="lme-01493427-ep-2", turn_id="turn-postcard-2",
            valid_at="2023-11-30T00:00:00+00:00",
        ),
    ]
    svc, sink = _make_service(rows=rows)
    first = svc.rebuild_scalar_projections(
        "lme-01493427", namespace="postcards", history_enabled=True)
    second = svc.rebuild_scalar_projections(
        "lme-01493427", namespace="postcards", history_enabled=True)
    assert first["state"]["abstained"][0]["reason"] == "no_anchor"
    assert first["history"]["complete"] is True
    assert first["history"]["results"][0]["entry_count"] == 2
    assert len(sink.drawn_history_entries[0]["entries"]) == 2
    assert [e.value_json for e in sink.drawn_history_entries[0]["entries"]] == [17, 25]
    assert 42 not in [e.value_json for e in sink.drawn_history_entries[0]["entries"]]
    assert second["complete"] is True


@pytest.mark.unit
def test_rebuild_history_draws_entry_edges():
    """HISTORY_ENTRY edges are drawn after history View write."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
    ])
    svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert len(sink.drawn_history_entries) == 1
    assert len(sink.drawn_history_entries[0]["entries"]) == 1


@pytest.mark.unit
def test_rebuild_history_preserves_all_twenty_contributors_while_bounding_payload():
    rows = [
        _assertion(
            assertion_id=f"a{i:02d}", value=i,
            valid_at=f"2023-{(i // 2) + 1:02d}-{(i % 2) + 1:02d}T00:00:00+00:00",
        )
        for i in range(20)
    ]
    svc, sink = _make_service(rows=rows)
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["complete"] is True
    assert result["results"][0]["entry_count"] == 20
    assert result["results"][0]["payload_entry_count"] == 16
    assert result["results"][0]["omitted_entry_count"] == 4
    assert [e.assertion_id for e in sink.drawn_history_entries[0]["entries"]] == [
        f"a{i:02d}" for i in range(20)
    ]
    assert sink.scalar_histories[0]["entry_count"] == 20
    assert len(sink.scalar_histories[0]["entries"]) == 16


@pytest.mark.unit
def test_rebuild_history_reconciles_vanished_slot():
    """A current history View whose slot is absent from the rebuild is retired."""
    svc, sink = _make_service(
        rows=[],  # no assertions left
        history_views=[{
            "subject_uuid": "ent-postcards",
            "view_key": "old-history-key",
            "attribute": "postcard_count",
            "scope": "collection",
            "value_kind": "count",
            "unit": "postcards",
        }],
    )
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["written"] == 0
    assert result["retired"] == 1
    assert "old-history-key" in sink.retired_history_keys


@pytest.mark.unit
def test_rebuild_history_multiple_slots():
    """Multiple slots under the same entity each get their own history View."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", attribute="postcard_count", value=17,
                   valid_at="2023-08-11T00:00:00+00:00"),
        _assertion(assertion_id="a2", attribute="coin_count", value=50, value_kind="count",
                   valid_at="2023-06-01T00:00:00+00:00"),
    ])
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["written"] == 2
    attrs = {h["attribute"] for h in sink.scalar_histories}
    assert attrs == {"postcard_count", "coin_count"}


@pytest.mark.unit
def test_rebuild_history_with_as_of():
    """as_of filter threads through to the builder."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
        _assertion(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00"),
    ])
    result = svc.rebuild_scalar_history(
        "ent-postcards", namespace="test",
        as_of=datetime(2023, 10, 1, tzinfo=timezone.utc))
    assert result["written"] == 1
    assert sink.scalar_histories[0]["entry_count"] == 1


@pytest.mark.unit
def test_rebuild_history_no_assertions():
    """An entity with no assertions produces empty result."""
    svc, sink = _make_service(rows=[])
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["written"] == 0
    assert result["retired"] == 0
    assert len(sink.scalar_histories) == 0


@pytest.mark.unit
def test_malformed_source_time_keeps_existing_history_pending():
    svc, sink = _make_service(
        rows=[
            _assertion(assertion_id="good", valid_at="2023-08-11T00:00:00+00:00"),
            _assertion(assertion_id="bad", valid_at=None),
        ],
        history_views=[{
            "subject_uuid": "ent-postcards", "view_key": "known-good",
            "attribute": "postcard_count", "scope": "collection",
            "value_kind": "count", "unit": "postcards",
        }],
    )
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["complete"] is False
    assert result["written"] == 0
    assert result["abstained"][0]["malformed_assertion_ids"] == ["bad"]
    assert sink.retired_history_keys == []


# ---- rebuild_scalar_projections (coordinator) ---------------------------------

@pytest.mark.unit
def test_coordinator_state_only_when_history_disabled():
    """With history_enabled=False, only scalar_state is rebuilt."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", operation="absolute", value=37,
                   valid_at="2026-07-01T00:00:00+00:00"),
    ])
    result = svc.rebuild_scalar_projections(
        "ent-postcards", namespace="test", history_enabled=False)
    assert result["state"]["written"] == 1
    assert result["history"] is None
    assert len(sink.scalar_histories) == 0


@pytest.mark.unit
def test_coordinator_both_when_history_enabled():
    """With history_enabled=True, both projections are rebuilt."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", operation="absolute", value=37,
                   valid_at="2026-07-01T00:00:00+00:00"),
    ])
    result = svc.rebuild_scalar_projections(
        "ent-postcards", namespace="test", history_enabled=True)
    assert result["state"]["written"] == 1
    assert result["history"]["written"] == 1
    assert len(sink.scalar_states) == 1
    assert len(sink.scalar_histories) == 1
    assert result["complete"] is True


@pytest.mark.unit
def test_coordinator_delta_only_state_abstains_history_projects():
    """Delta-only: scalar_state abstains (no_anchor), scalar_history materializes."""
    svc, sink = _make_service(rows=[
        _assertion(assertion_id="a1", operation="delta", value=17,
                   valid_at="2023-08-11T00:00:00+00:00"),
        _assertion(assertion_id="a2", operation="delta", value=25,
                   valid_at="2023-11-30T00:00:00+00:00"),
    ])
    result = svc.rebuild_scalar_projections(
        "ent-postcards", namespace="test", history_enabled=True)
    # State abstains
    assert result["state"]["written"] == 0
    assert len(result["state"]["abstained"]) == 1
    assert result["state"]["abstained"][0]["reason"] == "no_anchor"
    # History projects
    assert result["history"]["written"] == 1
    assert len(sink.scalar_histories) == 1
    assert sink.scalar_histories[0]["entry_count"] == 2


@pytest.mark.unit
def test_coordinator_history_failure_does_not_fail_state():
    """If scalar_history rebuild raises, state result is still returned."""

    class BrokenHistorySink(FakeViewSink):
        def record_scalar_history(self, **kwargs):
            raise RuntimeError("boom")

    source = FakeAssertionSource([
        _assertion(assertion_id="a1", operation="absolute", value=37,
                   valid_at="2026-07-01T00:00:00+00:00"),
    ])
    sink = BrokenHistorySink()
    svc = ScalarStateService(source, sink)
    result = svc.rebuild_scalar_projections(
        "ent-postcards", namespace="test", history_enabled=True)
    assert result["state"]["written"] == 1
    assert result["history"]["error"] is True


# ---- sink without history methods (backward compat) ---------------------------

@pytest.mark.unit
def test_rebuild_history_skips_unsupported_sink():
    """A sink without record_scalar_history returns skipped=True."""

    class MinimalSink:
        def record_scalar_state(self, **kwargs): return {}
        def list_scalar_state_views(self, **kwargs): return []
        def retire_scalar_state(self, **kwargs): return False

    source = FakeAssertionSource([
        _assertion(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
    ])
    svc = ScalarStateService(source, MinimalSink())
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["skipped"] is True


# ---- edge drawing failure is non-fatal ----------------------------------------

@pytest.mark.unit
def test_history_entry_draw_failure_is_incomplete_and_retryable():
    """HISTORY_ENTRY draw failure never becomes a completed projection."""

    class FailDrawSink(FakeViewSink):
        def draw_scalar_history_entries(self, **kwargs):
            raise RuntimeError("edge draw failed")

    source = FakeAssertionSource([
        _assertion(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
    ])
    sink = FailDrawSink()
    svc = ScalarStateService(source, sink)
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["complete"] is False
    assert result["error"] is True
    assert result["written"] == 0
    assert result["failed_slots"][0]["slot_key"][-1] == "postcards"


# ---- entry count mismatch warning --------------------------------------------

@pytest.mark.unit
def test_entry_count_mismatch_is_incomplete_and_retryable():
    """A short contributor redraw fails closed instead of merely warning."""

    class ShortDrawSink(FakeViewSink):
        """Returns fewer drawn edges than entries requested."""
        def draw_scalar_history_entries(self, *, view_uuid, entries):
            self.drawn_history_entries.append({"view_uuid": view_uuid, "entries": entries})
            # Pretend only 0 edges were drawn (e.g. assertions not found)
            return {"history_entries": 0}

    source = FakeAssertionSource([
        _assertion(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
    ])
    sink = ShortDrawSink()
    svc = ScalarStateService(source, sink)
    result = svc.rebuild_scalar_history("ent-postcards", namespace="test")
    assert result["complete"] is False
    assert result["error"] is True
    assert "count mismatch" in result["failed_slots"][0]["error"]


@pytest.mark.unit
def test_merge_receipt_stays_pending_until_history_retry_succeeds():
    class FailOnceHistorySink(FakeViewSink):
        def __init__(self):
            super().__init__()
            self.fail_history = True

        def draw_scalar_history_entries(self, **kwargs):
            if self.fail_history:
                self.fail_history = False
                raise RuntimeError("edge store unavailable")
            return super().draw_scalar_history_entries(**kwargs)

    source = FakeAssertionSourceWithLifecycle([
        _assertion(assertion_id="a1", subject_uuid="absorbed", operation="absolute", value=5,
                   valid_at="2026-07-01T00:00:00+00:00"),
    ])
    sink = FailOnceHistorySink()
    svc = ScalarStateService(source, sink, scalar_history_enabled=True)
    first = svc.handle_merge(
        absorbed_uuid="absorbed", survivor_uuid="survivor", merge_op_id="merge-retry")
    assert first["history_errored"] is True
    assert source.receipts == set()

    second = svc.handle_merge(
        absorbed_uuid="absorbed", survivor_uuid="survivor", merge_op_id="merge-retry")
    assert second["history_errored"] is False
    assert ("merge-retry", "ENTITY_MERGE", None) in source.receipts


# ---- lifecycle + history integration tests ------------------------------------

class FakeAssertionSourceWithLifecycle(FakeAssertionSource):
    """Extends FakeAssertionSource with merge/unmerge/repair lifecycle methods."""

    def __init__(self, rows=None):
        super().__init__(rows)
        self.rebinds: list[dict[str, Any]] = []
        self.receipts: set[tuple[str, str, str | None]] = set()
        self.intents: set[tuple[str, str, str | None]] = set()
        self._pending_repairs: list[dict[str, Any]] = []

    def rebind_assertions(self, *, absorbed_uuid, survivor_uuid, merge_op_id, namespace=None):
        moved = 0
        for r in self._rows:
            if r.get("subject_uuid") == absorbed_uuid:
                self.rebinds.append({
                    "op": merge_op_id, "assertion_id": r.get("assertion_id"),
                    "from": absorbed_uuid, "to": survivor_uuid,
                })
                r["subject_uuid"] = survivor_uuid
                moved += 1
        return {"rebound": moved, "source_keys": []}

    def restore_rebound_assertions(self, *, merge_op_id, namespace=None):
        recs = [rb for rb in self.rebinds if rb["op"] == merge_op_id]
        from_uuids: set[str] = set()
        for rb in recs:
            for r in self._rows:
                if r.get("assertion_id") == rb["assertion_id"]:
                    r["subject_uuid"] = rb["from"]
                    from_uuids.add(rb["from"])
                    break
        self.rebinds = [rb for rb in self.rebinds if rb["op"] != merge_op_id]
        return {"restored": len(recs), "source_keys": [], "from_uuids": sorted(from_uuids)}

    def record_reconcile_receipt(self, *, operation_id, operation_kind, merge_op_id=None,
                                 namespace=None):
        self.receipts.add((operation_id, operation_kind, namespace))

    def record_reconcile_intent(self, *, operation_id, operation_kind, merge_op_id=None,
                                namespace=None):
        self.intents.add((operation_id, operation_kind, namespace))

    def pending_projection_repairs(self, *, operation_id=None, namespaces=None, limit=200):
        return list(self._pending_repairs[:limit])

    def mark_projection_repair_complete(self, repair_key):
        self._pending_repairs = [
            r for r in self._pending_repairs if r.get("repair_key") != repair_key]
        return True


def _make_lifecycle_service(rows=None, state_views=None, history_views=None,
                            scalar_history_enabled=False, pending_repairs=None):
    source = FakeAssertionSourceWithLifecycle(rows or [])
    if pending_repairs:
        source._pending_repairs = pending_repairs
    sink = FakeViewSink()
    if state_views:
        sink._state_views = state_views
    if history_views:
        sink._history_views = history_views
    svc = ScalarStateService(source, sink, scalar_history_enabled=scalar_history_enabled)
    return svc, sink, source


@pytest.mark.unit
def test_merge_retires_absorbed_history_views():
    """handle_merge retires absorbed entity's scalar_history Views alongside state Views."""
    svc, sink, _src = _make_lifecycle_service(
        rows=[
            _assertion(assertion_id="a1", subject_uuid="absorbed", value=5,
                       valid_at="2026-07-01T00:00:00+00:00"),
            _assertion(assertion_id="a2", subject_uuid="survivor",
                       operation="absolute", value=8,
                       valid_at="2026-08-01T00:00:00+00:00"),
        ],
        state_views=[{
            "subject_uuid": "absorbed", "view_key": "s-absorbed",
            "attribute": "postcard_count", "scope": "collection",
            "value_kind": "count", "unit": "postcards",
        }],
        history_views=[{
            "subject_uuid": "absorbed", "view_key": "h-absorbed",
            "attribute": "postcard_count", "scope": "collection",
            "value_kind": "count", "unit": "postcards",
        }],
        scalar_history_enabled=True,
    )
    result = svc.handle_merge(
        absorbed_uuid="absorbed", survivor_uuid="survivor", merge_op_id="op1")
    assert "s-absorbed" in sink.retired_state_keys
    assert "h-absorbed" in sink.retired_history_keys
    assert result["retired_history"] == 1


@pytest.mark.unit
def test_merge_with_history_enabled_rebuilds_both():
    """handle_merge rebuilds survivor with both state and history when history_enabled."""
    svc, sink, _src = _make_lifecycle_service(
        rows=[
            _assertion(assertion_id="a1", subject_uuid="survivor",
                       operation="absolute", value=37,
                       valid_at="2026-07-01T00:00:00+00:00"),
        ],
        scalar_history_enabled=True,
    )
    result = svc.handle_merge(
        absorbed_uuid="absorbed", survivor_uuid="survivor", merge_op_id="op1")
    rebuild = result["survivor_rebuild"]
    assert rebuild["state"]["written"] == 1
    assert rebuild["history"]["written"] == 1


@pytest.mark.unit
def test_merge_without_history_only_rebuilds_state():
    """handle_merge only rebuilds state when history disabled (coordinator passes through)."""
    svc, sink, _src = _make_lifecycle_service(
        rows=[
            _assertion(assertion_id="a1", subject_uuid="survivor",
                       operation="absolute", value=37,
                       valid_at="2026-07-01T00:00:00+00:00"),
        ],
        scalar_history_enabled=False,
    )
    result = svc.handle_merge(
        absorbed_uuid="absorbed", survivor_uuid="survivor", merge_op_id="op1")
    rebuild = result["survivor_rebuild"]
    assert rebuild["state"]["written"] == 1
    assert rebuild["history"] is None


@pytest.mark.unit
def test_unmerge_with_history_rebuilds_both():
    """handle_unmerge rebuilds all affected entities with both projections."""
    svc, sink, _src = _make_lifecycle_service(
        rows=[
            _assertion(assertion_id="a1", subject_uuid="absorbed",
                       operation="absolute", value=5,
                       valid_at="2026-07-01T00:00:00+00:00"),
            _assertion(assertion_id="a2", subject_uuid="survivor",
                       operation="absolute", value=8,
                       valid_at="2026-08-01T00:00:00+00:00"),
        ],
        scalar_history_enabled=True,
    )
    # First merge, then unmerge
    svc.handle_merge(
        absorbed_uuid="absorbed", survivor_uuid="survivor", merge_op_id="op1")
    sink.scalar_histories.clear()
    sink.scalar_states.clear()
    result = svc.handle_unmerge(
        survivor_uuid="survivor", absorbed_uuid="absorbed",
        merge_op_id="op1", unmerge_op_id="u-op1")
    # Both entities rebuilt with history
    assert len(result["rebuilt"]) >= 2
    for _uuid, rebuild in result["rebuilt"].items():
        assert rebuild["state"] is not None
        assert rebuild["history"] is not None


@pytest.mark.unit
def test_unmerge_receipt_stays_pending_until_retry():
    class FailOnceHistorySink(FakeViewSink):
        def __init__(self):
            super().__init__()
            self.fail_history = True

        def draw_scalar_history_entries(self, **kwargs):
            if self.fail_history:
                self.fail_history = False
                raise RuntimeError("transient history edge failure")
            return super().draw_scalar_history_entries(**kwargs)

    source = FakeAssertionSourceWithLifecycle([
        _assertion(assertion_id="a1", subject_uuid="survivor", operation="absolute", value=8,
                   valid_at="2026-07-01T00:00:00+00:00"),
    ])
    sink = FailOnceHistorySink()
    svc = ScalarStateService(source, sink, scalar_history_enabled=True)
    first = svc.handle_unmerge(
        survivor_uuid="survivor", absorbed_uuid="absorbed", merge_op_id="merge-1",
        unmerge_op_id="unmerge-retry")
    assert first["history_errored"] is True
    assert source.receipts == set()

    second = svc.handle_unmerge(
        survivor_uuid="survivor", absorbed_uuid="absorbed", merge_op_id="merge-1",
        unmerge_op_id="unmerge-retry")
    assert second["history_errored"] is False
    assert ("unmerge-retry", "ENTITY_UNMERGE", None) in source.receipts


@pytest.mark.unit
def test_repair_pending_deletions_with_history():
    """repair_pending_deletions uses the coordinator when history_enabled."""
    svc, sink, _src = _make_lifecycle_service(
        rows=[
            _assertion(assertion_id="a1", subject_uuid="ent-postcards",
                       operation="absolute", value=37,
                       valid_at="2026-07-01T00:00:00+00:00"),
        ],
        pending_repairs=[{
            "repair_key": "rk-1",
            "subject_uuid": "ent-postcards",
            "namespace": "test",
        }],
        scalar_history_enabled=True,
    )
    result = svc.repair_pending_deletions(namespaces=["test"], limit=10)
    assert len(result["repaired"]) == 1
    # Both projections were written
    assert len(sink.scalar_states) >= 1
    assert len(sink.scalar_histories) >= 1


@pytest.mark.unit
def test_repair_pending_deletions_always_rebuilds_history():
    """repair_pending_deletions always rebuilds history (even when flag is off) because
    deletions must clean up all projections to prevent stale deleted data from remaining
    recallable via scalar_history Views."""
    svc, sink, _src = _make_lifecycle_service(
        rows=[
            _assertion(assertion_id="a1", subject_uuid="ent-postcards",
                       operation="absolute", value=37,
                       valid_at="2026-07-01T00:00:00+00:00"),
        ],
        pending_repairs=[{
            "repair_key": "rk-1",
            "subject_uuid": "ent-postcards",
            "namespace": "test",
        }],
        scalar_history_enabled=False,
    )
    result = svc.repair_pending_deletions(namespaces=["test"], limit=10)
    assert len(result["repaired"]) == 1
    assert len(sink.scalar_states) >= 1
    # Deletions ALWAYS rebuild history for correctness (P1 fix: stale history on deleted data).
    assert len(sink.scalar_histories) >= 1


@pytest.mark.unit
def test_delete_repair_receipt_stays_pending_after_history_failure_then_converges():
    class FailOnceHistorySink(FakeViewSink):
        def __init__(self):
            super().__init__()
            self.fail_history = True

        def draw_scalar_history_entries(self, **kwargs):
            if self.fail_history:
                self.fail_history = False
                raise RuntimeError("transient history edge failure")
            return super().draw_scalar_history_entries(**kwargs)

    svc, sink, source = _make_lifecycle_service(
        rows=[_assertion(assertion_id="a1", subject_uuid="ent-postcards",
                         operation="absolute", value=37,
                         valid_at="2026-07-01T00:00:00+00:00")],
        pending_repairs=[{
            "repair_key": "delete-retry", "subject_uuid": "ent-postcards", "namespace": "test",
        }],
        scalar_history_enabled=False,
    )
    # Replace the sink after construction while retaining the source receipt seam.
    failing_sink = FailOnceHistorySink()
    svc = ScalarStateService(source, failing_sink, scalar_history_enabled=False)
    first = svc.repair_pending_deletions(namespaces=["test"], limit=10)
    assert first["repaired"] == [] and first["failed"]
    assert source._pending_repairs

    second = svc.repair_pending_deletions(namespaces=["test"], limit=10)
    assert second["repaired"] == ["delete-retry"]
    assert source._pending_repairs == []
