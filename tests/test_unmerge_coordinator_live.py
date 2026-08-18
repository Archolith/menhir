"""UnmergeCoordinator against a REAL Neo4j (remediation plan Phase 5, online).

The headline test is the ROUND TRIP: merge, then unmerge, and assert the graph is byte-for-byte what
it was -- exact labels, typed properties, every relationship instance (direction, type, properties,
parallel multiplicity), episode provenance, and the survivor's pre-merge state. That is the property
the legacy path could never provide: its snapshot was lossy and it could not reverse the survivor at
all ("no pre-merge survivor snapshot exists").

The rest prove the REFUSALS. An unmerge that cannot be exact must abstain, never approximate:
newer survivor state, a missing peer, a graph no longer in the merge's after-state.

Supersedes tests/test_unmerge_e2e.py, which covered the now-removed unaudited direct-write path in
scripts/unmerge.py. Every assertion there has a strictly stronger counterpart here:

    restores the absorbed node          -> test_merge_then_unmerge_restores_exactly (exact labels +
                                           typed properties, not a property allowlist)
    restores provenance/relationships   -> the same test's full relationship-instance set equality
                                           (direction, properties, and parallel multiplicity)
    no provenance on BOTH identities    -> survivor relationship-set equality
    keeps the survivor's own provenance -> survivor relationship-set equality
    clears survivor lineage             -> lineage assertion + forward merge marked REVERSED
    idempotent                          -> test_unmerge_is_idempotent

The old idempotency test asserted ``skipped == 1`` -- it enshrined the very skip-if-node-exists
behavior that left crashed unmerges permanently half-restored. The replacement asserts the node is
not duplicated AND that a completed restore is recognized, without ever skipping a partial one.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain import merge_delta as md
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services import unmerge_coordinator as uc
from menhir.services.unmerge_coordinator import UnmergeCoordinator


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
def pair(live_repo):
    """A rich subgraph: typed props, multiple labels, parallel edges, in+out edges, an Episodic peer
    on EACH identity (so the rebound-MENTIONS subtraction is actually exercised)."""
    tag = f"test-unmerge-coord-{uuidlib.uuid4()}"
    ids = {
        "s": f"{tag}-s", "a": f"{tag}-a", "p": f"{tag}-p",
        "ep_s": f"{tag}-ep-s", "ep_a": f"{tag}-ep-a",
    }
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          summary:'survivor summary', content:'survivor content',
                          source:'claude-code', source_confidence:0.6})
        CREATE (a:Entity:Extra {uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          // Deliberately RICHER than the survivor: this makes the merge actually
                          // overwrite the survivor's summary AND content, so the round trip proves
                          // the delta is reversed rather than passing vacuously on unchanged text.
                          summary:'a much longer absorbed summary that is clearly richer than the survivor text',
                          content:'a much longer absorbed content body that clearly exceeds the survivor by well over twenty percent',
                          source:'codex',
                          source_confidence:0.8,
                          when: datetime('2026-07-13T10:30:15.123456789Z'),
                          day: date('2026-07-13'),
                          dur: duration({months:2, days:3}),
                          pt: point({x:1.0, y:2.0}),
                          tags: ['x','y'], num: 42})
        CREATE (p:Entity {uuid:$p, name:'peer', test_tag:$t})
        CREATE (eps:Episodic {uuid:$ep_s, name:'survivor episode', test_tag:$t})
        CREATE (epa:Episodic {uuid:$ep_a, name:'absorbed episode', test_tag:$t})
        CREATE (eps)-[:MENTIONS]->(s)
        CREATE (epa)-[:MENTIONS {conf:0.7}]->(a)
        CREATE (a)-[:RELATES_TO {weight:0.9, kind:'first'}]->(p)
        CREATE (a)-[:RELATES_TO {weight:0.3, kind:'parallel'}]->(p)
        CREATE (p)-[:RELATES_TO {weight:0.5, kind:'incoming'}]->(a)
        """,
        params={**ids, "t": tag},
    )
    yield ids
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def _snapshot_graph(repo: CorrelationRepository, uuid: str) -> dict:
    """The node's complete state, via the same lossless capture the snapshot uses."""
    from menhir.domain import merge_snapshot as ms

    state = repo.capture_node_state(uuid)
    return ms.decode_node(state) if state else None


def _rel_key(rels: list[dict]) -> set:
    """Comparable identity of every relationship INSTANCE: type, direction, peer, and properties."""
    return {
        (
            r["type"], r["direction"], r["peer_uuid"],
            tuple(sorted((k, str(v)) for k, v in r["properties"].items())),
        )
        for r in rels
    }


# --------------------------------------------------------------------------- the round trip

