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
from unittest.mock import MagicMock, patch

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
    return adapter


@pytest.fixture
def client(graph_adapter):
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=graph_adapter),
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


def test_no_projection_when_the_link_did_not_land(client, graph_adapter):
    """MATCH-only means an unknown id links nothing. Projecting anyway would enrich a turn that no
    memory references -- capture-volume enrichment, the cost ADR 0001 rejected."""
    graph_adapter.link_episode_admission.return_value = False
    resp = _post(client)
    assert resp.status_code == 200
    assert resp.json()["linked"] is False
    assert resp.json()["projection_uuid"] is None
    assert graph_adapter.create_evidence_projection.call_count == 0


def test_an_already_projected_turn_reports_no_new_projection(client, graph_adapter):
    """A second memory citing one turn: the link is redrawn (MERGE, idempotent) but no new
    projection. Reporting one would tell the caller a node was minted that was not."""
    graph_adapter.create_evidence_projection.return_value = None
    resp = _post(client)
    assert resp.json()["linked"] is True
    assert resp.json()["projection_uuid"] is None


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
