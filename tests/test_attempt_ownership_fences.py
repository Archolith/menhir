"""Attempt-ownership fences on terminal episode writes (offline).

An attempt's token is (owner, claim instant). Owner alone lets a stale attempt that ran in the
SAME worker finalize a newer claim of the same row. These tests pin:

* the query text/params of mark_episode_ready / mark_episode_failed with and without the claim
  instant (fence present exactly when an instant is supplied; legacy text otherwise);
* that every attempt-owned ready/failed write in enrichment_steps supplies the instant.

The real-Neo4j counterexamples live in tests/test_attempt_ownership_fences_live.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from menhir.domain.models import ProcessingState
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.services import enrichment_steps
from menhir.services.enrichment_steps import (
    EnrichmentContext,
    handle_enrichment_failure,
    run_preflight_rejection,
    stamp_and_finalize,
    try_reconcile_existing,
)

pytestmark = pytest.mark.unit

FENCE_SECONDS = "n.processing_started_at.epochSeconds = datetime($claim_started_at).epochSeconds"
FENCE_NANOS = "n.processing_started_at.nanosecond = datetime($claim_started_at).nanosecond"
TRANSIENT_ONLY_GUARD = "NOT $transient_requeue OR ($claim_started_at IS NOT NULL"
INSTANT = "2026-01-01T00:00:00.123456789Z"


class _RecordingNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query, params=None, **kwargs):
        self.calls.append((query, dict(params or {})))
        return [{"updated": 1}]


def _repo() -> tuple[EpisodeLifecycleRepository, _RecordingNeo4j]:
    repo = EpisodeLifecycleRepository()
    fake = _RecordingNeo4j()
    repo.neo4j = fake
    return repo, fake


def _ready(**extra) -> tuple[str, dict[str, Any]]:
    repo, fake = _repo()
    repo.mark_episode_ready(
        "ep-1", worker_id="w-1", resolved_episode_uuid="r", nodes_touched=1, edges_touched=2, **extra
    )
    return fake.calls[0]


def _failed(**extra) -> tuple[str, dict[str, Any]]:
    repo, fake = _repo()
    repo.mark_episode_failed("ep-1", "boom", worker_id="w-1", **extra)
    return fake.calls[0]


def _balanced(query: str) -> bool:
    return query.count("(") == query.count(")")


# ----------------------------------------------------------------------------- mark_episode_ready


def test_ready_with_claim_instant_fences_on_both_halves_of_the_instant() -> None:
    """Fails if either half of the epochSeconds+nanosecond fence is dropped from the owner
    branch, or the instant is not bound as a parameter."""
    query, params = _ready(claim_started_at=INSTANT)

    assert FENCE_SECONDS in query and FENCE_NANOS in query
    assert "n.processing_started_at IS NOT NULL" in query
    assert "n.processing_owner = $worker_id" in query
    assert params["claim_started_at"] == INSTANT
    assert _balanced(query)


def test_ready_without_claim_instant_has_the_legacy_text_and_no_param() -> None:
    """Fails if the fence leaks into the legacy call (older callers pass no instant and must
    keep the owner-only predicate byte for byte)."""
    query, params = _ready()

    assert "claim_started_at" not in query
    assert "epochSeconds" not in query and "nanosecond" not in query
    assert "processing_started_at" not in query.split("SET", 1)[0]
    assert "($worker_id IS NULL OR (n.processing_state = 'ENRICHING'" in query
    assert "n.processing_owner = $worker_id))" in query
    assert "claim_started_at" not in params
    assert _balanced(query)


def test_ready_instant_without_owner_does_not_add_a_fence() -> None:
    """The fence lives inside the owner branch only; an unowned write (worker_id None) has no
    owner branch to hang it on, and must not send an unused parameter."""
    repo, fake = _repo()
    repo.mark_episode_ready(
        "ep-1", worker_id=None, resolved_episode_uuid="r", nodes_touched=0, edges_touched=0,
        claim_started_at=INSTANT,
    )
    query, params = fake.calls[0]
    assert "claim_started_at" not in query
    assert "claim_started_at" not in params


# ---------------------------------------------------------------------------- mark_episode_failed


def test_failed_with_instant_fences_the_non_transient_owner_branch() -> None:
    """Fails if a non-transient failure with an instant is not fenced on it (a stale attempt
    could then fail a newer claim), or if the transient-only guard is kept on this path."""
    query, params = _failed(claim_started_at=INSTANT)

    assert FENCE_SECONDS in query and FENCE_NANOS in query
    assert TRANSIENT_ONLY_GUARD not in query
    assert params["claim_started_at"] == INSTANT
    assert params["transient_requeue"] is False
    assert _balanced(query)


def test_failed_without_instant_keeps_the_legacy_text() -> None:
    """Fails if the legacy (no-instant) text changes: the instant is only demanded under the
    transient_requeue guard, and a plain owner failure must stay owner-only."""
    query, params = _failed()

    assert TRANSIENT_ONLY_GUARD in query
    # The only place the instant is mentioned is inside the transient-requeue guard.
    guard_start = query.index(TRANSIENT_ONLY_GUARD)
    assert query.count("$claim_started_at") == query.count(
        "$claim_started_at", guard_start, query.index("SET", guard_start)
    )
    assert params["claim_started_at"] is None
    assert _balanced(query)


def test_failed_transient_requeue_text_is_the_same_with_or_without_the_instant() -> None:
    """The transient path already required the instant; supplying it must not change the text."""
    with_instant = _failed(transient_requeue=True, claim_started_at=INSTANT)
    without = _failed(transient_requeue=True)

    assert with_instant[0] == without[0]
    assert TRANSIENT_ONLY_GUARD in with_instant[0]
    assert with_instant[1]["claim_started_at"] == INSTANT


# ------------------------------------------------------------------------- enrichment_steps sites


def _ctx(adapter: Any, claimed: dict[str, object], **overrides: Any) -> EnrichmentContext:
    values: dict[str, Any] = dict(
        episode_uuid="ep-site",
        claimed=claimed,
        started=0.0,
        processing_attempts=1,
        worker_id="worker-site",
        graph_adapter=adapter,
        graphiti_client=MagicMock(),
        lifecycle_service=None,
        llm=None,
        ingest_gate=MagicMock(),
        processing_steps_total=5,
        settings_record_revisions=False,
        ready_warning_ms=10**9,
        graphiti_add_episode_timeout_s=1.0,
        graphiti_episode_max_estimated_tokens=0,
        get_queue_depth=lambda: 0,
    )
    values.update(overrides)
    return EnrichmentContext(**values)


def _adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.mark_episode_ready.return_value = True
    adapter.mark_episode_failed.return_value = True
    adapter.update_episode_processing.return_value = True
    adapter.stamp_ingest_metadata.return_value = SimpleNamespace(nodes_touched=0, edges_touched=0)
    adapter.fetch_episode_processing.return_value = {
        "processing_state": ProcessingState.ENRICHING,
        "processing_owner": "worker-site",
    }
    return adapter


def _only_call_kwargs(mock: MagicMock) -> dict[str, Any]:
    assert mock.call_count == 1, mock.call_args_list
    return mock.call_args.kwargs


def test_reconcile_ready_write_passes_the_claim_instant() -> None:
    """Fails if try_reconcile_existing stops passing claimed["processing_started_at"]."""
    instant = object()
    adapter = _adapter()
    adapter.find_completed_episode_artifact.return_value = {
        "resolved_episode_uuid": "resolved", "entity_uuids": [], "edge_uuids": [],
    }
    ctx = _ctx(adapter, {"processing_started_at": instant, "name": "n"})

    assert asyncio.run(try_reconcile_existing(ctx)) is True

    kwargs = _only_call_kwargs(adapter.mark_episode_ready)
    assert kwargs["claim_started_at"] is instant
    assert kwargs["worker_id"] == "worker-site"


def test_preflight_rejection_failed_write_passes_the_claim_instant() -> None:
    """Fails if run_preflight_rejection stops passing the instant to mark_episode_failed."""
    instant = object()
    adapter = _adapter()
    ctx = _ctx(
        adapter,
        {"processing_started_at": instant, "content": "word " * 5000},
        graphiti_episode_max_estimated_tokens=1,
    )

    assert asyncio.run(run_preflight_rejection(ctx)) is True

    kwargs = _only_call_kwargs(adapter.mark_episode_failed)
    assert kwargs["claim_started_at"] is instant
    assert kwargs["worker_id"] == "worker-site"


def _graphiti_result(nodes: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        episode=SimpleNamespace(uuid="resolved"), nodes=nodes, edges=[], episodic_edges=[]
    )


def test_zero_extraction_ready_write_passes_the_claim_instant() -> None:
    """Fails if the zero-extraction branch of stamp_and_finalize drops the instant."""
    enrichment_steps.clear_extraction_receipt()
    instant = object()
    adapter = _adapter()
    ctx = _ctx(adapter, {"processing_started_at": instant})

    asyncio.run(stamp_and_finalize(ctx, _graphiti_result([])))

    assert _only_call_kwargs(adapter.mark_episode_ready)["claim_started_at"] is instant


def test_final_ready_write_passes_the_claim_instant() -> None:
    """Fails if the normal (nodes extracted) final mark_episode_ready drops the instant."""
    enrichment_steps.clear_extraction_receipt()
    instant = object()
    adapter = _adapter()
    ctx = _ctx(adapter, {"processing_started_at": instant})
    # A nameless node keeps the correlation/anchoring side steps inert.
    node = SimpleNamespace(uuid="n1", name="", content="")

    asyncio.run(stamp_and_finalize(ctx, _graphiti_result([node])))

    assert _only_call_kwargs(adapter.mark_episode_ready)["claim_started_at"] is instant


@pytest.mark.parametrize("exc", [ValueError("boom"), TimeoutError("connection timeout")],
                         ids=["terminal", "retryable"])
def test_failure_handler_write_passes_the_claim_instant(exc: Exception) -> None:
    """Fails if handle_enrichment_failure drops the instant, for either grade of failure."""
    instant = object()
    adapter = _adapter()
    ctx = _ctx(adapter, {"processing_started_at": instant})

    asyncio.run(handle_enrichment_failure(ctx, exc))

    kwargs = _only_call_kwargs(adapter.mark_episode_failed)
    assert kwargs["claim_started_at"] is instant
    assert kwargs["worker_id"] == "worker-site"
