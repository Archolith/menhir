"""Rule 1 against a REAL Neo4j: a merge or an unmerge never makes content easier to delete.

Follows tests/test_merge_coordinator_live.py, test_delete_coordinator_live.py and
test_unmerge_coordinator_live.py. Each test fails on the pre-change code (survivor keeps its expired
TTL and low scope, so the sweep deletes it; a restored node keeps its expired TTL).
"""

from __future__ import annotations

import json
import os
import uuid as uuidlib

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.legacy_unmerge_coordinator import LegacyUnmergeCoordinator
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.unmerge_coordinator import UnmergeCoordinator

_PAST = "datetime() - duration({days: 1})"


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def journal(tmp_path):
    return GraphOperationsJournal(db_path=tmp_path / "saga.db")


@pytest.fixture
def merger(live_repo, journal):
    return MergeCoordinator(graph_adapter=CorrelationRepository(live_repo), journal=journal)


@pytest.fixture
def unmerger(live_repo, journal):
    return UnmergeCoordinator(graph_adapter=CorrelationRepository(live_repo), journal=journal)


@pytest.fixture
def sweeper(live_repo, journal):
    return DeleteCoordinator(graph_adapter=MemoryGraphAdapter(neo4j=live_repo), journal=journal)


@pytest.fixture
def make_pair(live_repo):
    """make_pair(s_scope, a_scope, s_expired=, a_expired=) -> ids. Cleans up by test_tag."""
    tags: list[str] = []

    def _make(s_scope, a_scope, *, s_expired=False, a_expired=False):
        tag = f"test-retention-{uuidlib.uuid4()}"
        tags.append(tag)
        ids = {"s": f"{tag}-s", "a": f"{tag}-a", "tag": tag}
        live_repo.execute(
            f"""
            CREATE (s:Entity {{uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                              freshness:'ACTIVE', summary:'short'}})
            CREATE (a:Entity {{uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                              freshness:'ACTIVE', summary:'short'}})
            SET s.scope = $s_scope, a.scope = $a_scope
            FOREACH (_ IN CASE WHEN $s_exp THEN [1] ELSE [] END | SET s.ttl_expires = {_PAST})
            FOREACH (_ IN CASE WHEN $a_exp THEN [1] ELSE [] END | SET a.ttl_expires = {_PAST})
            """,
            params={**ids, "t": tag, "s_scope": s_scope, "a_scope": a_scope,
                    "s_exp": s_expired, "a_exp": a_expired},
        )
        return ids

    yield _make
    for tag in tags:
        live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def _node(live_repo, uuid):
    rows = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.scope AS scope, n.ttl_expires AS ttl, "
        "n.promoted_at AS promoted_at",
        params={"u": uuid},
    )
    return rows[0] if rows else None


# --------------------------------------------------------------------------- merge raises retention

@pytest.mark.online
def test_persistent_absorbed_into_expired_session_survives_the_sweep(
    merger, sweeper, live_repo, make_pair
):
    ids = make_pair("SESSION", "PERSISTENT", s_expired=True)

    assert merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)["merged"] == 1
    node = _node(live_repo, ids["s"])
    assert node["scope"] == "PERSISTENT" and node["ttl"] is None and node["promoted_at"] is not None

    res = sweeper.delete_expired_session_nodes()
    assert ids["s"] not in res.get("deleted", [])
    assert _node(live_repo, ids["s"])["scope"] == "PERSISTENT"


@pytest.mark.online
def test_session_absorbed_into_expired_session_stays_session_without_ttl(
    merger, sweeper, live_repo, make_pair
):
    ids = make_pair("SESSION", "SESSION", s_expired=True, a_expired=True)

    assert merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)["merged"] == 1
    sweeper.delete_expired_session_nodes()

    node = _node(live_repo, ids["s"])
    assert node is not None, "the survivor holds the absorbed content and must outlive the sweep"
    assert node["scope"] == "SESSION" and node["ttl"] is None


@pytest.mark.online
def test_session_absorbed_into_persistent_stays_persistent(merger, live_repo, make_pair):
    ids = make_pair("PERSISTENT", "SESSION", a_expired=True)

    assert merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)["merged"] == 1
    node = _node(live_repo, ids["s"])
    assert node["scope"] == "PERSISTENT" and node["ttl"] is None
    assert node["promoted_at"] is None, "promoted_at is set only when the survivor rises"


@pytest.mark.online
def test_stored_scope_case_and_whitespace_is_ranked_and_written_canonically(
    merger, live_repo, make_pair
):
    ids = make_pair(" session ", " Persistent ")

    assert merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)["merged"] == 1
    assert _node(live_repo, ids["s"])["scope"] == "PERSISTENT"