@pytest.mark.online
def test_merge_then_unmerge_restores_exactly(merger, unmerger, live_repo, pair):
    repo = CorrelationRepository(live_repo)
    before_absorbed = _snapshot_graph(repo, pair["a"])
    before_survivor = _snapshot_graph(repo, pair["s"])

    merged = merger.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert merged["merged"] == 1
    merge_op = merged["op_id"]

    res = unmerger.unmerge(merge_op)
    assert res["restored"] == 1, res

    after_absorbed = _snapshot_graph(repo, pair["a"])
    after_survivor = _snapshot_graph(repo, pair["s"])

    # --- the absorbed node came back EXACTLY
    assert after_absorbed is not None, "absorbed node was not restored"
    assert after_absorbed["labels"] == before_absorbed["labels"], "labels must round-trip"

    # Properties: identical except the restore's own audit stamps and the system-managed exclusion.
    audit_keys = {"restored_from_merge", "restored_by_op", "restored_at"}
    from menhir.domain.merge_snapshot import SYSTEM_MANAGED_PROPERTIES

    before_props = {
        k: v for k, v in before_absorbed["properties"].items()
        if k not in SYSTEM_MANAGED_PROPERTIES
    }
    after_props = {
        k: v for k, v in after_absorbed["properties"].items()
        if k not in audit_keys and k not in SYSTEM_MANAGED_PROPERTIES
    }
    assert after_props == before_props, "typed properties must round-trip exactly"

    # Typed values are still driver types, not strings.
    from neo4j.time import Date, DateTime, Duration
    from neo4j.spatial import CartesianPoint

    assert isinstance(after_props["when"], DateTime)
    assert isinstance(after_props["day"], Date)
    assert isinstance(after_props["dur"], Duration)
    assert isinstance(after_props["pt"], CartesianPoint)
    assert after_props["tags"] == ["x", "y"]

    # --- every relationship instance came back: direction, type, properties, and MULTIPLICITY.
    assert _rel_key(after_absorbed["relationships"]) == _rel_key(before_absorbed["relationships"])
    parallel = [
        r for r in after_absorbed["relationships"]
        if r["type"] == "RELATES_TO" and r["peer_uuid"] == pair["p"] and r["direction"] == "out"
    ]
    assert len(parallel) == 2, "parallel edges must be restored as two distinct instances"

    # --- the survivor is back to its pre-merge state (the delta the merge owned was reversed).
    for key in md.MERGE_OWNED_SURVIVOR_PROPERTIES:
        assert (
            after_survivor["properties"].get(key) == before_survivor["properties"].get(key)
        ), key

    # --- provenance: the absorbed node's episode is its own again, and the merge's rebind onto the
    # survivor is gone, while the survivor's OWN episode is untouched.
    assert _rel_key(after_survivor["relationships"]) == _rel_key(before_survivor["relationships"])

    # --- lineage cleared and the forward merge marked REVERSED.
    lineage = live_repo.execute(
        "MATCH (s:Entity {uuid:$u}) RETURN coalesce(s.merged_from, []) AS mf", params={"u": pair["s"]}
    )[0]["mf"]
    assert pair["a"] not in lineage
    assert unmerger.journal.get(merge_op)["state"] == "REVERSED"
    assert unmerger.journal.get(res["op_id"])["state"] == "COMMITTED"


