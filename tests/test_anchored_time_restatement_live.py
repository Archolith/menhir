"""Graph-backed P3c counterexample: a restatement must not expire or unstamp a stored fact.

Episode 1 saves and persists a stamped, ended trip. Episode 2 restates it (no anchored-time
result for the mention, or a resolver-error report) through Graphiti's real
``resolve_extracted_edge`` with ``preserved_attributes=PRESERVED_EDGE_ATTRIBUTES``; after the
save the edge is still live and still carries its contract and stamp. Episode 3 contradicts it
inside the window and expires it. Needs archolith-graphiti-core 0.30.2.post6.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
import uuid as uuidlib

import pytest

from menhir.infrastructure.anchored_time import AnchoredTimeReport, AnchoredTimeResult
from menhir.infrastructure.anchored_time_persist import (
    PRESERVED_EDGE_ATTRIBUTES,
    persist_anchored_time,
)

pytestmark = pytest.mark.online

SPEECH = date(2024, 4, 10)
TRIP_START = datetime(2024, 3, 27, tzinfo=timezone.utc)
TRIP_END = datetime(2024, 4, 1, tzinfo=timezone.utc)
MILAN_START = datetime(2024, 3, 28, tzinfo=timezone.utc)
PATH_ONE_AT = datetime(2024, 4, 10, 18, 31, tzinfo=timezone.utc)


def _result(uuid: str, *, world_end: datetime | None = TRIP_END) -> AnchoredTimeResult:
    return AnchoredTimeResult(
        edge_uuid=uuid, fact_index=0, expression="from March 27 to April 1", basis="explicit_date",
        kind="point_event", granularity="day", window_start=date(2024, 3, 27),
        window_end=date(2024, 4, 1), anchor_ref=None, anchor_offset=None, planned_window=None,
        guard=None, written=False, reason="graphiti_inside_window", graphiti_valid_at=TRIP_START,
        new_valid_at=None, world_end=world_end,
    )


@pytest.fixture
async def driver(test_neo4j_repo):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    drv = Neo4jDriver(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                      os.environ["NEO4J_PASSWORD"], os.environ.get("NEO4J_DATABASE", "neo4j"))
    if drv._init_task is not None:
        await drv._init_task
    yield drv
    await drv.close()


def _state(repo, uid: str) -> dict:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.expired_at AS expired_at, "
        "r.invalid_at AS invalid_at, r.time_contract AS contract, r.time_expiry AS expiry, "
        "r.time_world_end AS world_end",
        params={"u": uid},
    )
    assert len(rows) == 1
    return rows[0]


class _DedupeLLM:
    def __init__(self, contradicted: list[int], duplicates: list[int] | None = None) -> None:
        self.contradicted, self.duplicates = contradicted, duplicates or []

    async def generate_response(self, *args, **kwargs):
        return {"duplicate_facts": self.duplicates, "contradicted_facts": self.contradicted}


async def _resolve_in_call(edge, related, existing, llm, results, episode_uuid, group_id, *,
                           status="ok"):
    """One add_episode-like scope: receipt, owner and this call's report, then the real resolver."""
    from graphiti_core.nodes import EpisodeType, EpisodicNode
    from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edge

    import menhir.infrastructure.graphiti_extraction_policy as policy

    episode = EpisodicNode(uuid=episode_uuid, name="ep", group_id=group_id, source=EpisodeType.message,
                           source_description="chat", content="user: Rome", valid_at=PATH_ONE_AT,
                           created_at=PATH_ONE_AT)
    owner = uuidlib.uuid4().hex
    receipt = policy.begin_extraction_receipt(episode_uuid, "user: Rome")
    binding = policy.anchored_time_owner.set(owner)
    try:
        report = AnchoredTimeReport(status=status, model="m", owner=owner, expiry=True,
                                    results=tuple(results))
        receipt.anchored_time = report
        resolved, invalidated, _ = await resolve_extracted_edge(
            llm, edge, related, existing, episode, edge_expiry_hook=policy.MenhirEdgeExpiryHook(),
            preserved_attributes=PRESERVED_EDGE_ATTRIBUTES)
        return resolved, invalidated, report
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()


