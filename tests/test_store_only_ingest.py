"""Store-only ingest: stored and embedded for the source-memory lane, never extracted.

Safety property: an episode submitted with ``store_only`` is never sent to Graphiti extraction and
never causes its turn's evidence projection to be minted, on its first attempt or any later one.
The Cypher (fence, durable mode, sweep exemption) is proven in `test_store_only_ingest_live.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.domain import MemorySession
from menhir.domain.ingest import EXTRACTION_MODE_STORE_ONLY, episode_request_digest
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository
from menhir.services import ingest_worker as iw
from menhir.services.ingest_intake import IngestIntakeMixin
from menhir.services.ingest_queue import IngestQueueMixin
from menhir.services.ingest_worker import IngestWorkerMixin

pytestmark = pytest.mark.unit


# --- request digest ---------------------------------------------------------------------------

def _digest(**kw: Any) -> str:
    base = dict(content="c", diff=None, source="user", flagged=False, bootstrap_scope=None,
                occurred_at=None, role=None, turn_evidence_uuid=None, user_statement=None)
    return episode_request_digest(**{**base, **kw})


def test_digest_is_unchanged_for_normal_submits_and_differs_for_store_only() -> None:
    # Digests stored before this field existed must keep matching their replays.
    assert _digest() == _digest(store_only=False)
    assert _digest(store_only=True) != _digest()


# --- intake -----------------------------------------------------------------------------------

class _Intake(IngestIntakeMixin):
    def __init__(self, adapter: Any) -> None:
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _session() -> MemorySession:
    return MemorySession(session_id="session-1", user_id="user-1",
                         started_at=datetime.now(timezone.utc))


def _intake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.fetch_turn_evidence.return_value = {
        "turn_id": "turn-1", "role": "user", "text": "What should I cook in my instant pot?",
        "session_id": "session-1", "namespace": None,
    }
    adapter.find_episode_by_ingest_key.return_value = None
    return adapter


async def _queue(adapter: MagicMock, **kw: Any) -> None:
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="What should I cook in my instant pot?", session=_session(), source="user",
        turn_evidence_uuid="turn-1", **kw)


async def test_store_only_records_the_mode_links_the_turn_and_mints_no_projection() -> None:
    adapter = _intake_adapter()
    await _queue(adapter, store_only=True)
    assert adapter.create_pending_episode.call_args.kwargs["extraction_mode"] == (
        EXTRACTION_MODE_STORE_ONLY)
    assert adapter.link_episode_admission.call_count == 1  # provenance is kept
    adapter.create_evidence_projection.assert_not_called()


async def test_normal_submit_is_unchanged() -> None:
    adapter = _intake_adapter()
    await _queue(adapter)
    assert "extraction_mode" not in adapter.create_pending_episode.call_args.kwargs
    adapter.create_evidence_projection.assert_called_once()


async def test_store_only_is_part_of_the_idempotency_digest() -> None:
    adapter = _intake_adapter()
    await _queue(adapter, store_only=True, idempotency_key="k-1")
    stored = adapter.create_pending_episode.call_args.kwargs["ingest_request_sha256"]
    normal = _intake_adapter()
    await _queue(normal, idempotency_key="k-1")
    assert stored != normal.create_pending_episode.call_args.kwargs["ingest_request_sha256"]


# --- worker -----------------------------------------------------------------------------------

class _Adapter:
    def __init__(self, claimed: dict[str, Any], finished: bool = True) -> None:
        self.claimed = claimed
        self.finished = finished
        self.calls: list[tuple[str, tuple, dict]] = []

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def claim_pending_episode(self, *a, **k):
        self.calls.append(("claim_pending_episode", a, k))
        return self.claimed

    def mark_episode_store_only(self, *a, **k):
        self.calls.append(("mark_episode_store_only", a, k))
        return self.finished

    def mark_episode_verbatim_skipped(self, *a, **k):
        self.calls.append(("mark_episode_verbatim_skipped", a, k))
        return None


class _Svc(IngestWorkerMixin, IngestQueueMixin):
    def __init__(self, adapter: _Adapter) -> None:
        self.graph_adapter = adapter
        self._worker_id = "worker-A"
        self._max_enrichment_attempts = 3
        self._context_window_retry_attempts = 3
        self._enrichment_lease_seconds = 60
        self._failed_enrichments = 0
        self._job_llm_call_counts: dict[str, int] = {}
        self.graphiti_client = None
        self.lifecycle_service = None
        self.llm = None
        self.get_queue_depth = lambda: 0
        self._inferred_self_mode = "enforce"
        self.budget_checked = False

    async def _enqueue_pending_episode(self, episode_uuid: str) -> None:
        pass

    async def _processing_heartbeat_loop(self, episode_uuid, stop_event, claim_started_at=None):
        await stop_event.wait()

    async def _check_session_budget(self, episode_uuid, session_id):
        self.budget_checked = True
        return None

    def _gate(self):
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            return None
        raise AttributeError(name)


CLAIM = {
    "uuid": "ep-1",
    "processing_attempts": 1,
    "processing_started_at": "STARTED-TOKEN",
    "is_evidence_projection": False,
    "turn_evidence_uuid": "turn-1",
    "source": "user",
    "extraction_mode": EXTRACTION_MODE_STORE_ONLY,
}


@pytest.fixture
def steps(monkeypatch):
    rec = SimpleNamespace(order=[])

    async def embed(ctx):
        rec.order.append("embed")

    async def reconcile(ctx):
        rec.order.append("reconcile")
        return False

    async def preflight(ctx):
        rec.order.append("preflight")
        return False

    async def extraction(ctx, **kw):
        rec.order.append("extraction")

    async def failure(ctx, exc):
        rec.order.append("failure")

    monkeypatch.setattr(iw, "EnrichmentContext", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(iw, "embed_episode_content", embed)
    monkeypatch.setattr(iw, "try_reconcile_existing", reconcile)
    monkeypatch.setattr(iw, "run_preflight_rejection", preflight)
    monkeypatch.setattr(iw, "run_graphiti_extraction", extraction)
    monkeypatch.setattr(iw, "handle_enrichment_failure", failure)
    return rec


async def test_store_only_claim_embeds_and_finishes_without_extraction(steps) -> None:
    svc = _Svc(_Adapter(dict(CLAIM)))
    await svc._process_pending_episode("ep-1")
    assert steps.order == ["embed"]
    assert not svc.budget_checked
    (name, args, kwargs), = [c for c in svc.graph_adapter.calls if c[0] == "mark_episode_store_only"]
    assert args == ("ep-1",)
    assert kwargs == {"worker_id": "worker-A", "claim_started_at": "STARTED-TOKEN"}
    assert "mark_episode_verbatim_skipped" not in svc.graph_adapter.names()


async def test_lost_fence_never_falls_through_to_extraction(steps) -> None:
    # Counterexample: this attempt's lease was taken over before the READY write landed.
    svc = _Svc(_Adapter(dict(CLAIM), finished=False))
    await svc._process_pending_episode("ep-1")
    assert "extraction" not in steps.order


async def test_a_claim_without_the_mode_still_extracts(steps) -> None:
    claim = {k: v for k, v in CLAIM.items() if k != "extraction_mode"}
    svc = _Svc(_Adapter(claim))
    await svc._process_pending_episode("ep-1")
    assert steps.order[-1] == "extraction"
    assert "mark_episode_store_only" not in svc.graph_adapter.names()


# --- Cypher shape -----------------------------------------------------------------------------

class _Recording:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return self.rows


def test_claim_returns_the_durable_mode() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _Recording()
    repo.claim_pending_episode("ep-1", max_attempts=3, worker_id="w", lease_seconds=60)
    claim_query = repo.neo4j.calls[1][0]
    assert "n.extraction_mode AS extraction_mode" in claim_query


def test_store_only_write_is_fenced_on_owner_instant_and_mode() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _Recording()
    assert repo.mark_episode_store_only("ep-1", worker_id="w", claim_started_at=None) is False
    assert repo.neo4j.calls == []  # no fence, no write
    repo.mark_episode_store_only("ep-1", worker_id="w", claim_started_at="T")
    query, params = repo.neo4j.calls[0]
    assert "n.processing_owner = $worker_id" in query
    assert "n.extraction_mode = $store_only" in query
    assert params["store_only"] == EXTRACTION_MODE_STORE_ONLY


def test_zero_extraction_reset_exempts_store_only() -> None:
    repo = EpisodeMaintenanceRepository()
    repo.neo4j = _Recording([{"reset": 0}])
    repo.reset_zero_extraction_episodes()
    assert "coalesce(n.extraction_mode, '') <> 'store_only'" in repo.neo4j.calls[0][0]


# --- REST -------------------------------------------------------------------------------------

def _app(graph_adapter: Any, backend: Any = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    ingest_service = SimpleNamespace(
        enqueue_pending_episode=AsyncMock(return_value=True), enrichment_enabled=lambda: True)
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=graph_adapter, ingest_service=ingest_service),
        session=SimpleNamespace(session_id="process-session", user_id="claude-code"),
        capabilities=SimpleNamespace(startup_mode="full", failures=[]),
    )
    if backend is not None:
        app.state.backend = backend
    return TestClient(app)


def test_late_admission_of_a_store_only_episode_links_but_does_not_project() -> None:
    adapter = MagicMock()
    adapter.link_episode_admission.return_value = True
    adapter.create_evidence_projection.return_value = "proj-1"
    adapter.episode_is_store_only.return_value = True
    with patch.object(api_routes, "_require_tier", lambda *_a, **_k: None):
        resp = _app(adapter).post("/api/episode-admission",
                                  json={"episode_uuid": "ep-1", "turn_evidence_uuid": "turn-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"linked": True, "projection_uuid": None}
    adapter.create_evidence_projection.assert_not_called()


def test_late_admission_of_a_normal_episode_still_projects() -> None:
    adapter = MagicMock()
    adapter.link_episode_admission.return_value = True
    adapter.create_evidence_projection.return_value = "proj-1"
    adapter.episode_is_store_only.return_value = False
    with patch.object(api_routes, "_require_tier", lambda *_a, **_k: None):
        resp = _app(adapter).post("/api/episode-admission",
                                  json={"episode_uuid": "ep-1", "turn_evidence_uuid": "turn-1"})
    assert resp.json()["projection_uuid"] == "proj-1"