@pytest.fixture
def provenance_pair(live_repo):
    """Both identities already carry the FULL provenance set, including an explicit downgrade.

    The survivor is itself the product of an earlier merge (`sources` of two, `corroboration` 2), and
    the absorbed node is a `project-scan` row deliberately stamped at agent tier rather than the 0.9
    its label allows. Between them they exercise every merge-owned property AND the two derivations a
    label-only rule got wrong.
    """
    tag = f"test-unmerge-prov-{uuidlib.uuid4()}"
    ids = {"s": f"{tag}-s", "a": f"{tag}-a"}
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          summary:'survivor summary', content:'survivor content',
                          source:'codex', sources:['codex','claude-code'],
                          source_confidence:0.5, corroboration:2})
        CREATE (a:Entity {uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          summary:'a much longer absorbed summary that is clearly richer than the survivor text',
                          content:'a much longer absorbed content body that clearly exceeds the survivor by well over twenty percent',
                          source:'project-scan', sources:['project-scan'],
                          source_confidence:0.5, corroboration:1})
        """,
        params={**ids, "t": tag},
    )
    yield ids
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


@pytest.mark.online
def test_merge_then_unmerge_restores_all_six_merge_owned_properties(
    merger, unmerger, live_repo, provenance_pair
):
    """Plan Phase 4. The survivor read behind Guard 2 returned only four of the six properties, so
    `sources` and `corroboration` came back as None, never matched the replayed merge output, and
    EVERY new merge refused to unmerge with SURVIVOR_CHANGED_SINCE_MERGE. Nothing was wrong with the
    graph -- the guard was comparing against a read that could not see what the merge wrote.
    """
    repo = CorrelationRepository(live_repo)
    s, a = provenance_pair["s"], provenance_pair["a"]
    before = repo.fetch_survivor_properties(s)
    before_absorbed = _snapshot_graph(repo, a)

    merged = merger.merge(survivor_uuid=s, absorbed_uuid=a, similarity=0.97)
    assert merged["merged"] == 1

    # The merge really did rewrite provenance -- otherwise the round trip would pass vacuously.
    during = repo.fetch_survivor_properties(s)
    assert during["sources"] == ["codex", "claude-code", "project-scan"]
    assert during["corroboration"] == 3
    assert during["source"] == "codex"
    # The explicit downgrade survives: project-scan's ceiling is 0.9, both inputs were held at 0.5.
    assert during["source_confidence"] == 0.5
    assert during["summary"] != before["summary"] and during["content"] != before["content"]

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res

    after = repo.fetch_survivor_properties(s)
    for key in md.MERGE_OWNED_SURVIVOR_PROPERTIES:
        assert after[key] == before[key], f"{key} did not round-trip"

    # The absorbed node's own provenance came back too, not just the survivor's delta.
    after_absorbed = _snapshot_graph(repo, a)
    for key in ("source", "sources", "source_confidence", "corroboration"):
        assert (
            after_absorbed["properties"].get(key) == before_absorbed["properties"].get(key)
        ), key


@pytest.mark.online
def test_newer_provenance_still_blocks_the_unmerge(merger, unmerger, live_repo, provenance_pair):
    """Restoring all six must not loosen the newer-state protection: a post-merge change to one of
    the NEW provenance properties has to refuse exactly like a changed summary does."""
    s, a = provenance_pair["s"], provenance_pair["a"]
    merged = merger.merge(survivor_uuid=s, absorbed_uuid=a, similarity=0.97)
    live_repo.execute(
        "MATCH (s:Entity {uuid:$u}) SET s.sources = s.sources + ['opencode']", params={"u": s}
    )

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 0
    assert res["reason"] == uc.SURVIVOR_CHANGED_SINCE_MERGE
    assert "sources" in res["diagnostics"]["differences"]
    assert "opencode" in live_repo.execute(
        "MATCH (s:Entity {uuid:$u}) RETURN s.sources AS s", params={"u": s}
    )[0]["s"], "the newer value must survive"


@pytest.mark.online
def test_dry_run_reports_without_mutating(merger, unmerger, live_repo, pair):
    merged = merger.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    res = unmerger.unmerge(merged["op_id"], dry_run=True)

    assert res["restored"] == 0 and res["reason"] == "DRY_RUN"
    assert res["would_restore"]["out_relationships"] == 2  # two parallel RELATES_TO
    assert res["would_restore"]["in_relationships"] == 2    # peer -> a, and episode MENTIONS -> a
    assert live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": pair["a"]}
    )[0]["c"] == 0, "a dry run must not restore anything"


# --------------------------------------------------------------------------- refusals

@pytest.mark.online
def test_newer_survivor_state_is_preserved_not_clobbered(merger, unmerger, live_repo, pair):
    """Invariant 9: if someone edited the survivor after the merge, refuse -- never overwrite."""
    merged = merger.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    live_repo.execute(
        "MATCH (s:Entity {uuid:$u}) SET s.summary = 'a human edited this afterwards'",
        params={"u": pair["s"]},
    )

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 0
    assert res["reason"] == uc.SURVIVOR_CHANGED_SINCE_MERGE
    assert "summary" in res["diagnostics"]["differences"]
    # The newer value survives, and the absorbed node was NOT resurrected on top of it.
    assert live_repo.execute(
        "MATCH (s:Entity {uuid:$u}) RETURN s.summary AS s", params={"u": pair["s"]}
    )[0]["s"] == "a human edited this afterwards"
    assert live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": pair["a"]}
    )[0]["c"] == 0


@pytest.mark.online
def test_missing_peer_refuses_and_does_not_fabricate(merger, unmerger, live_repo, pair):
    """A peer the snapshot references no longer exists: report it, restore nothing."""
    merged = merger.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    live_repo.execute("MATCH (p:Entity {uuid:$u}) DETACH DELETE p", params={"u": pair["p"]})

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 0
    assert res["reason"] == uc.MISSING_PEER
    assert pair["p"] in res["diagnostics"]["missing_peers"]
    assert live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": pair["a"]}
    )[0]["c"] == 0, "all-or-nothing: nothing is restored when a peer is missing"


@pytest.mark.online
def test_unmerge_is_idempotent(merger, unmerger, live_repo, pair):
    merged = merger.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert unmerger.unmerge(merged["op_id"])["restored"] == 1

    again = unmerger.unmerge(merged["op_id"])
    assert again["restored"] == 0
    assert again["reason"] in (uc.ALREADY_RESTORED, uc.NOT_A_COMMITTED_MERGE)
    assert live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": pair["a"]}
    )[0]["c"] == 1, "the node must not be duplicated by a second unmerge"


@pytest.mark.online
def test_unjournaled_merge_cannot_be_unmerged(unmerger):
    res = unmerger.unmerge("no-such-op")
    assert res["restored"] == 0
    assert res["reason"] == uc.NOT_A_COMMITTED_MERGE


@pytest.mark.online
def test_crash_before_commit_replays_as_committed(merger, unmerger, live_repo, pair):
    """Crash between the atomic restore and COMMITTED: replay recognizes the restored state."""
    merged = merger.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)

    real_commit = unmerger.journal.mark_committed
    unmerger.journal.mark_committed = lambda op_id: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        unmerger.unmerge(merged["op_id"])
    unmerger.journal.mark_committed = real_commit  # type: ignore[method-assign]

    # The restore landed; the unmerge row is still PREPARED.
    assert live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": pair["a"]}
    )[0]["c"] == 1
    assert len(unmerger.journal.list_by_state("PREPARED")) == 1

    out = unmerger._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out == {"replayed": 1, "drifted": 0, "failed": 0}
    assert unmerger.journal.list_by_state("PREPARED") == []


@pytest.mark.online
def test_unmerge_preserves_other_absorptions_audit_when_nodes_were_related(
    live_repo, journal, merger, unmerger
):
    """Unmerging A must not delete B's audit entry just because A appears inside it.

    Regression for the review's Defect #1. The merge audit entry embeds the absorbed node's
    relationships, each with a `peer_uuid`. So when two RELATED nodes are both absorbed into the same
    survivor, one entry's JSON contains the other's uuid. The lineage cleanup used
    `WHERE NOT x CONTAINS $absorbed`, which is a substring match over the whole blob -- so unmerging A
    would also strip B's audit entry (A appears in it as a peer). That silently corrupts B's
    recoverability record. The fix matches the `absorbed_uuid` FIELD, not any occurrence of the uuid.
    """
    tag = f"test-unmerge-audit-{uuidlib.uuid4()}"
    s, a, b = f"{tag}-s", f"{tag}-a", f"{tag}-b"
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT'})
        CREATE (a:Entity {uuid:$a, name:'node a', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT'})
        CREATE (b:Entity {uuid:$b, name:'node b', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT'})
        CREATE (a)-[:RELATES_TO {weight:0.5}]->(b)
        """,
        params={"s": s, "a": a, "b": b, "t": tag},
    )
    try:
        # Absorb B first (while A still exists), so B's audit entry captures A as a peer_uuid.
        assert merger.merge(survivor_uuid=s, absorbed_uuid=b, similarity=0.97)["merged"] == 1
        # Then absorb A.
        a_merge = merger.merge(survivor_uuid=s, absorbed_uuid=a, similarity=0.97)
        assert a_merge["merged"] == 1

        # Sanity: B's audit entry really does contain A's uuid (else the test proves nothing).
        audit = live_repo.execute(
            "MATCH (s:Entity {uuid:$s}) RETURN coalesce(s.merge_audit, []) AS ma", params={"s": s}
        )[0]["ma"]
        b_entries = [x for x in audit if f'"absorbed_uuid": "{b}"' in x]
        assert b_entries and a in b_entries[0], "precondition: B's entry must reference A as a peer"

        # Unmerge A. B's audit entry must survive.
        assert unmerger.unmerge(a_merge["op_id"])["restored"] == 1

        audit_after = live_repo.execute(
            "MATCH (s:Entity {uuid:$s}) RETURN coalesce(s.merge_audit, []) AS ma", params={"s": s}
        )[0]["ma"]
        assert any(f'"absorbed_uuid": "{b}"' in x for x in audit_after), (
            "B's audit entry was wrongly deleted because A appeared inside it"
        )
        assert not any(f'"absorbed_uuid": "{a}"' in x for x in audit_after), (
            "A's own audit entry should have been removed"
        )
        assert b in live_repo.execute(
            "MATCH (s:Entity {uuid:$s}) RETURN coalesce(s.merged_from, []) AS mf", params={"s": s}
        )[0]["mf"], "B must remain in the survivor's lineage"
    finally:
        live_repo.execute("MATCH (n) WHERE n.test_tag=$t DETACH DELETE n", params={"t": tag})