@pytest.mark.parametrize("variant", ["no_result", "resolver_error"])
async def test_restated_stamped_trip_stays_live_and_stamped_then_contradiction_expires(
        driver, test_neo4j_repo, variant):
    from graphiti_core.edges import EntityEdge

    g = f"g-{uuidlib.uuid4().hex[:8]}"
    user, rome, milan, pasta = (str(uuidlib.uuid4()) for _ in range(4))
    test_neo4j_repo.execute(
        "UNWIND $ids AS id CREATE (:Entity {uuid: id, group_id: $g})",
        params={"ids": [user, rome, milan, pasta], "g": g})

    def new_edge(fact, target, *, valid_at, invalid_at=None, episode):
        return EntityEdge(uuid=str(uuidlib.uuid4()), group_id=g, source_node_uuid=user,
                          target_node_uuid=target, created_at=PATH_ONE_AT, name="RELATES_TO",
                          fact=fact, episodes=[episode], valid_at=valid_at, invalid_at=invalid_at,
                          fact_embedding=[0.1, 0.2, 0.3, 0.4])

    other_ep = str(uuidlib.uuid4())
    other = new_edge("The user likes pasta.", pasta, valid_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                     episode=other_ep)
    await other.save(driver)

    # Episode 1 saves an ended trip and persists the contract and the world-end stamp.
    ep1 = str(uuidlib.uuid4())
    trip = new_edge("The user was in Rome.", rome, valid_at=TRIP_START, invalid_at=TRIP_END, episode=ep1)
    resolved, _, report = await _resolve_in_call(trip, [], [other], _DedupeLLM([]),
                                                 [_result(trip.uuid)], ep1, g)
    assert resolved is trip and resolved.expired_at is None
    await resolved.save(driver)
    await persist_anchored_time(driver, report, episode_uuid=ep1, group_id=g, edge_uuids=[trip.uuid],
                                speech_date=SPEECH)
    state = _state(test_neo4j_repo, trip.uuid)
    assert state["expired_at"] is None and state["expiry"] == "world_end"
    assert state["contract"] is not None and state["world_end"] == TRIP_END.isoformat()

    # Episode 2 restates it as a duplicate; the report has no result for the mention (or the
    # resolver errored). The stored stamp alone must keep it live, and the stamp must survive
    # the duplicate path's attribute clear and EntityEdge.save's full property replace.
    ep2 = str(uuidlib.uuid4())
    stored = await EntityEdge.get_by_uuid(driver, trip.uuid)
    await stored.load_fact_embedding(driver)
    restated = new_edge("The user visited Rome.", rome, valid_at=TRIP_START, invalid_at=TRIP_END,
                        episode=ep2)
    if variant == "no_result":
        results, status = [_result("unrelated-edge")], "ok"
    else:
        results, status = [], "error"
    resolved, _, report = await _resolve_in_call(restated, [stored], [], _DedupeLLM([], [0]),
                                                 results, ep2, g, status=status)
    assert resolved.uuid == trip.uuid
    assert resolved.expired_at is None
    await resolved.save(driver)
    await persist_anchored_time(driver, report, episode_uuid=ep2, group_id=g, edge_uuids=[resolved.uuid],
                                speech_date=SPEECH)
    state = _state(test_neo4j_repo, trip.uuid)
    assert state["expired_at"] is None
    assert state["invalid_at"].to_native() == TRIP_END
    assert state["contract"] is not None
    assert state["expiry"] == "world_end" and state["world_end"] == TRIP_END.isoformat()

    # Episode 3 contradicts it inside the window (Milan from March 28): the trip is expired.
    ep3 = str(uuidlib.uuid4())
    stored = await EntityEdge.get_by_uuid(driver, trip.uuid)
    await stored.load_fact_embedding(driver)
    in_milan = new_edge("The user was in Milan.", milan, valid_at=MILAN_START, episode=ep3)
    resolved, invalidated, _ = await _resolve_in_call(
        in_milan, [], [stored], _DedupeLLM([0]), [_result(in_milan.uuid, world_end=None)], ep3, g)
    assert [e.uuid for e in invalidated] == [trip.uuid]
    for edge in [resolved, *invalidated]:
        await edge.save(driver)
    state = _state(test_neo4j_repo, trip.uuid)
    assert state["expired_at"] is not None
    assert state["invalid_at"].to_native() == MILAN_START
