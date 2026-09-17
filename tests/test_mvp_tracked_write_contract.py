"""#118 local-MVP contract pins; backend fakes are not live stdio/E2E evidence."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from menhir.mcp import formatters

track = importlib.import_module("menhir.mcp.tools.ingest.add_memory_and_track")
pytestmark = pytest.mark.unit


@pytest.fixture
def tracked_backend(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    backend = SimpleNamespace(
        episode_id=str(uuid4()),
        queue_episode=AsyncMock(),
        fetch_episode_processing=AsyncMock(return_value={"processing_state": "PENDING"}),
        get_queue_depth=AsyncMock(return_value=1),
    )
    backend.queue_episode.return_value = {"status": "queued", "episode_id": backend.episode_id}
    session = SimpleNamespace(user_id="operator", session_id="local-mvp-session")
    monkeypatch.setattr(track.AddMemoryAndTrackTool, "get_backend", lambda _self: backend)
    monkeypatch.setattr(track, "get_mcp_session", lambda: session)
    # Unrelated queue-wide diagnostics are outside this episode contract. Keep the real
    # endpoint -> collector -> formatter bindings, not a mocked status result.
    monkeypatch.setattr(track, "_queue_summary", AsyncMock(return_value="queue_status: test"))
    return backend


def _guidance(output: str) -> str:
    matches = [line for line in output.splitlines() if line.startswith("guidance:")]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("state", ["PENDING", "ENRICHING", "QUEUED", "UNKNOWN", "", "NEW_STATE"])
@pytest.mark.parametrize("renderer", [formatters._format_episode_status, formatters._format_episode_watch])
def test_incomplete_observations_never_recommend_another_write(state, renderer) -> None:
    output = renderer(
        episode_uuid="existing-episode", row={"processing_state": state}, history=[], timed_out=True,
    )
    guidance = _guidance(output)
    assert "get_enrichment_status(episode_uuid=<episode_id>, wait=True)" in guidance
    assert "same episode and namespace" in guidance
    assert "does not cancel" in guidance
    assert "Do not submit this memory again" in guidance
    assert "do not broaden permissions" in guidance
    assert "add_memory_and_track" not in guidance
    assert "will finish" not in guidance
    assert "p95" not in guidance
    assert "status: READY" not in output


@pytest.mark.parametrize("renderer", [formatters._format_episode_status, formatters._format_episode_watch])
@pytest.mark.parametrize("timed_out", [False, True])
def test_failed_observation_is_not_permission_to_resubmit(renderer, timed_out: bool) -> None:
    output = renderer(
        episode_uuid="existing-episode", row={"processing_state": "FAILED"}, history=[], timed_out=timed_out,
    )
    guidance = _guidance(output)
    assert "authorized retry/repair on the existing episode" in guidance
    assert "not a new write" in guidance
    assert "not proof" in guidance
    assert "safe to retry" not in guidance


@pytest.mark.parametrize("renderer", [formatters._format_episode_status, formatters._format_episode_watch])
def test_ready_observation_is_not_a_retrieval_guarantee(renderer) -> None:
    output = renderer(
        episode_uuid="existing-episode", row={"processing_state": "READY"}, history=[], timed_out=False,
    )
    assert "status: READY" in output
    assert "timed_out: False" in output
    guidance = _guidance(output)
    assert "not a guarantee" in guidance
    assert "Verify retrieval separately" in guidance


@pytest.mark.parametrize("renderer", [formatters._format_episode_status, formatters._format_episode_watch])
def test_missing_status_does_not_infer_write_failure(renderer) -> None:
    output = renderer(episode_uuid="existing-episode", row=None, history=[], timed_out=False)
    assert "episode_id: existing-episode" in output
    assert "status: not_found" in output
    assert "does not establish that a write failed" in _guidance(output)
    assert "status: FAILED" not in output
    assert "status: READY" not in output


@pytest.mark.asyncio
async def test_endpoint_timeout_then_observation_does_not_queue_twice(tracked_backend) -> None:
    backend = tracked_backend
    output = await track.AddMemoryAndTrackTool().endpoint(
        "The billing service uses PostgreSQL 16.", namespace="billing", timeout_s=0,
    )
    assert f"episode_id: {backend.episode_id}" in output
    assert "status: PENDING" in output
    assert "timed_out: True" in output
    assert "get_enrichment_status" in _guidance(output)
    backend.queue_episode.assert_awaited_once()
    assert backend.queue_episode.call_args.kwargs["namespace"] == "billing"

    # Continue through the production status collector used by the read-only status tool.
    # This does not test MCP framing or the status tool's ownership guard.
    backend.fetch_episode_processing.return_value = {"processing_state": "READY"}
    row, history, timed_out = await formatters._collect_episode_status(
        backend, backend.episode_id, timeout_s=0, poll_interval_s=0.05,
    )
    followup = formatters._format_episode_status(
        episode_uuid=backend.episode_id, row=row, history=history, timed_out=timed_out,
    )
    assert "status: READY" in followup
    assert not timed_out
    backend.queue_episode.assert_awaited_once()
    assert all(call.args == (backend.episode_id,) for call in backend.fetch_episode_processing.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["READY", "FAILED"])
async def test_endpoint_terminal_states_are_explained_without_timeout(tracked_backend, state: str) -> None:
    tracked_backend.fetch_episode_processing.return_value = {"processing_state": state}
    output = await track.AddMemoryAndTrackTool().endpoint("Durable fact", timeout_s=0)
    assert f"status: {state}" in output
    assert "timed_out: False" in output
    assert _guidance(output)
    tracked_backend.queue_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_acceptance_tracking_failure_preserves_receipt_and_redacts_error(tracked_backend, caplog) -> None:
    secret_error = "backend credentials https://user:private-password@example.invalid"
    tracked_backend.fetch_episode_processing.side_effect = RuntimeError(secret_error)
    output = await track.AddMemoryAndTrackTool().endpoint("Durable fact", timeout_s=0)
    assert f"episode_id: {tracked_backend.episode_id}" in output
    assert "write_status: accepted" in output
    assert "tracking_status: UNAVAILABLE" in output
    assert "status: UNKNOWN" in output
    assert "Do not submit this memory again" in _guidance(output)
    assert "status: READY" not in output
    assert "status: FAILED" not in output
    assert "timed_out:" not in output  # No timeout was established.
    assert "steps:" not in output  # No observation means no invented counts.
    assert secret_error not in output
    assert secret_error not in caplog.text
    tracked_backend.queue_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_failure_does_not_claim_acceptance_or_start_tracking(tracked_backend) -> None:
    tracked_backend.queue_episode.return_value = {"status": "failed"}
    output = await track.AddMemoryAndTrackTool().endpoint("Durable fact", timeout_s=0)
    assert output == "Failed to queue memory."
    tracked_backend.fetch_episode_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracking_cancellation_still_propagates(tracked_backend) -> None:
    tracked_backend.fetch_episode_processing.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await track.AddMemoryAndTrackTool().endpoint("Durable fact", timeout_s=0)
    tracked_backend.queue_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_context_is_forwarded_unchanged(tracked_backend) -> None:
    tracked_backend.fetch_episode_processing.return_value = {"processing_state": "READY"}
    await track.AddMemoryAndTrackTool().endpoint(
        "Durable fact", source="manual", namespace="billing", timeout_s=0,
        diff="+postgres = 16", turn_evidence_uuid="turn-1",
    )
    tracked_backend.queue_episode.assert_awaited_once_with(
        "Durable fact", user_id="operator", session_id="local-mvp-session", source="manual",
        namespace="billing", diff="+postgres = 16", turn_evidence_uuid="turn-1",
    )


def test_formatter_preserves_existing_fields_and_does_not_mutate_observation() -> None:
    row = {"processing_state": "ENRICHING", "processing_stage": "extract", "processing_progress": 25.0}
    history = [{"state": "PENDING", "attempts": 1, "queue_depth": 2, "processing_error": None}]
    before = deepcopy((row, history))
    output = formatters._format_episode_status(
        episode_uuid="existing-episode", row=row, history=history, timed_out=True,
    )
    assert "status: ENRICHING\nstage: extract\n" in output
    assert "progress: 25.0" in output
    assert "updates: 1" in output
    assert "history:\n  [1] state=PENDING" in output
    assert (row, history) == before


def test_tracked_write_signature_does_not_grow_permissions_or_invent_options() -> None:
    expected = {"text", "source", "timeout_s", "poll_interval_s", "diff", "turn_evidence_uuid", "namespace"}
    assert set(inspect.signature(track.add_memory_and_track).parameters) == expected
    assert set(inspect.signature(track.AddMemoryAndTrackTool.endpoint).parameters) == expected | {"self"}
    assert track.AddMemoryAndTrackTool.oauth_scopes == ("menhir:write",)
    assert not track.AddMemoryAndTrackTool.read_only_hint


@pytest.mark.asyncio
async def test_post_acceptance_formatter_failure_preserves_receipt(tracked_backend, monkeypatch, caplog) -> None:
    tracked_backend.fetch_episode_processing.return_value = {"processing_state": "READY"}
    secret_error = "malformed diagnostic with private-provider-token"

    def broken_formatter(**_kwargs):
        raise ValueError(secret_error)

    monkeypatch.setattr(track, "_format_episode_status", broken_formatter)
    output = await track.AddMemoryAndTrackTool().endpoint("Durable fact", timeout_s=0)
    assert f"episode_id: {tracked_backend.episode_id}" in output
    assert "write_status: accepted" in output
    assert "tracking_status: UNAVAILABLE" in output
    assert "status: UNKNOWN" in output
    assert secret_error not in output
    assert secret_error not in caplog.text
    tracked_backend.queue_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_diagnostic_failure_keeps_observed_episode_status(tracked_backend, monkeypatch, caplog) -> None:
    tracked_backend.fetch_episode_processing.return_value = {"processing_state": "READY"}
    secret_error = "queue diagnostic with private-provider-token"
    monkeypatch.setattr(track, "_queue_summary", AsyncMock(side_effect=ValueError(secret_error)))
    output = await track.AddMemoryAndTrackTool().endpoint("Durable fact", timeout_s=0)
    assert f"episode_id: {tracked_backend.episode_id}" in output
    assert "status: READY" in output
    assert "timed_out: False" in output
    assert "queue_status: UNAVAILABLE" in output
    assert "not a guarantee" in _guidance(output)
    assert secret_error not in output
    assert secret_error not in caplog.text
    tracked_backend.queue_episode.assert_awaited_once()
