"""LEGACY_ENTITY_UNMERGE + recoverability inventory against a REAL Neo4j (plan Phase 5, online).

The historical snapshot is lossy, so this lane can only PARTIALLY reverse a merge. The tests below
exist to pin the thing that actually matters: it must never present that partial restoration as a
recovery. Every result carries exact=False and an explicit list of what it could not bring back, it
refuses to touch anything outside the manifest, and it refuses to run at all until the degradation is
acknowledged.
"""

from __future__ import annotations

import json
import sqlite3
import uuid as uuidlib

import pytest

from menhir.domain import legacy_snapshot as ls
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services import legacy_unmerge_coordinator as luc
from menhir.services.legacy_unmerge_coordinator import LegacyUnmergeCoordinator
from menhir.services.merge_recoverability import (
    EXACT,
    GRAPH_SNAPSHOT_ONLY,
    LEGACY_SIDECAR,
    LINEAGE_ONLY,
    inventory,
)


class _FakeAuditStore:
    """Stands in for the telemetry sidecar's merge_audit table (isolated per test)."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetch_merge_audit(self, *, survivor_uuid=None, absorbed_uuid=None, limit=100):
        out = [
            r for r in self._rows
            if (absorbed_uuid is None or r["absorbed_uuid"] == absorbed_uuid)
            and (survivor_uuid is None or r["survivor_uuid"] == survivor_uuid)
        ]
        return out[:limit]


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def journal(tmp_path):
    return GraphOperationsJournal(db_path=tmp_path / "saga.db")


@pytest.fixture
def legacy_merged(live_repo):
    """A graph in the state a LEGACY merge left behind: absorbed node gone, survivor carrying the
    lineage, and a lossy audit entry as the only record."""
    tag = f"test-legacy-unmerge-{uuidlib.uuid4()}"
    s, a, p, ep = f"{tag}-s", f"{tag}-a", f"{tag}-p", f"{tag}-ep"
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          merged_from: [$a], summary:'merged summary'})
        CREATE (p:Entity {uuid:$p, name:'peer', test_tag:$t})
        CREATE (e:Episodic {uuid:$ep, name:'episode', test_tag:$t})
        CREATE (e)-[:MENTIONS]->(s)
        CREATE (s)-[:RELATES_TO {bridged_from: $a, type:'bridged'}]->(p)
        """,
        params={"s": s, "a": a, "p": p, "ep": ep, "t": tag},
    )
    entry = {
        "snapshot_at": "2026-01-01T00:00:00Z",
        "absorbed_uuid": a,
        "survivor_uuid": s,
        "similarity": 0.97,
        "properties": {
            "uuid": a, "name": "absorbed", "summary": "abs summary", "content": "abs content",
            "source": "codex", "source_confidence": 0.8, "type": "SEMANTIC",
            "scope": "PERSISTENT", "created_at": "2026-01-01T00:00:00Z", "namespace": "default",
        },
        "relationships": [
            {"peer_uuid": p, "rel_type": "RELATES_TO", "weight": 0.9, "direction": "out"},
        ],
        "mentioned_by_episodes": [ep],
        "survivor_episodes_before": [],
    }
    yield {
        "survivor": s, "absorbed": a, "peer": p, "episode": ep, "tag": tag,
        "store": _FakeAuditStore([
            {"survivor_uuid": s, "absorbed_uuid": a, "similarity": 0.97,
             "snapshot_json": json.dumps(entry)}
        ]),
        "entry": entry,
    }
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


@pytest.fixture
def coord(live_repo, journal, legacy_merged):
    return LegacyUnmergeCoordinator(
        graph_adapter=CorrelationRepository(live_repo),
        journal=journal,
        telemetry_store=legacy_merged["store"],
    )


def _exists(live_repo, uuid: str) -> bool:
    return int(live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": uuid}
    )[0]["c"]) > 0


# --------------------------------------------------------------------------- refusals

@pytest.mark.online
def test_refuses_anything_outside_the_manifest(coord, live_repo, legacy_merged):
    res = coord.unmerge_legacy(
        legacy_merged["absorbed"], manifest=["some-other-uuid"], acknowledge_degraded=True
    )
    assert res["restored"] == 0
    assert res["reason"] == luc.NOT_IN_MANIFEST
    assert not _exists(live_repo, legacy_merged["absorbed"])


@pytest.mark.online
def test_refuses_until_degradation_is_acknowledged(coord, live_repo, legacy_merged):
    a = legacy_merged["absorbed"]
    res = coord.unmerge_legacy(a, manifest=[a])  # acknowledge_degraded defaults to False

    assert res["restored"] == 0
    assert res["reason"] == luc.DEGRADATION_NOT_ACKNOWLEDGED
    assert res["exact"] is False
    # It must SAY what it cannot restore, before it is allowed to do anything.
    assert ls.OMITS_SURVIVOR_PRE_MERGE_STATE in res["degradations"]
    assert ls.OMITS_NODE_LABELS in res["degradations"]
    assert res["would_restore"]["survivor_restored"] is False
    assert not _exists(live_repo, a)


@pytest.mark.online
def test_lineage_only_merge_is_not_recoverable(live_repo, journal):
    """No snapshot anywhere: refuse. Never reconstruct a before-state from the survivor."""
    coord = LegacyUnmergeCoordinator(
        graph_adapter=CorrelationRepository(live_repo),
        journal=journal,
        telemetry_store=_FakeAuditStore([]),
    )
    res = coord.unmerge_legacy("ghost", manifest=["ghost"], acknowledge_degraded=True)
    assert res["restored"] == 0
    assert res["reason"] == luc.NO_LEGACY_SNAPSHOT
    assert "NOT recoverable" in res["diagnostics"]["note"]


