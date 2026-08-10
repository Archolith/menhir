"""Unit tests for M6 Phase 6 LLM session budget caps in IngestService."""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from menhir.services import IngestService


def _make_service() -> IngestService:
    """Build an IngestService with mock dependencies for budget-cap testing."""
    return IngestService(
        graphiti_client=MagicMock(),
        graph_adapter=MagicMock(),
        llm=MagicMock(),
    )


# ---------------------------------------------------------------------------
# 1. Returns None (allowed) when under budget
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_session_budget_returns_none_when_under_budget() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=5, llm_session_window_seconds=60)

    result = await svc._check_session_budget("ep-1", "session-a")
    assert result is None, "Expected None (allowed) when no prior calls recorded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_session_budget_allows_up_to_max_minus_one() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=3, llm_session_window_seconds=60)

    # Record 2 calls (limit is 3) — the third should still be allowed.
    for _ in range(2):
        result = await svc._check_session_budget("ep-1", "session-a")
        assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_session_budget_allows_exactly_at_limit() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=3, llm_session_window_seconds=60)

    # First 3 calls should all return None (allowed).
    for i in range(3):
        result = await svc._check_session_budget("ep-1", "session-a")
        assert result is None, f"Call {i+1} of 3 should be allowed"


# ---------------------------------------------------------------------------
# 2. Returns retry_after_s (float) when budget is exceeded
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_session_budget_returns_retry_when_exceeded() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=2, llm_session_window_seconds=60)

    # Consume the full budget.
    for _ in range(2):
        assert await svc._check_session_budget("ep-1", "session-a") is None

    # The next call should be rejected with a positive retry_after_s.
    retry = await svc._check_session_budget("ep-1", "session-a")
    assert retry is not None
    assert isinstance(retry, float)
    assert retry >= 1.0, "retry_after_s must be at least 1.0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_after_is_bounded_by_window() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=1, llm_session_window_seconds=30)

    assert await svc._check_session_budget("ep-1", "sess-1") is None

    retry = await svc._check_session_budget("ep-1", "sess-1")
    assert retry is not None
    assert retry <= 30.0, "retry_after_s should not exceed the window"


# ---------------------------------------------------------------------------
# 3. Rolling window expires old calls — budget resets after window passes
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_window_expires_old_calls() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=2, llm_session_window_seconds=10)

    # Patch monotonic so we can control time.
    fake_now = 1000.0

    with patch("menhir.services.ingest_queue.monotonic") as mock_mono:
        mock_mono.return_value = fake_now

        # Fill the budget.
        assert await svc._check_session_budget("ep-1", "sess-1") is None
        assert await svc._check_session_budget("ep-1", "sess-1") is None

        # Still within window — should be blocked.
        mock_mono.return_value = fake_now + 5.0
        retry = await svc._check_session_budget("ep-1", "sess-1")
        assert retry is not None

        # Advance past the window — old calls should expire.
        mock_mono.return_value = fake_now + 11.0
        result = await svc._check_session_budget("ep-1", "sess-1")
        assert result is None, "Budget should have reset after the window expired"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_window_expiry_frees_some_budget() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=2, llm_session_window_seconds=10)

    with patch("menhir.services.ingest_queue.monotonic") as mock_mono:
        # First call at t=1000.
        mock_mono.return_value = 1000.0
        assert await svc._check_session_budget("ep-1", "sess-1") is None

        # Second call at t=1005.
        mock_mono.return_value = 1005.0
        assert await svc._check_session_budget("ep-1", "sess-1") is None

        # At t=1009, first call is still in window (age 9 < 10), so blocked.
        mock_mono.return_value = 1009.0
        assert await svc._check_session_budget("ep-1", "sess-1") is not None

        # At t=1010, first call expires (age 10 >= 10), one slot opens.
        mock_mono.return_value = 1010.0
        result = await svc._check_session_budget("ep-1", "sess-1")
        assert result is None, "First call should have expired, freeing one slot"


# ---------------------------------------------------------------------------
# 4. configure() sets budget parameters correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_configure_sets_budget_max_calls() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=42)
    assert svc._budget_settings_max_calls == 42


@pytest.mark.unit
def test_configure_sets_budget_window_seconds() -> None:
    svc = _make_service()
    svc.configure(llm_session_window_seconds=120)
    assert svc._budget_settings_window_s == 120


@pytest.mark.unit
def test_configure_sets_max_per_job() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_enrichment_job=7)
    assert svc._budget_settings_max_per_job == 7


@pytest.mark.unit
def test_configure_enforces_minimum_of_one() -> None:
    svc = _make_service()
    svc.configure(
        max_llm_calls_per_session_window=0,
        llm_session_window_seconds=-5,
        max_llm_calls_per_enrichment_job=0,
    )
    assert svc._budget_settings_max_calls >= 1
    assert svc._budget_settings_window_s >= 1
    assert svc._budget_settings_max_per_job >= 1


@pytest.mark.unit
def test_configure_leaves_defaults_when_none() -> None:
    svc = _make_service()
    default_max = svc._budget_settings_max_calls
    default_window = svc._budget_settings_window_s
    default_per_job = svc._budget_settings_max_per_job

    svc.configure()  # No args — nothing should change.

    assert svc._budget_settings_max_calls == default_max
    assert svc._budget_settings_window_s == default_window
    assert svc._budget_settings_max_per_job == default_per_job


# ---------------------------------------------------------------------------
# 5. Different session_ids have independent budgets
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_independent_budgets_per_session_id() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=1, llm_session_window_seconds=60)

    # Session A exhausts its budget.
    assert await svc._check_session_budget("ep-1", "session-a") is None
    assert await svc._check_session_budget("ep-1", "session-a") is not None

    # Session B should still be allowed — independent budget.
    result = await svc._check_session_budget("ep-2", "session-b")
    assert result is None, "session-b should have its own independent budget"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_session_different_episodes_share_budget() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=1, llm_session_window_seconds=60)

    # Same session_id, different episode UUIDs — should share the session budget.
    assert await svc._check_session_budget("ep-1", "shared-session") is None
    retry = await svc._check_session_budget("ep-2", "shared-session")
    assert retry is not None, "Same session_id should share budget across episodes"


# ---------------------------------------------------------------------------
# 6. Budget check with no session_id uses episode_uuid as key
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_session_id_uses_episode_uuid_as_key() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=1, llm_session_window_seconds=60)

    # session_id=None — should use "episode:<uuid>" as the budget key.
    assert await svc._check_session_budget("ep-abc", None) is None
    retry = await svc._check_session_budget("ep-abc", None)
    assert retry is not None, "Same episode key should be rate-limited"

    # Verify the key was stored as "episode:ep-abc".
    assert "episode:ep-abc" in svc._session_llm_call_times


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_session_different_episodes_have_independent_budgets() -> None:
    svc = _make_service()
    svc.configure(max_llm_calls_per_session_window=1, llm_session_window_seconds=60)

    # Two different episode UUIDs with no session_id — independent keys.
    assert await svc._check_session_budget("ep-1", None) is None
    assert await svc._check_session_budget("ep-1", None) is not None  # blocked

    # ep-2 should still be allowed.
    result = await svc._check_session_budget("ep-2", None)
    assert result is None, "Different episode UUIDs with no session_id should be independent"
