"""Two-tier self Phase 3 (verbatim extraction skip): worker / queue wiring, offline.

The Cypher guarantees live in `tests/test_verbatim_skip_live.py`. These tests pin the PYTHON
decisions around it: when the worker even asks the graph to skip, what it does with the answer, that
a failing projection eagerly re-enables its clients, and that lease recovery always sweeps.

The harness is a bare class mixing in the real `IngestWorkerMixin` / `IngestQueueMixin` with fake
adapters; the enrichment steps are replaced at the `ingest_worker` module seam.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from menhir.services import ingest_worker as iw
from menhir.services.ingest_queue import IngestQueueMixin
from menhir.services.ingest_worker import IngestWorkerMixin


class _Adapter:
    """Fake graph adapter recording call order; behaviors are set per test."""

    def __init__(self, *, claimed: dict[str, Any] | None = None) -> None:
        self.claimed = claimed
        self.calls: list[tuple[str, tuple, dict]] = []
        self.skip_result: Any = None
        self.skip_exc: Exception | None = None
        self.reenable_result: list[str] = []
        self.reenable_exc: Exception | None = None
        self.pending: list[str] = []

    def _rec(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def claim_pending_episode(self, *a, **k):
        self._rec("claim_pending_episode", a, k)
        return self.claimed

    def mark_episode_verbatim_skipped(self, *a, **k):
        self._rec("mark_episode_verbatim_skipped", a, k)
        if self.skip_exc:
            raise self.skip_exc
        return self.skip_result

    def reenable_verbatim_skips(self, *a, **k):
        self._rec("reenable_verbatim_skips", a, k)
        if self.reenable_exc:
            raise self.reenable_exc
        return list(self.reenable_result)

    # lease-recovery surface
    def fail_exhausted_pending_episodes(self, **k):
        self._rec("fail_exhausted_pending_episodes", (), k)
        return 0

    def fail_transient_exhausted_pending_episodes(self, **k):
        self._rec("fail_transient_exhausted_pending_episodes", (), k)
        return 0

    def reset_orphaned_enriching_episodes(self, **k):
        self._rec("reset_orphaned_enriching_episodes", (), k)
        return 0

    def reset_stale_enriching_episodes(self, **k):
        self._rec("reset_stale_enriching_episodes", (), k)
        return 0

    def list_pending_episode_uuids(self, **k):
        self._rec("list_pending_episode_uuids", (), k)
        return list(self.pending)


class _NoSweepAdapter(_Adapter):
    """An adapter predating the feature: the attribute must be genuinely absent."""

    def __getattribute__(self, name):
        if name == "reenable_verbatim_skips":
            raise AttributeError(name)
        return super().__getattribute__(name)


class _Svc(IngestWorkerMixin, IngestQueueMixin):
    def __init__(self, adapter: _Adapter, mode: Any = "enforce") -> None:
        self.graph_adapter = adapter
        self._worker_id = "worker-A"
        self._max_enrichment_attempts = 3
        self._context_window_retry_attempts = 3
        self._enrichment_lease_seconds = 60
        self._failed_enrichments = 0
        self._job_llm_call_counts: dict[str, int] = {}
        self.enqueued: list[str] = []
        self.graphiti_client = None
        self.lifecycle_service = None
        self.llm = None
        self.get_queue_depth = lambda: 0
        if mode != "__missing__":
            self._inferred_self_mode = mode

    async def _enqueue_pending_episode(self, episode_uuid: str) -> None:
        self.enqueued.append(episode_uuid)

    async def _processing_heartbeat_loop(self, episode_uuid, stop_event):
        await stop_event.wait()

    async def _check_session_budget(self, episode_uuid, session_id):
        return None

    def _gate(self):
        return None

    def __getattr__(self, name):  # any other config attribute the context copies
        if name.startswith("_"):
            return None
        raise AttributeError(name)


CLIENT_CLAIM = {
    "uuid": "client-1",
    "processing_attempts": 1,
    "processing_started_at": "STARTED-TOKEN",
    "is_evidence_projection": False,
    "turn_evidence_uuid": "turn-1",
    "source": "agent",
}


def _svc(mode: Any = "enforce", **adapter_kw) -> _Svc:
    return _Svc(_Adapter(**adapter_kw), mode)


# ----------------------------------------------------------------------------------- (a)

@pytest.mark.parametrize("mode", ["off", "observe", "garbage", "", None, "__missing__"])
async def test_no_skip_unless_mode_is_enforce(mode):
    svc = _svc(mode)
    svc.graph_adapter.skip_result = "proj-1"  # would skip if asked: the guard must be the mode
    assert await svc._try_verbatim_skip("client-1", dict(CLIENT_CLAIM)) is False
    assert "mark_episode_verbatim_skipped" not in svc.graph_adapter.names()


async def test_no_skip_when_claim_is_a_projection():
    svc = _svc()
    svc.graph_adapter.skip_result = "proj-1"
    claim = {**CLIENT_CLAIM, "is_evidence_projection": True}
    assert await svc._try_verbatim_skip("client-1", claim) is False
    assert svc.graph_adapter.names() == []


@pytest.mark.parametrize("missing", [None, ""])
async def test_no_skip_without_turn_evidence_uuid(missing):
    svc = _svc()
    svc.graph_adapter.skip_result = "proj-1"
    claim = {**CLIENT_CLAIM, "turn_evidence_uuid": missing}
    assert await svc._try_verbatim_skip("client-1", claim) is False
    assert svc.graph_adapter.names() == []
    claim.pop("turn_evidence_uuid")
    assert await svc._try_verbatim_skip("client-1", claim) is False
    assert svc.graph_adapter.names() == []


# ----------------------------------------------------------------------------------- (b)

async def test_enforce_skip_true_passes_this_claims_fence_to_the_graph():
    svc = _svc()
    svc.graph_adapter.skip_result = "proj-1"
    assert await svc._try_verbatim_skip("client-1", dict(CLIENT_CLAIM)) is True
    (name, args, kwargs), = svc.graph_adapter.calls
    assert name == "mark_episode_verbatim_skipped"
    assert args == ("client-1",)
    # The fence is THIS claim's owner and start instant, taken from the claim row.
    assert kwargs == {"worker_id": "worker-A", "claim_started_at": "STARTED-TOKEN"}


async def test_enforce_graph_declines_means_extract():
    svc = _svc()
    svc.graph_adapter.skip_result = None
    assert await svc._try_verbatim_skip("client-1", dict(CLIENT_CLAIM)) is False
    assert svc.graph_adapter.names() == ["mark_episode_verbatim_skipped"]


async def test_enforce_graph_error_fails_open_to_extraction(caplog):
    svc = _svc()
    svc.graph_adapter.skip_exc = RuntimeError("neo4j down")
    with caplog.at_level(logging.ERROR):
        assert await svc._try_verbatim_skip("client-1", dict(CLIENT_CLAIM)) is False


# ----------------------------------------------------------------------------------- (c)

@pytest.fixture
def steps(monkeypatch):
    """Replace the enrichment steps at the worker's import seam; record what runs."""
    rec = SimpleNamespace(
        extraction=MagicMock(name="run_graphiti_extraction"),
        failure=MagicMock(name="handle_enrichment_failure"),
        order=[],
        extraction_exc=None,
    )

    async def embed(ctx):
        rec.order.append("embed")

    async def reconcile(ctx):
        return False

    async def preflight(ctx):
        return False

    async def extraction(ctx, **kw):
        rec.order.append("extraction")
        rec.extraction(ctx, **kw)
        if rec.extraction_exc:
            raise rec.extraction_exc

    async def failure(ctx, exc):
        rec.order.append("handle_enrichment_failure")
        rec.failure(ctx, exc)

    monkeypatch.setattr(iw, "EnrichmentContext", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(iw, "embed_episode_content", embed)
    monkeypatch.setattr(iw, "try_reconcile_existing", reconcile)
    monkeypatch.setattr(iw, "run_preflight_rejection", preflight)
    monkeypatch.setattr(iw, "run_graphiti_extraction", extraction)
    monkeypatch.setattr(iw, "handle_enrichment_failure", failure)
    return rec


async def test_process_enforce_skip_true_does_not_extract(steps):
    svc = _svc(claimed=dict(CLIENT_CLAIM))
    svc.graph_adapter.skip_result = "proj-1"
    await svc._process_pending_episode("client-1")
    steps.extraction.assert_not_called()
    assert "mark_episode_verbatim_skipped" in svc.graph_adapter.names()


async def test_process_enforce_skip_false_extracts(steps):
    svc = _svc(claimed=dict(CLIENT_CLAIM))
    svc.graph_adapter.skip_result = None
    await svc._process_pending_episode("client-1")
    steps.extraction.assert_called_once()
    assert steps.order == ["embed", "extraction"]


async def test_process_off_mode_extracts_without_asking_graph(steps):
    svc = _svc("off", claimed=dict(CLIENT_CLAIM))
    svc.graph_adapter.skip_result = "proj-1"
    await svc._process_pending_episode("client-1")
    steps.extraction.assert_called_once()
    assert "mark_episode_verbatim_skipped" not in svc.graph_adapter.names()


async def test_failing_projection_reenables_its_clients_after_failure_handling(steps):
    claim = {**CLIENT_CLAIM, "uuid": "proj-1", "is_evidence_projection": True}
    svc = _svc(claimed=claim)
    svc.graph_adapter.reenable_result = ["client-a", "client-b"]
    steps.extraction_exc = RuntimeError("extraction blew up")
    await svc._process_pending_episode("proj-1")
    assert steps.order == ["embed", "extraction", "handle_enrichment_failure"]
    (name, args, kwargs), = [c for c in svc.graph_adapter.calls
                             if c[0] == "reenable_verbatim_skips"]
    assert kwargs == {"projection_uuid": "proj-1"}
    assert svc.enqueued == ["client-a", "client-b"]
    # A projection is never itself a skip candidate.
    assert "mark_episode_verbatim_skipped" not in svc.graph_adapter.names()


async def test_failing_client_episode_does_not_trigger_reenable(steps):
    svc = _svc(claimed=dict(CLIENT_CLAIM))
    steps.extraction_exc = RuntimeError("extraction blew up")
    await svc._process_pending_episode("client-1")
    assert steps.order[-1] == "handle_enrichment_failure"
    assert "reenable_verbatim_skips" not in svc.graph_adapter.names()
    assert svc.enqueued == []


async def test_projection_failure_reenable_error_is_contained(steps):
    claim = {**CLIENT_CLAIM, "uuid": "proj-1", "is_evidence_projection": True}
    svc = _svc(claimed=claim)
    svc.graph_adapter.reenable_exc = RuntimeError("neo4j down")
    steps.extraction_exc = RuntimeError("extraction blew up")
    await svc._process_pending_episode("proj-1")  # must not raise
    assert svc.enqueued == []


async def test_projection_failure_with_adapter_lacking_method_is_contained(steps):
    claim = {**CLIENT_CLAIM, "uuid": "proj-1", "is_evidence_projection": True}
    svc = _Svc(_NoSweepAdapter(claimed=claim))
    steps.extraction_exc = RuntimeError("extraction blew up")
    await svc._process_pending_episode("proj-1")  # must not raise


# ----------------------------------------------------------------------------------- (d)

async def test_lease_recovery_sweeps_before_listing_pending_and_enqueues_both():
    svc = _svc("off")  # regardless of mode
    svc.graph_adapter.reenable_result = ["client-a"]
    svc.graph_adapter.pending = ["client-a", "other"]
    await svc._recover_stale_episode_leases(limit=7)
    names = svc.graph_adapter.names()
    assert names.index("reenable_verbatim_skips") < names.index("list_pending_episode_uuids")
    sweep = next(c for c in svc.graph_adapter.calls if c[0] == "reenable_verbatim_skips")
    assert sweep[2] == {"limit": 7}
    assert svc.enqueued == ["client-a", "other"]


async def test_lease_recovery_tolerates_adapter_without_the_method():
    svc = _Svc(_NoSweepAdapter())
    svc.graph_adapter.pending = ["p1"]
    result = await svc._recover_stale_episode_leases()
    assert result == (0, 1)
    assert svc.enqueued == ["p1"]


async def test_lease_recovery_logs_and_continues_when_sweep_raises(caplog):
    svc = _svc()
    svc.graph_adapter.reenable_exc = RuntimeError("boom")
    svc.graph_adapter.pending = ["p1", "p2"]
    with caplog.at_level(logging.ERROR):
        result = await svc._recover_stale_episode_leases()
    assert result == (0, 2)
    assert svc.enqueued == ["p1", "p2"]
    assert any("re-enable sweep failed" in r.getMessage() for r in caplog.records)
