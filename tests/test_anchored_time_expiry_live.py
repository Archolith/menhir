"""Graph-backed counterexamples for P3 expiry (P3b: the fork's edge_expiry_hook).

Edges are saved through Graphiti's own ``EntityEdge.save`` so the stored ``invalid_at`` has the
type and zone Graphiti writes. The episode chain runs Graphiti's real ``resolve_extracted_edge``
with ``MenhirEdgeExpiryHook`` against edges loaded from and saved back to Neo4j. The persist
cases assert the statement only ever stamps a live edge and never clears ``expired_at``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
import threading
import time
import uuid as uuidlib

import pytest

from menhir.domain.namespace import namespace_spellings, tenant_scope_cypher
from menhir.infrastructure.anchored_time import AnchoredTimeReport, AnchoredTimeResult
from menhir.infrastructure.anchored_time_persist import (
    CONTRACT_PROPERTIES,
    EXPIRY_PERSIST_CYPHER,
    LOCK_PROPERTY,
    contract_rows,
    persist_anchored_time,
)

pytestmark = pytest.mark.online

SPEECH = date(2024, 4, 10)
TRIP_START = datetime(2024, 3, 27, tzinfo=timezone.utc)
TRIP_END = datetime(2024, 4, 1, tzinfo=timezone.utc)
PATH_ONE_AT = datetime(2024, 4, 10, 18, 31, tzinfo=timezone.utc)
_BLOCK_WAIT_S = 20.0


def _result(uuid: str, *, world_end: datetime | None = TRIP_END, kind: str = "point_event") -> AnchoredTimeResult:
    return AnchoredTimeResult(
        edge_uuid=uuid, fact_index=0, expression="from March 27 to April 1", basis="explicit_date",
        kind=kind, granularity="day", window_start=date(2024, 3, 27), window_end=date(2024, 4, 1),
        anchor_ref=None, anchor_offset=None, planned_window=None, guard=None, written=False,
        reason="graphiti_inside_window", graphiti_valid_at=TRIP_START, new_valid_at=None,
        world_end=world_end,
    )


def _report(*results: AnchoredTimeResult, expiry: bool = True) -> AnchoredTimeReport:
    return AnchoredTimeReport(status="ok", model="m", results=tuple(results), expiry=expiry)


@pytest.fixture
async def driver(test_neo4j_repo):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    drv = Neo4jDriver(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                      os.environ["NEO4J_PASSWORD"], os.environ.get("NEO4J_DATABASE", "neo4j"))
    if drv._init_task is not None:
        await drv._init_task
    yield drv
    await drv.close()


async def _saved_edge(driver, repo, *, group_id: str, episodes: list[str],
                      invalid_at: datetime | None = TRIP_END,
                      expired_at: datetime | None = None) -> str:
    """An ended edge as Graphiti saves it under WORLD_END (own end, not expired)."""
    from graphiti_core.edges import EntityEdge

    a, b = str(uuidlib.uuid4()), str(uuidlib.uuid4())
    repo.execute("CREATE (:Entity {uuid: $a, group_id: $g}), (:Entity {uuid: $b, group_id: $g})",
                 params={"a": a, "b": b, "g": group_id})
    edge = EntityEdge(
        uuid=str(uuidlib.uuid4()), group_id=group_id, source_node_uuid=a, target_node_uuid=b,
        created_at=PATH_ONE_AT, name="RELATES_TO", fact="The user was in Rome.", episodes=episodes,
        valid_at=TRIP_START, invalid_at=invalid_at, expired_at=expired_at,
        fact_embedding=[0.1, 0.2, 0.3, 0.4],
    )
    await edge.save(driver)
    return edge.uuid


def _state(repo, uid: str) -> dict:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.expired_at AS expired_at, "
        "r.invalid_at AS invalid_at, r.valid_at AS valid_at, r.time_contract AS contract, "
        f"r.time_expiry AS expiry, r.time_world_end AS world_end, r.{LOCK_PROPERTY} AS lock",
        params={"u": uid},
    )
    assert len(rows) == 1
    return rows[0]


async def _persist(driver, report, *, episode: str, group: str, edges: list[str]) -> None:
    await persist_anchored_time(driver, report, episode_uuid=episode, group_id=group,
                                edge_uuids=edges, speech_date=SPEECH)


def _ids() -> tuple[str, str]:
    return f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4())


async def test_live_ended_edge_is_stamped_and_stays_live(driver, test_neo4j_repo):
    g, ep = _ids()
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep])
    rep = _report(_result(uid))
    await _persist(driver, rep, episode=ep, group=g, edges=[uid])
    state = _state(test_neo4j_repo, uid)
    assert state["expired_at"] is None
    assert (state["expiry"], state["world_end"]) == ("world_end", TRIP_END.isoformat())
    assert state["contract"].startswith("at1|") and state["lock"] is None
    assert state["invalid_at"].to_native() == TRIP_END  # world end kept
    assert (rep.persist, rep.persisted, rep.stamped) == ("ok", 1, 1)


async def test_expired_edge_is_never_unexpired(driver, test_neo4j_repo):
    # P3b: whatever Graphiti expired (hook said EXPIRE, or a contradiction) stays expired.
    g, ep = _ids()
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep], expired_at=PATH_ONE_AT)
    rep = _report(_result(uid))
    await _persist(driver, rep, episode=ep, group=g, edges=[uid])
    state = _state(test_neo4j_repo, uid)
    assert state["expired_at"] is not None and state["expiry"] is None
    assert state["contract"] is not None  # P2 contract still written: it is world time
    assert (rep.persisted, rep.stamped) == (1, 0)


async def test_same_instant_in_another_zone_still_matches(driver, test_neo4j_repo):
    g, ep = _ids()
    chicago = TRIP_END.astimezone(timezone(timedelta(hours=-5)))
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep], invalid_at=chicago)
    rep = _report(_result(uid, world_end=TRIP_END))
    await _persist(driver, rep, episode=ep, group=g, edges=[uid])
    assert _state(test_neo4j_repo, uid)["expiry"] == "world_end" and rep.stamped == 1


async def test_contradiction_committed_before_persist_is_not_stamped(driver, test_neo4j_repo):
    g, ep = _ids()
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep])
    # Another episode's path 2/3 rewrote the end between save and persist.
    test_neo4j_repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.invalid_at = datetime('2024-03-30T00:00:00Z'), "
        "r.expired_at = datetime()", params={"u": uid})
    rep = _report(_result(uid))
    await _persist(driver, rep, episode=ep, group=g, edges=[uid])
    state = _state(test_neo4j_repo, uid)
    assert state["expired_at"] is not None and state["expiry"] is None and state["world_end"] is None
    assert state["contract"] is not None
    assert (rep.persisted, rep.stamped) == (1, 0)


async def test_sub_millisecond_move_is_not_stamped(driver, test_neo4j_repo):
    # Codex P3 review: epochMillis matched an invalid_at moved by 800 us. Still live here, so
    # only the full-precision equality keeps the stamp off.
    g, ep = _ids()
    end = TRIP_END + timedelta(microseconds=100)
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep], invalid_at=end)
    test_neo4j_repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.invalid_at = $moved",
        params={"u": uid, "moved": end + timedelta(microseconds=800)})
    rep = _report(_result(uid, world_end=end))
    await _persist(driver, rep, episode=ep, group=g, edges=[uid])
    assert _state(test_neo4j_repo, uid)["expiry"] is None and rep.stamped == 0


async def test_microsecond_world_end_still_matches(driver, test_neo4j_repo):
    g, ep = _ids()
    end = TRIP_END + timedelta(microseconds=123456)
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep], invalid_at=end)
    rep = _report(_result(uid, world_end=end))
    await _persist(driver, rep, episode=ep, group=g, edges=[uid])
    assert _state(test_neo4j_repo, uid)["expiry"] == "world_end" and rep.stamped == 1


async def test_gates_stamp_nothing(driver, test_neo4j_repo):
    g, ep = _ids()
    cases = {
        "flag_off": (_report(_result("{uid}"), expiry=False), ep, g),
        "plan": (_report(_result("{uid}", world_end=None, kind="plan")), ep, g),
        "other_episode": (_report(_result("{uid}")), str(uuidlib.uuid4()), g),
        "other_group": (_report(_result("{uid}")), ep, f"g-{uuidlib.uuid4().hex[:8]}"),
    }
    for name, (template, episode, group) in cases.items():
        uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep])
        rep = _report(*(replace(r, edge_uuid=uid) for r in template.results),
                      expiry=template.expiry)
        await _persist(driver, rep, episode=episode, group=group, edges=[uid])
        state = _state(test_neo4j_repo, uid)
        assert state["expired_at"] is None, name
        assert state["expiry"] is None and rep.stamped == 0, name


async def test_duplicate_writes_nothing(driver, test_neo4j_repo):
    # The extracted uuid resolved to an existing edge that now lists this episode.
    g, ep = _ids()
    existing = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[str(uuidlib.uuid4()), ep])
    rep = _report(_result(str(uuidlib.uuid4())))
    await _persist(driver, rep, episode=ep, group=g, edges=[existing])
    assert rep.persist == "no_rows"
    assert _state(test_neo4j_repo, existing)["expiry"] is None


async def test_replay_never_touches_a_later_contradiction(driver, test_neo4j_repo):
    g, ep = _ids()
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep])
    await _persist(driver, _report(_result(uid)), episode=ep, group=g, edges=[uid])
    assert _state(test_neo4j_repo, uid)["expiry"] == "world_end"
    # A later contradiction expires it, even leaving the same invalid_at (worst case).
    test_neo4j_repo.execute("MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.expired_at = datetime()",
                            params={"u": uid})
    replay = _report(_result(uid))
    await _persist(driver, replay, episode=ep, group=g, edges=[uid])
    assert _state(test_neo4j_repo, uid)["expired_at"] is not None
    assert (replay.persisted, replay.stamped) == (0, 0)


# --- plan section 4: save, restate as a duplicate, contradict inside the window ------------------

class _DedupeLLM:
    def __init__(self, contradicted: list[int], duplicates: list[int] | None = None) -> None:
        self.contradicted, self.duplicates = contradicted, duplicates or []

    async def generate_response(self, *args, **kwargs):
        return {"duplicate_facts": self.duplicates, "contradicted_facts": self.contradicted}


async def _resolve_in_call(edge, related, existing, llm, results, episode_uuid, group_id):
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
        report = AnchoredTimeReport(status="ok", model="m", owner=owner, expiry=True,
                                    results=tuple(results))
        receipt.anchored_time = report
        resolved, invalidated, _ = await resolve_extracted_edge(
            llm, edge, related, existing, episode, edge_expiry_hook=policy.MenhirEdgeExpiryHook())
        return resolved, invalidated, report
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()


async def test_episode_chain_restated_duplicate_stays_live_contradiction_expires(driver, test_neo4j_repo):
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

    # Episode 1 saves an ended trip; a candidate exists, so upstream would expire it (path 1).
    ep1 = str(uuidlib.uuid4())
    trip = new_edge("The user was in Rome.", rome, valid_at=TRIP_START, invalid_at=TRIP_END, episode=ep1)
    resolved, _, report = await _resolve_in_call(trip, [], [other], _DedupeLLM([]),
                                                 [_result(trip.uuid)], ep1, g)
    assert resolved is trip and resolved.expired_at is None and report.world_end_kept == 1
    await resolved.save(driver)
    await _persist(driver, report, episode=ep1, group=g, edges=[trip.uuid])
    assert (report.persist, report.stamped) == ("ok", 1)
    state = _state(test_neo4j_repo, trip.uuid)
    assert state["expired_at"] is None and state["expiry"] == "world_end"

    # Episode 2 restates it; it resolves to the stored edge as a duplicate (P3-1).
    ep2 = str(uuidlib.uuid4())
    stored = await EntityEdge.get_by_uuid(driver, trip.uuid)
    await stored.load_fact_embedding(driver)
    restated = new_edge("The user visited Rome.", rome, valid_at=TRIP_START, invalid_at=TRIP_END,
                        episode=ep2)
    resolved, _, report = await _resolve_in_call(restated, [stored], [], _DedupeLLM([], [0]),
                                                 [_result(restated.uuid)], ep2, g)
    assert resolved.uuid == trip.uuid and resolved.expired_at is None
    await resolved.save(driver)
    await _persist(driver, report, episode=ep2, group=g, edges=[resolved.uuid])
    assert report.persist == "no_rows"
    state = _state(test_neo4j_repo, trip.uuid)
    assert state["expired_at"] is None and state["invalid_at"].to_native() == TRIP_END
    # Pre-existing (P2): the resolver clears attributes and EntityEdge.save replaces all edge
    # properties, so a restatement drops the first episode's contract and stamp. Expiry and the
    # "ended" role read only expired_at/invalid_at.
    assert state["contract"] is None and state["expiry"] is None

    # Episode 3 contradicts it inside the window (Milan from March 28): the trip is expired.
    ep3 = str(uuidlib.uuid4())
    stored = await EntityEdge.get_by_uuid(driver, trip.uuid)
    await stored.load_fact_embedding(driver)
    in_milan = new_edge("The user was in Milan.", milan,
                        valid_at=datetime(2024, 3, 28, tzinfo=timezone.utc), episode=ep3)
    resolved, invalidated, _ = await _resolve_in_call(
        in_milan, [], [stored], _DedupeLLM([0]), [_result(in_milan.uuid, world_end=None)], ep3, g)
    assert [e.uuid for e in invalidated] == [trip.uuid]
    for edge in [resolved, *invalidated]:
        await edge.save(driver)
    state = _state(test_neo4j_repo, trip.uuid)
    assert state["expired_at"] is not None
    assert state["invalid_at"].to_native() == datetime(2024, 3, 28, tzinfo=timezone.utc)


#: Control: decides the stamp from the pre-lock read, then locks and writes. Under the observed
#: schedule it must stamp the contradicted edge, which shows the harness exercises the race.
_ELIGIBLE = (tenant_scope_cypher("r") + " AND r.group_id = $group_id AND r.time_contract IS NULL "
             "AND $episode_uuid IN coalesce(r.episodes, [])")
PRE_LOCK_EXPIRY_CYPHER = (
    "UNWIND $rows AS row "
    "MATCH ()-[r:RELATES_TO {uuid: row.uuid}]->() "
    "WHERE " + _ELIGIBLE + " "
    "WITH r, row, (r.expired_at IS NULL "
    "AND datetime(r.invalid_at).epochSeconds = row.time_world_end_s "
    "AND datetime(r.invalid_at).nanosecond = row.time_world_end_ns) AS stamp "
    f"SET r.{LOCK_PROPERTY} = true "
    f"REMOVE r.{LOCK_PROPERTY} "
    "SET " + ", ".join(f"r.{name} = row.{name}" for name in CONTRACT_PROPERTIES) + " "
    "FOREACH (_ IN CASE WHEN stamp THEN [1] ELSE [] END | "
    "SET r.time_expiry = 'world_end') "
    "RETURN count(r) AS written, sum(CASE WHEN stamp THEN 1 ELSE 0 END) AS stamped"
)


def _race(uid: str, g: str, ep: str, statement: str) -> dict:
    """Contradiction writer A holds the edge lock; persist B starts, is seen blocked, A commits."""
    neo4j = pytest.importorskip("neo4j")
    tag = uuidlib.uuid4().hex
    rows = contract_rows(_report(_result(uid)), [uid], SPEECH)
    params = {"rows": rows, "group_id": g, "episode_uuid": ep,
              "tenant_namespaces": namespace_spellings(g)}
    database = os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")
    drv = neo4j.GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
              os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")))
    result: dict = {}
    try:
        session_a = drv.session(database=database)
        tx_a = session_a.begin_transaction(metadata={"p3": f"{tag}-a"})
        tx_a.run("MATCH ()-[r:RELATES_TO {uuid: $u}]->() "
                 "SET r.invalid_at = datetime('2024-03-30T00:00:00Z'), r.expired_at = datetime()",
                 u=uid).consume()

        @neo4j.unit_of_work(metadata={"p3": f"{tag}-b"})
        def write_b(tx):
            return tx.run(statement, params).single().data()

        def writer_b() -> None:
            try:
                with drv.session(database=database) as session_b:
                    result["b"] = session_b.execute_write(write_b)
            except Exception as exc:  # surfaced by the assertions below
                result["error_b"] = exc

        thread = threading.Thread(target=writer_b)
        thread.start()
        try:
            result["blocked"] = _wait_until_blocked(drv, database, tag)
        finally:
            tx_a.commit()
            session_a.close()
            thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        drv.close()
    return result


def _wait_until_blocked(drv, database: str, tag: str) -> dict | None:
    deadline = time.monotonic() + _BLOCK_WAIT_S
    with drv.session(database=database) as monitor:
        while time.monotonic() < deadline:
            rows = monitor.run(
                "SHOW TRANSACTIONS YIELD transactionId, status, metaData "
                "WHERE metaData.p3 IN [$a, $b] RETURN transactionId, status, metaData.p3 AS who",
                a=f"{tag}-a", b=f"{tag}-b").data()
            ids = {row["who"]: row for row in rows}
            a, b = ids.get(f"{tag}-a"), ids.get(f"{tag}-b")
            if a and b and str(b["status"]).startswith("Blocked by") \
                    and a["transactionId"] in str(b["status"]):
                return {"a": a["transactionId"], "b": b["transactionId"], "status": b["status"]}
            time.sleep(0.05)
    return None


async def test_contradiction_in_flight_wins_over_persist(driver, test_neo4j_repo):
    g, ep = _ids()
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep])
    result = _race(uid, g, ep, EXPIRY_PERSIST_CYPHER)
    assert result["blocked"], "persist was never observed blocked on the contradiction's lock"
    assert "error_b" not in result, result.get("error_b")
    assert result["b"] == {"written": 1, "stamped": 0}
    state = _state(test_neo4j_repo, uid)
    assert state["expired_at"] is not None and state["expiry"] is None
    assert state["lock"] is None


async def test_pre_lock_decision_loses_the_race(driver, test_neo4j_repo):
    g, ep = _ids()
    uid = await _saved_edge(driver, test_neo4j_repo, group_id=g, episodes=[ep])
    result = _race(uid, g, ep, PRE_LOCK_EXPIRY_CYPHER)
    assert result["blocked"], "persist was never observed blocked on the contradiction's lock"
    assert "error_b" not in result, result.get("error_b")
    assert result["b"] == {"written": 1, "stamped": 1}
    state = _state(test_neo4j_repo, uid)
    assert state["expired_at"] is not None and state["expiry"] == "world_end"  # wrongly stamped
