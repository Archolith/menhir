"""Graph-backed counterexamples for the P2 persist step (plan section 4).

The offline file proves which rows reach the statement; this one proves what the statement does
to a real graph: only this episode's new edges in this tenant get a contract, exactly once.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
import uuid as uuidlib

import pytest

from menhir.infrastructure.anchored_time import AnchoredTimeReport, AnchoredTimeResult
from menhir.infrastructure.anchored_time_persist import persist_anchored_time

pytestmark = pytest.mark.online

SPEECH = date(2023, 5, 14)


def _result(uuid: str, expression: str = "a month ago") -> AnchoredTimeResult:
    return AnchoredTimeResult(
        edge_uuid=uuid, fact_index=0, expression=expression, basis="speech_relative",
        kind="point_event", granularity="month", window_start=date(2023, 4, 1),
        window_end=date(2023, 4, 30), anchor_ref=None, anchor_offset=None, planned_window=None,
        guard=None, written=True, reason="written",
        graphiti_valid_at=datetime(2023, 5, 14, tzinfo=timezone.utc),
        new_valid_at=datetime(2023, 4, 15, tzinfo=timezone.utc),
    )


def _report(*uuids: str, expression: str = "a month ago") -> AnchoredTimeReport:
    return AnchoredTimeReport(status="ok", model="m",
                              results=tuple(_result(u, expression) for u in uuids))


def _edge(repo, *, group_id: str, episodes: list[str], expired: bool = False) -> str:
    uid = str(uuidlib.uuid4())
    repo.execute(
        "CREATE (a:Entity {uuid: $a, group_id: $g})-[:RELATES_TO {uuid: $u, group_id: $g, "
        "episodes: $eps, fact: 'f', expired_at: CASE WHEN $expired THEN datetime() END}]->"
        "(b:Entity {uuid: $b, group_id: $g})",
        params={"a": str(uuidlib.uuid4()), "b": str(uuidlib.uuid4()), "u": uid, "g": group_id,
                "eps": episodes, "expired": expired},
    )
    return uid


def _contract(repo, uid: str) -> dict:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.time_contract AS contract, "
        "r.time_expression AS expression, r.time_window_start AS start, "
        "r.time_speech_date AS speech, r.valid_at AS valid_at, r.invalid_at AS invalid_at",
        params={"u": uid},
    )
    assert len(rows) == 1
    return rows[0]


@pytest.fixture
async def driver(test_neo4j_repo):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    drv = Neo4jDriver(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                      os.environ["NEO4J_PASSWORD"], os.environ.get("NEO4J_DATABASE", "neo4j"))
    if drv._init_task is not None:
        await drv._init_task
    yield drv
    await drv.close()


async def _persist(driver, report, *, episode: str, group: str, edges: list[str]) -> None:
    await persist_anchored_time(driver, report, episode_uuid=episode, group_id=group,
                                edge_uuids=edges, speech_date=SPEECH)


async def test_new_edge_gets_the_contract_and_nothing_else_changes(driver, test_neo4j_repo):
    g, ep = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4())
    new = _edge(test_neo4j_repo, group_id=g, episodes=[ep])
    rep = _report(new)
    await _persist(driver, rep, episode=ep, group=g, edges=[new])
    row = _contract(test_neo4j_repo, new)
    assert row["contract"].startswith("at1|") and row["expression"] == "a month ago"
    assert row["start"] == "2023-04-01" and row["speech"] == "2023-05-14"
    assert row["valid_at"] is None and row["invalid_at"] is None  # P2 never writes time itself
    assert (rep.persist, rep.persisted) == ("ok", 1)


async def test_duplicate_resolved_to_existing_edge_writes_nothing(driver, test_neo4j_repo):
    # The extracted uuid never reached the graph; the existing edge it resolved to was returned
    # (and now lists this episode) but is not in the report.
    g, old_ep, ep = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4()), str(uuidlib.uuid4())
    existing = _edge(test_neo4j_repo, group_id=g, episodes=[old_ep, ep])
    extracted = str(uuidlib.uuid4())
    rep = _report(extracted)
    await _persist(driver, rep, episode=ep, group=g, edges=[existing])
    assert _contract(test_neo4j_repo, existing)["contract"] is None
    assert rep.persist == "no_rows"
    # Even if the extracted uuid were reported as returned, it matches no edge: no error.
    await _persist(driver, rep, episode=ep, group=g, edges=[extracted])
    assert (rep.persist, rep.persisted) == ("ok", 0)


async def test_report_never_writes_an_edge_of_another_episode(driver, test_neo4j_repo):
    # Attempt 1 timed out locally but committed; attempt 2 must not touch attempt 1's edges.
    g, ep1, ep2 = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4()), str(uuidlib.uuid4())
    attempt1 = _edge(test_neo4j_repo, group_id=g, episodes=[ep1])
    rep = _report(attempt1)
    await _persist(driver, rep, episode=ep2, group=g, edges=[attempt1])
    assert _contract(test_neo4j_repo, attempt1)["contract"] is None
    assert rep.persisted == 0


async def test_replay_is_a_no_op(driver, test_neo4j_repo):
    g, ep = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4())
    new = _edge(test_neo4j_repo, group_id=g, episodes=[ep])
    await _persist(driver, _report(new), episode=ep, group=g, edges=[new])
    second = _report(new, expression="different words")
    await _persist(driver, second, episode=ep, group=g, edges=[new])
    assert _contract(test_neo4j_repo, new)["expression"] == "a month ago"
    assert (second.persist, second.persisted) == ("ok", 0)


async def test_other_tenant_is_not_matched(driver, test_neo4j_repo):
    ep = str(uuidlib.uuid4())
    foreign = _edge(test_neo4j_repo, group_id=f"g-{uuidlib.uuid4().hex[:8]}", episodes=[ep])
    rep = _report(foreign)
    await _persist(driver, rep, episode=ep, group=f"g-{uuidlib.uuid4().hex[:8]}", edges=[foreign])
    assert _contract(test_neo4j_repo, foreign)["contract"] is None
    assert rep.persisted == 0


async def test_new_edge_expired_in_the_same_episode_still_gets_its_contract(driver, test_neo4j_repo):
    g, ep = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4())
    expired = _edge(test_neo4j_repo, group_id=g, episodes=[ep], expired=True)
    await _persist(driver, _report(expired), episode=ep, group=g, edges=[expired])
    assert _contract(test_neo4j_repo, expired)["contract"] is not None
