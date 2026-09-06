"""POST /api/episode-admission: join a memory to the turn it answered, after the fact.

WHY AN ENDPOINT AT ALL. A host's post-tool lifecycle event fires AFTER `add_memory` has run, so it
cannot pass `turn_evidence_uuid` on the original call. It reports the pairing here instead.

WHAT THIS IS NOT. Verification. Both ids are caller-supplied and the server cannot confirm the
memory came from that turn; the tests below pin the honest behaviour (MATCH-only, no projection
without a landed link) rather than pretending to a guarantee. See the CORRECTION in
`.agent/plans/menhir-evidence-projection-episodes.md`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router


@pytest.fixture
def graph_adapter():
    adapter = MagicMock()
    adapter.link_episode_admission.return_value = True
    adapter.create_evidence_projection.return_value = "proj-1"
    adapter.find_pending_evidence_projection_uuid.return_value = None
    return adapter


@pytest.fixture
def client(graph_adapter):
    app = FastAPI()
    app.include_router(router)
    ingest_service = SimpleNamespace(
        enqueue_pending_episode=AsyncMock(return_value=True),
        enrichment_enabled=lambda: True,
    )
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(
            graph_adapter=graph_adapter,
            ingest_service=ingest_service,
        ),
        session=SimpleNamespace(session_id="process-session", user_id="claude-code"),
        capabilities=SimpleNamespace(startup_mode="full", failures=[]),
    )
    with patch.object(api_routes, "_require_tier", lambda *_a, **_k: None):
        yield TestClient(app)


def _post(client, **body):
    payload = {"episode_uuid": "ep-1", "turn_evidence_uuid": "turn-1"}
    payload.update(body)
    return client.post("/api/episode-admission", json=payload)


def test_links_and_projects(client, graph_adapter):
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["linked"] is True
    assert data["projection_uuid"] == "proj-1"
    # The route now resolves the namespace ONCE and passes it to both the link and the
    # projection, so a pinned caller cannot join or read across silos. None is the unpinned
    # value and preserves the previous behaviour.
    assert graph_adapter.link_episode_admission.call_args.kwargs == {
        "episode_uuid": "ep-1", "turn_evidence_uuid": "turn-1", "namespace": None}
    client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode.assert_awaited_once_with(
        "proj-1"
    )


def test_no_projection_when_the_link_did_not_land(client, graph_adapter):
    """MATCH-only means an unknown id links nothing. Projecting anyway would enrich a turn that no
    memory references -- capture-volume enrichment, the cost ADR 0001 rejected."""
    graph_adapter.link_episode_admission.return_value = False
    resp = _post(client)
    assert resp.status_code == 200
    assert resp.json()["linked"] is False
    assert resp.json()["projection_uuid"] is None
    assert graph_adapter.create_evidence_projection.call_count == 0
    client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode.assert_not_awaited()


def test_an_already_projected_turn_reports_no_new_projection(client, graph_adapter):
    """A second memory citing one turn: the link is redrawn (MERGE, idempotent) but no new
    projection. Reporting one would tell the caller a node was minted that was not."""
    graph_adapter.create_evidence_projection.return_value = None
    resp = _post(client)
    assert resp.json()["linked"] is True
    assert resp.json()["projection_uuid"] is None
    client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode.assert_not_awaited()


def test_retry_recovers_projection_created_before_enqueue_failure(client, graph_adapter):
    queue = client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode
    queue.side_effect = [RuntimeError("queue unavailable"), True]

    with pytest.raises(RuntimeError, match="queue unavailable"):
        _post(client)

    graph_adapter.create_evidence_projection.return_value = None
    graph_adapter.find_pending_evidence_projection_uuid.return_value = "proj-1"
    retry = _post(client)

    assert retry.status_code == 200
    assert retry.json() == {"linked": True, "projection_uuid": None}
    assert queue.await_count == 2
    assert queue.await_args_list[-1].args == ("proj-1",)


def test_the_projection_is_named_for_its_turn_not_the_episode(client, graph_adapter):
    """Projections are keyed and named by TURN: one turn yields one projection no matter how many
    memories cite it."""
    _post(client)
    kwargs = graph_adapter.create_evidence_projection.call_args.kwargs
    assert kwargs["turn_evidence_uuid"] == "turn-1"
    assert "turn-1" in kwargs["name"]


@pytest.mark.parametrize("body,why", [
    ({"episode_uuid": ""}, "blank episode"),
    ({"turn_evidence_uuid": ""}, "blank turn"),
    ({"episode_uuid": "   "}, "whitespace episode"),
    ({"turn_evidence_uuid": "  "}, "whitespace turn"),
])
def test_blank_ids_are_refused(client, graph_adapter, body, why):
    """A blank id would MATCH nothing and quietly report linked=false; 400 names the bad side instead
    of looking like a legitimate miss."""
    assert _post(client, **body).status_code == 400, why
    assert graph_adapter.link_episode_admission.call_count == 0


def test_unavailable_adapter_is_503_not_500(client, graph_adapter):
    del graph_adapter.link_episode_admission
    assert _post(client).status_code == 503


def test_unavailable_queue_refuses_before_writing(client, graph_adapter):
    del client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode

    assert _post(client).status_code == 503
    graph_adapter.link_episode_admission.assert_not_called()
    graph_adapter.create_evidence_projection.assert_not_called()


def test_disabled_enrichment_refuses_before_writing(client, graph_adapter):
    client.app.state.runtime_ctx.built.ingest_service.enrichment_enabled = lambda: False

    resp = _post(client)

    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"]
    graph_adapter.link_episode_admission.assert_not_called()
    graph_adapter.create_evidence_projection.assert_not_called()


def test_enqueue_failure_is_visible(client, graph_adapter):
    client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode.side_effect = (
        RuntimeError("queue unavailable")
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        _post(client)


def test_queue_false_is_an_idempotent_success(client, graph_adapter):
    client.app.state.runtime_ctx.built.ingest_service.enqueue_pending_episode.return_value = False

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json()["projection_uuid"] == "proj-1"
