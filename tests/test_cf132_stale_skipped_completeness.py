"""CF-132 — `rebuild_scalar_state` must not hardcode `complete: True` when a slot was stale-skipped.

Before this fix, when `record_scalar_state` reported `stale_skipped` the slot was excluded from
`desired_slots` (the loop `continue`s), so the reconcile loop RETIRED the existing View — but the
function still returned `"complete": True`. The result: the slot ended with no current View and no
pending retry marker, so `_rebuild_succeeded` / `_projection_complete` were satisfied and
`mark_projection_complete` cleared the durable marker -> permanent silent loss of a materialised
scalar slot on an LWW race the code already anticipates.

The fix mirrors the sibling method (`rebuild_scalar_history` computes `complete = not failed_slots and
not blocked_slots`): `"complete": not stale_skipped`.

Offline. The seam I assert against for the durable-marker consequence is `_rebuild_succeeded` gating
`mark_projection_complete` inside `bind_and_persist_typed_scalars` (typed_scalar_persistence.py), fed
by the REAL `ScalarStateService.rebuild_scalar_state` result — that is the exact call path that clears
the marker in production.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.services.scalar_state_service import ScalarStateService, _projection_complete
from menhir.services.typed_scalar_persistence import _rebuild_succeeded, bind_and_persist_typed_scalars
from menhir.services.typed_scalar_perception import (
    TypedScalarDecision,
    TypedScalarProposal,
)


def _state_row(**over: Any) -> dict[str, Any]:
    base = dict(
        assertion_id="a1", subject_uuid="ent-coins", subject_display="my coins",
        attribute="owned", scope="pre-1920", value_kind="count", unit="", operation="absolute",
        value=37, valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="user", episode_uuid="ep-1", binding_pending=False,
    )
    base.update(over)
    return base


def _history_row(**over: Any) -> dict[str, Any]:
    base = dict(
        assertion_id="a1", subject_uuid="ent-postcards", subject_display="my postcards",
        attribute="postcard_count", scope="collection", value_kind="count", unit="postcards",
        operation="delta", value=17, valid_at="2023-08-11T00:00:00+00:00",
        learned_at="2023-08-11T00:00:00+00:00", evidence_tier="user",
        episode_uuid="ep-1", turn_id="t1", stated_span="17 postcards",
        binding_pending=False,
    )
    base.update(over)
    return base


class _AssertionSource:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def materializable_assertions_for_entity(
        self, subject_uuid: str, *, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("subject_uuid") == subject_uuid]


class _ViewSink:
    def __init__(self, *, stale_skipped: bool = False) -> None:
        self.stale_skipped = stale_skipped
        self.records: list[dict[str, Any]] = []
        self.retired: list[str] = []
        self.history_records: list[dict[str, Any]] = []
        self.draw_fail = False

    def record_scalar_state(self, **kwargs: Any) -> dict[str, Any]:
        self.records.append(kwargs)
        if self.stale_skipped:
            return {"stale_skipped": True, "view_key": "vk-new"}
        return {"view_key": "vk-new", "uuid": "view-new", "created": True, "superseded": False}

    def list_scalar_state_views(
        self, *, subject_uuid: str, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        # Simulate a current View on the same slot: on a stale-skip it is NOT in desired_slots so it
        # gets retired (deliberate); on a clean rebuild it IS in desired_slots so it is kept.
        return [{"view_key": "vk-old", "attribute": "owned", "scope": "pre-1920",
                 "value_kind": "count", "unit": ""}]

    def retire_scalar_state(self, *, view_key: str) -> bool:
        self.retired.append(view_key)
        return True

    # scalar_history support (sibling test)
    def record_scalar_history(self, **kwargs: Any) -> dict[str, Any]:
        self.history_records.append(kwargs)
        return {"view_key": "h-1", "uuid": "view-h1", "created": True, "superseded": False}

    def draw_scalar_history_entries(
        self, *, view_uuid: str, entries: list[dict[str, Any]],
    ) -> dict[str, int]:
        if self.draw_fail:
            raise RuntimeError("draw failed")
        return {"history_entries": len(entries)}

    def list_scalar_history_views(
        self, *, subject_uuid: str, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return []


def _prop(**over: Any) -> TypedScalarProposal:
    base = dict(
        subject_text="my coins", attribute="owned", scope="pre-1920", value_kind="count",
        unit="", operation="absolute", value=37, stated_span="I own 37 coins",
        episode_uuid="ep-1", span_start=0, span_end=15, when="2026-07-01T00:00:00+00:00",
    )
    base.update(over)
    return TypedScalarProposal(**base)


def _decision(p: TypedScalarProposal) -> TypedScalarDecision:
    return TypedScalarDecision(
        source_key=p.source_key, committed=True, reason="unanimous", veto="commit",
        agreement=1.0, k=1, distribution={}, proposal=p,
    )


@pytest.mark.unit
def test_stale_skipped_slot_reports_complete_false():
    """THE FINDING: a rebuild where one slot comes back stale_skipped must report complete False."""
    svc = ScalarStateService(_AssertionSource([_state_row()]), _ViewSink(stale_skipped=True))
    res = svc.rebuild_scalar_state("ent-coins")
    assert res["complete"] is False
    assert len(res["stale_skipped"]) == 1
    assert res["stale_skipped"][0]["slot_key"][0] == "owned"


@pytest.mark.unit
def test_stale_skipped_does_not_clear_durable_retry_marker():
    """THE CONSEQUENCE (the point): with complete False, _rebuild_succeeded fails closed and
    mark_projection_complete is NOT called — the durable retry marker survives. Seam observed:
    bind_and_persist_typed_scalars -> _rebuild_succeeded -> mark_projection_complete, fed by the real
    ScalarStateService.rebuild_scalar_state."""
    sink = _ViewSink(stale_skipped=True)
    svc = ScalarStateService(_AssertionSource([_state_row()]), sink)
    marked: list[list[str]] = []
    out = bind_and_persist_typed_scalars(
        [_decision(_prop())],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-coins", "name": "my coins"}],
        record_assertion=lambda a: {"assertion_id": "a1", "binding_pending": False,
                                    "binding_mismatch": False, "created": True},
        rebuild_scalar_state=lambda su: svc.rebuild_scalar_state(su),
        mark_projection_complete=lambda ids: marked.append(ids),
    )
    assert out["bound"] == 1
    assert out["rebuilt"] == 0                              # rebuild did not succeed
    assert out["results"][0]["projection_incomplete"] is True
    assert marked == []                                     # durable marker survives


@pytest.mark.unit
def test_complete_false_fails_closed_in_the_gates():
    """The gates themselves: a stale-skipped rebuild result is not a completion proof, so the marker
    clear is blocked at either seam."""
    stale = {"complete": False, "stale_skipped": [{"slot_key": ["coins", "", "count", ""]}]}
    assert _rebuild_succeeded(stale) is False
    assert _projection_complete(stale) is False


@pytest.mark.unit
def test_clean_rebuild_reports_complete_true_and_clears_marker():
    """POSITIVE CONTROL: a clean rebuild (no stale_skipped) still reports complete True AND still
    clears the marker. Without this, hardcoding False would pass the two tests above."""
    sink = _ViewSink(stale_skipped=False)
    svc = ScalarStateService(_AssertionSource([_state_row()]), sink)
    res = svc.rebuild_scalar_state("ent-coins")
    assert res["complete"] is True and res["stale_skipped"] == []

    marked: list[list[str]] = []
    out = bind_and_persist_typed_scalars(
        [_decision(_prop())],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-coins", "name": "my coins"}],
        record_assertion=lambda a: {"assertion_id": "a1", "binding_pending": False,
                                    "binding_mismatch": False, "created": True},
        rebuild_scalar_state=lambda su: svc.rebuild_scalar_state(su),
        mark_projection_complete=lambda ids: marked.append(ids),
    )
    assert out["rebuilt"] == 1
    assert marked == [["a1"]]


@pytest.mark.unit
def test_history_sibling_computes_its_own_completeness():
    """Consistency with the sibling: rebuild_scalar_history derives completeness from
    failed_slots/blocked_slots, NOT from stale_skipped, and is unchanged by this fix."""
    # clean history rebuild -> complete True
    sink = _ViewSink()
    svc = ScalarStateService(_AssertionSource([_history_row()]), sink)
    assert svc.rebuild_scalar_history("ent-postcards", namespace="test")["complete"] is True
    # a history projection failure -> failed_slots drives complete False (its own seam, untouched)
    sink2 = _ViewSink()
    sink2.draw_fail = True
    svc2 = ScalarStateService(_AssertionSource([_history_row()]), sink2)
    res2 = svc2.rebuild_scalar_history("ent-postcards", namespace="test")
    assert res2["complete"] is False
    assert res2["failed_slots"]


@pytest.mark.unit
def test_stale_skipped_slot_view_still_retired():
    """The retirement on a stale-skipped slot is deliberate and must not change: the slot is excluded
    from desired_slots, so the existing View is retired with NO replacement — and now (with complete
    False) a pending retry marker is left so the slot is not lost forever."""
    sink = _ViewSink(stale_skipped=True)
    svc = ScalarStateService(_AssertionSource([_state_row()]), sink)
    res = svc.rebuild_scalar_state("ent-coins")
    assert sink.retired == ["vk-old"]
    assert res["complete"] is False