# --------------------------------------------------------------------------- the sweep race

@pytest.mark.online
def test_merge_committing_between_selection_and_delete_spares_the_survivor(
    merger, sweeper, live_repo, make_pair
):
    ids = make_pair("SESSION", "SESSION", s_expired=True)
    real_select = sweeper.graph_adapter.fetch_ttl_expired_session_uuids

    def select_then_merge(*args, **kwargs):
        rows = real_select(*args, **kwargs)  # the sweep has selected S
        assert ids["s"] in {r["uuid"] for r in rows}
        # A merge into S commits and releases its lock before the delete statement runs.
        assert merger.merge(
            survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97
        )["merged"] == 1
        return rows

    sweeper.graph_adapter.fetch_ttl_expired_session_uuids = select_then_merge  # type: ignore[method-assign]
    res = sweeper.delete_expired_session_nodes()

    assert ids["s"] in res["skipped"], "reported skipped, not claimed deleted"
    assert ids["s"] not in res["deleted"]
    assert _node(live_repo, ids["s"]) is not None


# --------------------------------------------------------------------------- replay

@pytest.mark.online
def test_prepared_merge_replays_with_the_live_scope_of_the_absorbed_node(
    merger, live_repo, make_pair, monkeypatch
):
    ids = make_pair("SESSION", "SESSION", s_expired=True)
    monkeypatch.setattr(
        merger.graph_adapter, "merge_entity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("neo4j outage")),
    )
    with pytest.raises(RuntimeError):
        merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)
    monkeypatch.undo()
    assert len(merger.journal.list_by_state("PREPARED")) == 1

    # The absorbed node is promoted after PREPARE and before the row is replayed.
    live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) SET n.scope = 'PERSISTENT', n.ttl_expires = null",
        params={"u": ids["a"]},
    )
    out = merger._replay_prepared()

    assert out["replayed"] == 1, out
    node = _node(live_repo, ids["s"])
    assert node["scope"] == "PERSISTENT" and node["ttl"] is None


# --------------------------------------------------------------------------- unmerge

@pytest.mark.online
def test_unmerge_of_a_node_with_an_expired_snapshot_ttl_survives_the_sweep(
    merger, unmerger, sweeper, live_repo, make_pair
):
    """The snapshot holds a ttl_expires in the past; the restored node must not be sweepable."""
    ids = make_pair("SESSION", "SESSION", s_expired=True, a_expired=True)
    merged = merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)
    assert merged["merged"] == 1

    assert unmerger.unmerge(merged["op_id"])["restored"] == 1
    assert _node(live_repo, ids["a"])["ttl"] is None

    res = sweeper.delete_expired_session_nodes()
    assert ids["a"] not in res.get("deleted", [])
    assert _node(live_repo, ids["a"]) is not None


@pytest.mark.online
def test_unmerge_does_not_lower_a_survivor_promoted_after_the_merge(
    merger, unmerger, live_repo, make_pair
):
    ids = make_pair("SESSION", "SESSION")
    merged = merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)
    assert merged["merged"] == 1
    live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) SET n.scope = 'PERSISTENT'", params={"u": ids["s"]}
    )

    assert unmerger.unmerge(merged["op_id"])["restored"] == 1
    node = _node(live_repo, ids["s"])
    assert node["scope"] == "PERSISTENT" and node["ttl"] is None


@pytest.mark.online
def test_legacy_unmerge_restores_a_node_that_the_sweep_cannot_select(
    live_repo, journal, sweeper, make_pair
):
    """Legacy snapshots never carried ttl_expires (not in LEGACY_PROPERTY_ALLOWLIST), so this
    guards the guarantee rather than a pre-change failure: the restore statement is shared and
    now clears it unconditionally."""
    ids = make_pair("SESSION", "SESSION")
    live_repo.execute(
        "MATCH (n:Entity {uuid:$a}) DETACH DELETE n "
        "WITH 1 AS _ MATCH (s:Entity {uuid:$s}) SET s.merged_from = [$a]",
        params=ids,
    )
    entry = {
        "snapshot_at": "2026-01-01T00:00:00Z", "absorbed_uuid": ids["a"],
        "survivor_uuid": ids["s"], "similarity": 0.97,
        "properties": {
            "uuid": ids["a"], "name": "absorbed", "scope": "SESSION", "namespace": "default",
            "ttl_expires": "2026-01-01T00:00:00Z",
        },
        "relationships": [], "mentioned_by_episodes": [], "survivor_episodes_before": [],
    }

    class _Audit:
        def fetch_merge_audit(self, *, absorbed_uuid=None, survivor_uuid=None, limit=1):
            return [{"survivor_uuid": ids["s"], "absorbed_uuid": ids["a"], "similarity": 0.97,
                     "snapshot_json": json.dumps(entry)}]

    coord = LegacyUnmergeCoordinator(
        graph_adapter=CorrelationRepository(live_repo), journal=journal, telemetry_store=_Audit()
    )
    res = coord.unmerge_legacy(ids["a"], manifest=[ids["a"]], acknowledge_degraded=True)
    assert res["restored"] == 1, res
    assert _node(live_repo, ids["a"])["ttl"] is None
    sweeper.delete_expired_session_nodes()
    assert _node(live_repo, ids["a"]) is not None