# --------------------------------------------------------------------------- degraded restore

@pytest.mark.online
def test_degraded_restore_never_claims_exactness(coord, live_repo, legacy_merged):
    a, s, p = legacy_merged["absorbed"], legacy_merged["survivor"], legacy_merged["peer"]

    res = coord.unmerge_legacy(a, manifest=[a], acknowledge_degraded=True)

    assert res["restored"] == 1
    # The load-bearing assertion of this entire lane.
    assert res["exact"] is False
    assert res["survivor_restored"] is False
    assert ls.OMITS_SURVIVOR_PRE_MERGE_STATE in res["degradations"]

    # It did restore what it could: the node, its allowlisted properties, its Entity edge.
    assert _exists(live_repo, a)
    row = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.name AS name, labels(n) AS labels, "
        "n.summary AS summary", params={"u": a},
    )[0]
    assert row["name"] == "absorbed"
    assert row["labels"] == ["Entity"]  # labels were never recorded; :Entity is the assumption

    peers = live_repo.execute(
        "MATCH (:Entity {uuid:$a})-[:RELATES_TO]->(x) RETURN collect(x.uuid) AS p", params={"a": a}
    )[0]["p"]
    assert p in peers

    # Provenance came back to the absorbed node, and this merge's bridge is gone.
    eps = live_repo.execute(
        "MATCH (e:Episodic)-[:MENTIONS]->(:Entity {uuid:$a}) RETURN collect(e.uuid) AS e",
        params={"a": a},
    )[0]["e"]
    assert legacy_merged["episode"] in eps
    assert live_repo.execute(
        "MATCH (:Entity {uuid:$s})-[b:RELATES_TO {bridged_from:$a}]-() RETURN count(b) AS c",
        params={"s": s, "a": a},
    )[0]["c"] == 0

    # Lineage cleared, and the survivor's summary is UNCHANGED -- we did not guess at its prior value.
    surv = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN coalesce(n.merged_from, []) AS mf, n.summary AS summary",
        params={"u": s},
    )[0]
    assert a not in surv["mf"]
    assert surv["summary"] == "merged summary", "the survivor must not be fabricated back"

    assert coord.journal.get(res["op_id"])["state"] == "COMMITTED"


@pytest.mark.online
def test_missing_peer_refuses(coord, live_repo, legacy_merged):
    live_repo.execute(
        "MATCH (p:Entity {uuid:$u}) DETACH DELETE p", params={"u": legacy_merged["peer"]}
    )
    a = legacy_merged["absorbed"]
    res = coord.unmerge_legacy(a, manifest=[a], acknowledge_degraded=True)
    assert res["restored"] == 0
    assert res["reason"] == luc.MISSING_PEER
    assert not _exists(live_repo, a)


# --------------------------------------------------------------------------- inventory

@pytest.mark.online
def test_inventory_classifies_by_what_can_actually_be_recovered(live_repo, journal, legacy_merged):
    report = inventory(
        neo4j=live_repo, journal=journal, telemetry_store=legacy_merged["store"]
    )
    by_uuid = {r["absorbed_uuid"]: r["tier"] for r in report.rows}
    assert by_uuid[legacy_merged["absorbed"]] == LEGACY_SIDECAR
    assert report.counts[LEGACY_SIDECAR] >= 1


@pytest.mark.online
def test_inventory_reports_lineage_only_as_unrecoverable(live_repo, journal):
    tag = f"test-inv-{uuidlib.uuid4()}"
    s, a = f"{tag}-s", f"{tag}-a"
    live_repo.execute(
        "CREATE (s:Entity {uuid:$s, test_tag:$t, merged_from:[$a]})",
        params={"s": s, "a": a, "t": tag},
    )
    try:
        report = inventory(neo4j=live_repo, journal=journal, telemetry_store=_FakeAuditStore([]))
        by_uuid = {r["absorbed_uuid"]: r["tier"] for r in report.rows}
        assert by_uuid[a] == LINEAGE_ONLY
        assert report.not_recoverable >= 1
        assert "NOT recoverable" in report.summary()
    finally:
        live_repo.execute("MATCH (n) WHERE n.test_tag=$t DETACH DELETE n", params={"t": tag})


@pytest.mark.online
def test_inventory_flags_graph_only_snapshots_as_a_wasting_asset(live_repo, journal):
    """A snapshot that lives ONLY on the survivor dies with it -- the inventory must say so."""
    tag = f"test-inv-graph-{uuidlib.uuid4()}"
    s, a = f"{tag}-s", f"{tag}-a"
    live_repo.execute(
        "CREATE (s:Entity {uuid:$s, test_tag:$t, merged_from:[$a], "
        "merge_audit:[$blob]})",
        params={"s": s, "a": a, "t": tag, "blob": json.dumps({"absorbed_uuid": a})},
    )
    try:
        report = inventory(neo4j=live_repo, journal=journal, telemetry_store=_FakeAuditStore([]))
        by_uuid = {r["absorbed_uuid"]: r["tier"] for r in report.rows}
        assert by_uuid[a] == GRAPH_SNAPSHOT_ONLY
        assert "WASTING" in report.summary()
        assert "backfill_merge_audit" in report.summary()
    finally:
        live_repo.execute("MATCH (n) WHERE n.test_tag=$t DETACH DELETE n", params={"t": tag})
