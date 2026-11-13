"""#604: a concurrent admission denial must not silently drop the audit record.

All of one subject's denials share a view key. A write that loses the compare-and-set raises
:class:`ViewWriteConflict`, and the admission-audit writer re-reads and retries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain import MemorySession
from menhir.infrastructure import memory_graph_adapter as mga
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.temporal_repository import TemporalRepository
from menhir.infrastructure.view_write_repository import (
    ViewWriteConflict,
    ViewWriteRepositoryMixin,
)
from menhir.services.ingest_intake import IngestIntakeMixin

_LOST = "View write refused: LOST UPDATE on view_key=k. Expected the current version ..."


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(mga, "_sleep", lambda _s: None)


class _NoRowsNeo4j:
    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        return []


def _refused_write(
    monkeypatch: pytest.MonkeyPatch, diagnosis: str, *, current: dict | None = None,
    eps: list[str] | None = None,
) -> None:
    repo = ViewWriteRepositoryMixin(neo4j=_NoRowsNeo4j())
    monkeypatch.setattr(repo, "_current_by_key", lambda key, view_class=None: current)
    monkeypatch.setattr(repo, "_diagnose_refusal", lambda **_: diagnosis)
    repo._write_version(
        kind="admission_audit", key="k", subject="s", name="n", summary="s", sig="sig",
        extra_props={}, namespace="agent-status", valid_at="2026-01-01T00:00:00+00:00",
        source="admission_gate", source_confidence=0.6, episode_uuids=eps or [],
        name_embedding=None,
    )


@pytest.mark.unit
def test_lost_compare_and_set_raises_a_typed_conflict(monkeypatch) -> None:
    with pytest.raises(ViewWriteConflict, match="LOST UPDATE"):
        _refused_write(monkeypatch, _LOST, eps=["e1"])


@pytest.mark.unit
def test_a_write_with_no_contributors_is_a_conflict_even_if_diagnosis_fails(monkeypatch) -> None:
    """With no contributors only the compare-and-set can drop the row; a failed diagnosis
    read (which returns the generic message) must not turn it into a non-retryable error."""
    with pytest.raises(ViewWriteConflict):
        _refused_write(monkeypatch, "FACT View write refused: every declared contributor ...")


@pytest.mark.unit
def test_evidence_refusals_stay_plain_value_errors(monkeypatch) -> None:
    with pytest.raises(ValueError) as excinfo:
        _refused_write(
            monkeypatch, "FACT View write refused: declared contributor UUIDs ...", eps=["e1"]
        )
    assert not isinstance(excinfo.value, ViewWriteConflict)


@pytest.mark.unit
def test_an_unchanged_write_whose_version_vanished_is_a_conflict(monkeypatch) -> None:
    """The same-signature path touches the version it read; no row means it was superseded or
    retired in between."""
    with pytest.raises(ViewWriteConflict, match="concurrent lifecycle change"):
        _refused_write(monkeypatch, _LOST, current={"uuid": "v1", "sig": "sig"})


class _Views:
    def __init__(self, failures: int, exc: Exception | None = None) -> None:
        self.failures = failures
        self.exc = exc or ViewWriteConflict(_LOST)
        self.calls: list[dict[str, Any]] = []

    def record(self, kind_name: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(payload)
        if len(self.calls) <= self.failures:
            raise self.exc
        return {"uuid": f"u{len(self.calls)}", "created": True}


def _adapter(views: _Views, *, turn_exists: bool = True) -> MemoryGraphAdapter:
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._views = views
    adapter.fetch_turn_evidence = lambda turn_id: {"turn_id": turn_id} if turn_exists else None
    return adapter


@pytest.mark.unit
def test_a_lost_race_is_retried_until_it_lands() -> None:
    views = _Views(failures=10)
    result = _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert result["created"] is True
    assert len(views.calls) == 11


@pytest.mark.unit
def test_exhausted_retries_raise_the_conflict() -> None:
    views = _Views(failures=10_000)
    with pytest.raises(ViewWriteConflict):
        _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert len(views.calls) == mga._ADMISSION_AUDIT_MAX_ATTEMPTS


@pytest.mark.unit
def test_the_time_budget_stops_retries(monkeypatch) -> None:
    clock = iter([0.0, 0.0, mga._ADMISSION_AUDIT_BUDGET_S + 1])
    monkeypatch.setattr(mga.time, "monotonic", lambda: next(clock))
    views = _Views(failures=10_000)
    with pytest.raises(ViewWriteConflict):
        _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert len(views.calls) == 2


@pytest.mark.unit
def test_a_retry_drops_a_turn_id_whose_turn_was_erased() -> None:
    """An erasure retires audit views naming the turn and strips the id; the conflict may be
    that retirement, so the retry must not write the erased id back."""
    views = _Views(failures=1)
    _adapter(views, turn_exists=False).record_admission_audit(
        subject="user-1", turn_evidence_uuid="turn-1", reason="denied"
    )
    assert [c["turn_evidence_uuid"] for c in views.calls] == ["turn-1", None]


@pytest.mark.unit
def test_a_retry_keeps_a_turn_id_whose_turn_still_exists() -> None:
    views = _Views(failures=1)
    _adapter(views).record_admission_audit(
        subject="user-1", turn_evidence_uuid="turn-1", reason="denied"
    )
    assert [c["turn_evidence_uuid"] for c in views.calls] == ["turn-1", "turn-1"]


@pytest.mark.unit
def test_other_refusals_are_not_retried() -> None:
    views = _Views(failures=1, exc=ValueError("FACT View write refused: evidence"))
    with pytest.raises(ValueError):
        _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert len(views.calls) == 1


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter) -> None:
        self.graph_adapter = adapter
        self._enrichment_enabled = False
        self._ensure_enrichment_worker = AsyncMock()
        self._enqueue_pending_episode = AsyncMock()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_reports_a_dropped_denial_audit_at_warning(caplog) -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = None
    adapter.record_admission_audit.side_effect = ViewWriteConflict("LOST UPDATE")
    session = MemorySession(
        session_id="session-1", user_id="user-1",
        started_at=datetime.now(timezone.utc), client_name="opencode",
    )
    caplog.set_level(logging.WARNING, logger="menhir.services.ingest_intake")

    # A user-tier claim with no turn evidence is denied; the ingest itself still succeeds.
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="hello", session=session, source="user",
    )

    adapter.record_admission_audit.assert_called_once()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "admission audit" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "session-1" in message and "user-1" not in message
    assert "no turn_evidence_uuid provided" in message


@pytest.mark.unit
def test_temporal_reports_a_dropped_audit_at_warning(caplog) -> None:
    neo4j = MagicMock()
    neo4j.execute.return_value = [{"uuid": "t1"}]
    recorder = MagicMock(side_effect=ViewWriteConflict("LOST UPDATE"))
    caplog.set_level(logging.WARNING, logger="menhir.infrastructure.temporal_repository")

    TemporalRepository(neo4j).create_temporal(
        content="dentist on friday", target_date="2027-02-16", source="user",
        namespace="ns-1", audit_recorder=recorder,
    )

    recorder.assert_called_once()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "admission audit" in r.getMessage()]
    assert len(warnings) == 1
    assert "ns-1" in warnings[0].getMessage()