# --------------------------------------------------------------------------- refused scopes

_REFUSED = ["CANDIDATE", None, "ARCHIVED"]


def _set_scope(live_repo, uuid, scope):
    live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) SET n.scope = $s", params={"u": uuid, "s": scope}
    )  # a null parameter removes the property


@pytest.mark.online
@pytest.mark.parametrize("bad", _REFUSED)
@pytest.mark.parametrize("side", ["s", "a"])
def test_refused_scope_is_refused_in_preflight(merger, live_repo, make_pair, bad, side):
    ids = make_pair("SESSION", "SESSION")
    _set_scope(live_repo, ids[side], bad)

    verdict = CorrelationRepository(live_repo).evaluate_merge_eligibility(ids["s"], ids["a"])
    assert verdict.allowed is False
    assert verdict.reason_code == me.UNMERGEABLE_SCOPE

    res = merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["a"], similarity=0.97)
    assert res["merged"] == 0 and res["reason"] == me.UNMERGEABLE_SCOPE
    assert _node(live_repo, ids["a"]) is not None


@pytest.mark.online
@pytest.mark.parametrize("bad", _REFUSED)
@pytest.mark.parametrize("side", ["s", "a"])
def test_scope_changed_after_preflight_is_refused_in_the_mutation(live_repo, make_pair, bad, side):
    ids = make_pair("SESSION", "SESSION")
    repo = CorrelationRepository(live_repo)
    real_eval = repo.evaluate_merge_eligibility

    def eval_then_change(s, a):
        verdict = real_eval(s, a)
        assert verdict.allowed is True
        _set_scope(live_repo, ids[side], bad)  # another writer, after preflight
        return verdict

    repo.evaluate_merge_eligibility = eval_then_change  # type: ignore[method-assign]
    res = repo.merge_entity(ids["s"], ids["a"], similarity=0.97)

    assert res["merged"] == 0
    assert res["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"
    assert _node(live_repo, ids["a"]) is not None, "no partial merge"


# --------------------------------------------------------------------------- unjournaled writers

@pytest.mark.online
@pytest.mark.parametrize("new_scope,side,expected", [
    ("PERSISTENT", "s", "PERSISTENT"),
    ("PERSISTENT", "a", "PERSISTENT"),
    ("PROMOTED", "s", None),
    ("PROMOTED", "a", None),
])
def test_scope_committed_while_the_merge_waits_is_not_overwritten(
    live_repo, make_pair, new_scope, side, expected
):
    """Promotion takes no journal lock. It holds the node's write lock here while the merge starts,
    so a merge that read scope before locking would act on, and write back, the old value."""
    import threading
    import time

    from neo4j import GraphDatabase

    ids = make_pair("SESSION", "SESSION")
    repo = CorrelationRepository(live_repo)
    result: dict = {}

    def run_merge():
        try:
            result.update(repo.merge_entity(ids["s"], ids["a"], similarity=0.97))
        except Exception as exc:  # noqa: BLE001 -- reported by the assertion below
            result["error"] = repr(exc)

    driver = GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(
            os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
            os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
        ),
    )
    try:
        with driver.session() as session:
            tx = session.begin_transaction()
            tx.run("MATCH (n:Entity {uuid:$u}) SET n.scope = $c", u=ids[side], c=new_scope).consume()
            worker = threading.Thread(target=run_merge)
            worker.start()
            time.sleep(2)  # the merge has passed its preflight reads and waits on the lock
            assert worker.is_alive(), result
            tx.commit()
            worker.join(60)
            assert not worker.is_alive()
    finally:
        driver.close()

    assert "error" not in result, result
    survivor, absorbed = _node(live_repo, ids["s"]), _node(live_repo, ids["a"])
    if expected is None:
        assert result["merged"] == 0 and absorbed is not None
        assert _node(live_repo, ids[side])["scope"] == "PROMOTED"
    else:
        assert result["merged"] == 1 and absorbed is None
        assert survivor["scope"] == expected
