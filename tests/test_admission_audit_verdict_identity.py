"""#622/#623 unit checks: one record per verdict, and the turn id goes through the guarded write."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure import memory_graph_adapter as mga
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.view_models import AdmissionAuditKind
from menhir.infrastructure.view_write_repository import (
    ViewWriteConflict,
    ViewWriteRepositoryMixin,
)

_PAYLOAD = {"requested_source": "user", "effective_source": "agent", "granted": False,
            "turn_evidence_uuid": None, "reason": "no turn_evidence_uuid provided"}


@pytest.mark.unit
def test_identical_verdicts_have_different_signatures() -> None:
    kind = AdmissionAuditKind()
    assert kind.signature({**_PAYLOAD, "verdict_id": "a"}) != kind.signature(
        {**_PAYLOAD, "verdict_id": "b"})


@pytest.mark.unit
def test_a_verdicts_signature_is_stable_and_not_derived_from_the_turn() -> None:
    kind = AdmissionAuditKind()
    sig = kind.signature({**_PAYLOAD, "verdict_id": "v1", "turn_evidence_uuid": "turn-1"})
    assert sig == kind.signature({**_PAYLOAD, "verdict_id": "v1", "turn_evidence_uuid": None})
    assert "turn-1" not in sig


class _Views:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[dict[str, Any]] = []

    def record(self, kind_name: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(payload)
        if len(self.calls) <= self.failures:
            raise ViewWriteConflict("LOST UPDATE")
        return {"uuid": "u", "created": True}


@pytest.mark.unit
def test_retries_share_a_verdict_id_and_calls_do_not(monkeypatch) -> None:
    monkeypatch.setattr(mga, "_sleep", lambda _s: None)
    views = _Views(failures=2)
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._views = views
    adapter.fetch_turn_evidence = lambda turn_id: None

    adapter.record_admission_audit(subject="user-1", reason="denied")
    adapter.record_admission_audit(subject="user-1", reason="denied")

    ids = [c["verdict_id"] for c in views.calls]
    assert len(ids) == 4 and all(ids)
    assert ids[0] == ids[1] == ids[2]
    assert ids[3] != ids[0]


def _captured_write(monkeypatch, kind_name: str, **payload: Any) -> dict[str, Any]:
    repo = ViewWriteRepositoryMixin(neo4j=None)
    captured: dict[str, Any] = {}

    def fake_write_version(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"uuid": "u"}

    monkeypatch.setattr(repo, "_write_version", fake_write_version)
    repo.record(kind_name, subject="user-1", namespace="agent-status", **payload)
    return captured


@pytest.mark.unit
def test_the_audit_turn_id_is_written_only_through_the_guard(monkeypatch) -> None:
    """write_props alone would SET the id unconditionally; it must reach the writer as the
    guarded parameter instead."""
    captured = _captured_write(
        monkeypatch, "admission_audit", **{**_PAYLOAD, "turn_evidence_uuid": "turn-1",
                                           "verdict_id": "v1"})
    assert "turn_evidence_uuid" not in captured["extra_props"]
    assert captured["guarded_turn_evidence_uuid"] == "turn-1"
    assert captured["extra_props"]["admission_verdict_id"] == "v1"


@pytest.mark.unit
def test_an_audit_without_a_turn_has_no_guard(monkeypatch) -> None:
    captured = _captured_write(monkeypatch, "admission_audit", **{**_PAYLOAD, "verdict_id": "v1"})
    assert captured["guarded_turn_evidence_uuid"] is None
    assert "turn_evidence_uuid" not in captured["extra_props"]


@pytest.mark.unit
def test_other_kinds_are_unguarded(monkeypatch) -> None:
    captured = _captured_write(monkeypatch, "counter", counter="denials", value=1.0)
    assert captured["guarded_turn_evidence_uuid"] is None
