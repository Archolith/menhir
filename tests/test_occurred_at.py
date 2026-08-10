"""Tests for occurred_at → reference_time backdating passthrough."""

from __future__ import annotations

import pytest

from menhir.services.ingest_service import _parse_occurred_at


# ---------------------------------------------------------------------------
# _parse_occurred_at unit tests
# ---------------------------------------------------------------------------

class TestParseOccurredAt:
    def test_none_returns_none(self):
        assert _parse_occurred_at(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_occurred_at("") is None

    def test_valid_iso_with_tz(self):
        from datetime import datetime, timezone
        dt = _parse_occurred_at("2023-07-14T08:30:00+00:00")
        assert dt is not None
        assert dt.year == 2023
        assert dt.month == 7
        assert dt.day == 14
        assert dt.tzinfo is not None

    def test_valid_iso_naive_gets_utc(self):
        from datetime import timezone
        dt = _parse_occurred_at("2023-07-14T08:30:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_string_returns_none(self):
        assert _parse_occurred_at("not-a-date") is None

    def test_lme_style_date_string(self):
        # LME haystack_dates are "2023/07/14 (Fri) 08:30" — not directly ISO,
        # but the plan says parse failures fall back to None gracefully.
        result = _parse_occurred_at("2023/07/14 (Fri) 08:30")
        assert result is None  # not ISO; falls back to None without crash


# ---------------------------------------------------------------------------
# Integration: queue_episode_for_enrichment persists reference_time
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_occurred_at_persisted_as_reference_time(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
):
    """occurred_at ISO string → reference_time stored on the Episodic row."""
    from menhir.domain.session import new_session
    from menhir.services.ingest_service import IngestService

    session = new_session(user_id="test-user")
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graph_adapter=adapter,
        graphiti_client=stub_graphiti_client,
        llm=stub_llm_adapter,
    )

    async def _noop_enqueue(uuid: str) -> None:
        pass

    monkeypatch.setattr(service, "_ensure_enrichment_worker", lambda: _noop_enqueue(""))
    monkeypatch.setattr(service, "_enqueue_pending_episode", _noop_enqueue)

    result = await service.queue_episode_for_enrichment(
        "Alice moved to Seattle in July 2023",
        session,
        "unit-test",
        occurred_at="2023-07-14T00:00:00+00:00",
    )
    assert result.status.value == "queued"
    row = adapter.pending_episode_rows[result.episode_id]
    ref = row["reference_time"]
    assert ref is not None
    assert ref.year == 2023
    assert ref.month == 7
    assert ref.day == 14


@pytest.mark.unit
@pytest.mark.asyncio
async def test_occurred_at_none_keeps_reference_time_none(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
):
    """occurred_at=None → reference_time=None; live-ingest back-compat unchanged."""
    from menhir.domain.session import new_session
    from menhir.services.ingest_service import IngestService

    session = new_session(user_id="test-user")
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graph_adapter=adapter,
        graphiti_client=stub_graphiti_client,
        llm=stub_llm_adapter,
    )

    async def _noop_enqueue(uuid: str) -> None:
        pass

    monkeypatch.setattr(service, "_ensure_enrichment_worker", lambda: _noop_enqueue(""))
    monkeypatch.setattr(service, "_enqueue_pending_episode", _noop_enqueue)

    result = await service.queue_episode_for_enrichment(
        "Normal live-ingest episode",
        session,
        "unit-test",
    )
    assert result.status.value == "queued"
    row = adapter.pending_episode_rows[result.episode_id]
    assert row["reference_time"] is None
