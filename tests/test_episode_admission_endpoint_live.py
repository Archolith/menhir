"""Live proof (:7688) that /api/episode-admission produces the graph it claims to.

`tests/test_episode_admission_endpoint.py` mocks the adapter, so it proves the route calls the right
methods and nothing about what lands in Neo4j. This wires the real `MemoryGraphAdapter` to the real
app and asserts on the GRAPH -- the seam where a route that "works" can still write nothing useful.

Covers the whole server half of the loop: capture a turn, write a memory, report the pairing, and
check that the edge, the projection, and the projection's exclusion from recall all exist together.
The only untested link in the chain after this is the hook process itself.
"""

from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository


@pytest.fixture
def live_client(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=adapter),
        session=SimpleNamespace(session_id="s", user_id="u"),
        capabilities=SimpleNamespace(startup_mode="full", failures=[]),
    )
    with patch.object(api_routes, "_require_tier", lambda *_a, **_k: None):
        yield TestClient(app), adapter


def _seed_turn(raw, *, ns, text):
    return TurnEvidenceRepository(raw).record_turn_evidence(
        text=text, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="s",
        prompt_id=f"p-{uuidlib.uuid4().hex[:8]}")["turn_id"]


def _seed_memory(adapter, *, ns):
    uuid = str(uuidlib.uuid4())
    adapter.create_pending_episode(
        episode_uuid=uuid, name="memory", content="User owns 25 postcards",
        session_id="s", user_id="u", source="claude-code", source_confidence=0.7, namespace=ns)
    return uuid


@pytest.mark.online
def test_the_full_server_half_of_the_loop(live_client, test_neo4j_repo):
    client, adapter = live_client
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    turn_text = "I have 25 postcards in the blue album"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text=turn_text)
    episode = _seed_memory(adapter, ns=ns)

    resp = client.post("/api/episode-admission",
                       json={"episode_uuid": episode, "turn_evidence_uuid": tid})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["linked"] is True
    projection = body["projection_uuid"]
    assert projection

    # the memory now reaches its turn
    rows = test_neo4j_repo.execute(
        "MATCH (e:Episodic {uuid: $e})-[:ADMITTED_ON]->(t:TurnEvidence) RETURN t.turn_id AS tid",
        params={"e": episode})
    assert [r["tid"] for r in rows] == [tid]

    # and the user's own words are queued for enrichment as a projection
    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $p}) RETURN p.content AS content, p.processing_state AS state, "
        "p.is_evidence_projection AS flag", params={"p": projection})
    assert rows[0]["content"] == turn_text        # the USER's phrasing, not the memory's paraphrase
    assert rows[0]["state"] == "PENDING"
    assert rows[0]["flag"] is True


@pytest.mark.online
def test_a_second_memory_on_the_same_turn_links_but_does_not_re_project(live_client,
                                                                       test_neo4j_repo):
    """Two memories from one turn is ordinary. The link is drawn for both; the projection is minted
    once, or every turn would yield duplicate entities in proportion to how much was remembered."""
    client, adapter = live_client
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 25 postcards")
    first, second = _seed_memory(adapter, ns=ns), _seed_memory(adapter, ns=ns)

    a = client.post("/api/episode-admission",
                    json={"episode_uuid": first, "turn_evidence_uuid": tid}).json()
    b = client.post("/api/episode-admission",
                    json={"episode_uuid": second, "turn_evidence_uuid": tid}).json()

    assert a["linked"] is True and b["linked"] is True
    assert a["projection_uuid"] and b["projection_uuid"] is None

    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN count(p) AS c", params={"t": tid})
    assert rows[0]["c"] == 1

    # both memories still reach the turn
    rows = test_neo4j_repo.execute(
        "MATCH (e:Episodic)-[:ADMITTED_ON]->(:TurnEvidence {turn_id: $t}) "
        "WHERE e.is_evidence_projection IS NULL RETURN count(e) AS c", params={"t": tid})
    assert rows[0]["c"] == 2


@pytest.mark.online
def test_an_unknown_episode_links_nothing_and_projects_nothing(live_client, test_neo4j_repo):
    """MATCH-only, end to end: a stale or wrong id must be inert, not create stubs. If this failed,
    a caller could mint :Episodic nodes through a provenance endpoint."""
    client, _ = live_client
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 25 postcards")

    body = client.post("/api/episode-admission",
                       json={"episode_uuid": f"ghost-{uuidlib.uuid4().hex}",
                             "turn_evidence_uuid": tid}).json()
    assert body["linked"] is False
    assert body["projection_uuid"] is None

    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN count(p) AS c", params={"t": tid})
    assert rows[0]["c"] == 0, "projected a turn that no memory references"


@pytest.mark.online
def test_an_unknown_turn_creates_no_turn_node(live_client, test_neo4j_repo):
    client, adapter = live_client
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    episode = _seed_memory(adapter, ns=ns)
    ghost = f"ghost-{uuidlib.uuid4().hex}"

    body = client.post("/api/episode-admission",
                       json={"episode_uuid": episode, "turn_evidence_uuid": ghost}).json()
    assert body["linked"] is False

    rows = test_neo4j_repo.execute(
        "MATCH (t:TurnEvidence {turn_id: $t}) RETURN count(t) AS c", params={"t": ghost})
    assert rows[0]["c"] == 0, "MERGEd a TurnEvidence stub nobody ever said"
