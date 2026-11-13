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
from menhir.infrastructure.memory_graph_adapter import (
    _ADMISSION_AUDIT_ATTEMPTS,
    MemoryGraphAdapter,
)
from menhir.infrastructure.view_write_repository import (
    ViewWriteConflict,
    ViewWriteRepositoryMixin,
)
from menhir.services.ingest_intake import IngestIntakeMixin


class _NoRowsNeo4j:
    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        return []


def _refused_write(monkeypatch: pytest.MonkeyPatch, diagnosis: str) -> None:
    repo = ViewWriteRepositoryMixin(neo4j=_NoRowsNeo4j())
    monkeypatch.setattr(repo, "_current_by_key", lambda key, view_class=None: None)
    monkeypatch.setattr(repo, "_diagnose_refusal", lambda **_: diagnosis)
    repo._write_version(
        kind="admission_audit", key="k", subject="s", name="n", summary="s", sig="sig",
        extra_props={}, namespace="agent-status", valid_at="2026-01-01T00:00:00+00:00",
        source="admission_gate", source_confidence=0.6, episode_uuids=[], name_embedding=None,
    )


@pytest.mark.unit
def test_lost_compare_and_set_raises_a_typed_conflict(monkeypatch) -> None:
    with pytest.raises(ViewWriteConflict, match="LOST UPDATE"):
        _refused_write(
            monkeypatch,
            "View write refused: LOST UPDATE on view_key=k. Expected the current version ...",
        )


@pytest.mark.unit
def test_other_refusals_stay_plain_value_errors(monkeypatch) -> None:
    with pytest.raises(ValueError) as excinfo:
        _refused_write(monkeypatch, "FACT View write refused: declared contributor UUIDs ...")
    assert not isinstance(excinfo.value, ViewWriteConflict)


class _Views:
    def __init__(self, failures: int, exc: Exception | None = None) -> None:
        self.failures = failures
        self.exc = exc or ViewWriteConflict("View write refused: LOST UPDATE on view_key=k")
        self.calls = 0

    def record(self, kind_name: str, **payload: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return {"uuid": f"u{self.calls}", "created": True}


def _adapter(views: _Views) -> MemoryGraphAdapter:
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._views = views
    return adapter


@pytest.mark.unit
def test_a_lost_race_is_retried_until_it_lands() -> None:
    views = _Views(failures=_ADMISSION_AUDIT_ATTEMPTS - 1)
    result = _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert result["created"] is True
    assert views.calls == _ADMISSION_AUDIT_ATTEMPTS


@pytest.mark.unit
def test_exhausted_retries_raise_the_conflict() -> None:
    views = _Views(failures=_ADMISSION_AUDIT_ATTEMPTS)
    with pytest.raises(ViewWriteConflict):
        _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert views.calls == _ADMISSION_AUDIT_ATTEMPTS


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
    assert "user-1" in warnings[0].getMessage()
    assert "no turn_evidence_uuid provided" in warnings[0].getMessage()


@pytest.mark.unit
def test_other_refusals_are_not_retried() -> None:
    views = _Views(failures=1, exc=ValueError("FACT View write refused: evidence"))
    with pytest.raises(ValueError):
        _adapter(views).record_admission_audit(subject="user-1", reason="downgraded")
    assert views.calls == 1
