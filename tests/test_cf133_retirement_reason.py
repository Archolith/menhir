"""CF-133 — the retirement-reason label compared a 4-tuple to a 5-tuple.

`vslot = _slot_of_view(view)` is a 4-tuple (attribute, scope, value_kind, unit), but the
`Expiry.slot_key` / `Abstention.slot_key` it was compared against are 5-tuples that include
`subject_uuid`. A 4-tuple never equals a 5-tuple, so BOTH conditions were permanently False
and `_reason` was the constant `"vanished"` for every retire. The fix compares
`tuple(slot_key[1:]) == vslot` (drop subject_uuid); site 2 uses `slot_key[1:5]` (drop
subject_uuid, keep unit).

Tests drive the real `rebuild_scalar_state` through the real fold so both coordinate systems
are built by the real normalizers (`slot_of` and `_slot_of_view`), proving the
normalization claim rather than trusting it.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.domain.scalar_history import MALFORMED_VALID_AT
from menhir.domain.scalar_state_fold import Abstention, FoldResult
from menhir.services.scalar_state_service import ScalarStateService, _blocked_slots


class _AssertionSource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def materializable_assertions_for_entity(
        self, subject_uuid: str, *, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("subject_uuid") == subject_uuid]


class _ViewSink:
    def __init__(self, view: dict[str, Any] | None) -> None:
        self._view = view
        self.retired: list[str] = []

    def record_scalar_state(self, **kwargs: Any) -> dict[str, Any]:
        return {"view_key": "vk-new", "uuid": "u-new", "created": True, "superseded": False}

    def list_scalar_state_views(
        self, *, subject_uuid: str, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return [self._view] if self._view else []

    def retire_scalar_state(self, *, view_key: str) -> bool:
        self.retired.append(view_key)
        return True


def _row(*, attribute: str, op: str, value: Any = 10,
         valid_at: str = "2020-01-01T00:00:00+00:00") -> dict[str, Any]:
    return dict(
        assertion_id=f"a-{op}", subject_uuid="ent-coins", subject_display="my coins",
        attribute=attribute, scope="collection", value_kind="count", unit="coins",
        operation=op, value=value, valid_at=valid_at, learned_at=valid_at,
        evidence_tier="user", episode_uuid="ep1", binding_pending=False,
    )


def _expiry_rows(*, attribute: str = "coins") -> list[dict[str, Any]]:
    # absolute then a LATER expire -> the fold yields an Expiry (no current state).
    return [
        _row(attribute=attribute, op="absolute", value=10, valid_at="2020-01-01T00:00:00+00:00"),
        _row(attribute=attribute, op="expire", value=10, valid_at="2022-01-01T00:00:00+00:00"),
    ]


def _abstain_rows(*, attribute: str = "coins") -> list[dict[str, Any]]:
    # a delta with no absolute anchor -> the fold yields an Abstention (NO_ANCHOR).
    return [_row(attribute=attribute, op="delta", value=5)]


def _view(*, attribute: str = "coins", key: str = "vk-1") -> dict[str, Any]:
    return {"view_key": key, "attribute": attribute, "scope": "collection",
            "value_kind": "count", "unit": "coins", "ss_value": 10}


def _retire_reasons(monkeypatch, rows: list[dict[str, Any]], view: dict[str, Any]) -> list[str]:
    audits: list[str] = []

    def fake_audit(event: str, state: str, **kwargs: Any) -> None:
        if event == "reconcile_retire":
            audits.append(state)

    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.audit", fake_audit)
    svc = ScalarStateService(_AssertionSource(rows), _ViewSink(view))
    res = svc.rebuild_scalar_state("ent-coins")
    assert res["complete"] is True
    assert svc._views.retired == [view.get("view_key")]  # the current View was reconciled away
    return audits


@pytest.mark.unit
def test_expiry_reason(monkeypatch):
    """Regression: an expiry-driven retirement must resolve to 'expiry', not the constant 'vanished'."""
    reasons = _retire_reasons(monkeypatch, _expiry_rows(), _view())
    assert reasons == ["expiry"]


@pytest.mark.unit
def test_abstain_reason(monkeypatch):
    """Regression: an abstention-driven retirement must resolve to 'abstain', not 'vanished'."""
    reasons = _retire_reasons(monkeypatch, _abstain_rows(), _view())
    assert reasons == ["abstain"]


@pytest.mark.unit
def test_vanished_when_neither_matches(monkeypatch):
    """POSITIVE CONTROL / MUTATION PIN: a slot matching NEITHER an expiry nor an abstention must
    still return 'vanished'. Note the pre-fix code returned the constant 'vanished' for EVERY
    input (4-tuple never equals 5-tuple), so this control alone was satisfied by the bug — the
    expiry/abstain tests above are what actually pin the fix."""
    # expiry rows produce an Expiry on (ent-coins, coins, ...) but the current View is on a
    # DIFFERENT slot (cars), so no expiry/abstention matches it.
    reasons = _retire_reasons(monkeypatch, _expiry_rows(), _view(attribute="cars", key="vk-cars"))
    assert reasons == ["vanished"]


@pytest.mark.unit
def test_case_whitespace_normalization_matches(monkeypatch):
    """Regression: an Expiry whose slot_key carries 'Coins' (unnormalized input) still matches a
    view whose attribute is 'coins' — proves slot_of and _slot_of_view normalize identically."""
    reasons = _retire_reasons(monkeypatch, _expiry_rows(attribute="Coins"), _view(attribute="coins"))
    assert reasons == ["expiry"]


@pytest.mark.unit
def test_blocked_slots_site_two_shape():
    """Regression (site 2): blocked_slots elements must be 4-tuples equal to slot_key[1:5] —
    dropping subject_uuid but KEEPING unit, so they share the module's view-slot coordinate
    system (pre-fix [:4] kept subject_uuid and dropped unit)."""
    slot_key = ("ent-coins", "coins", "collection", "count", "coins")
    result = FoldResult(abstentions=[
        Abstention(slot_key=slot_key, reason=MALFORMED_VALID_AT, subject_uuid="ent-coins"),
    ])
    blocked = _blocked_slots(result)
    assert len(blocked) == 1
    element = next(iter(blocked))
    assert len(element) == 4
    assert element == slot_key[1:5]
    assert element == ("coins", "collection", "count", "coins")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
