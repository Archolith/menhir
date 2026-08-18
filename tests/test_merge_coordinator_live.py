"""MergeCoordinator against a REAL Neo4j (remediation plan Phase 4, online).

These prove the properties a fake graph cannot:

* the recovery snapshot is durable BEFORE the absorbed node is deleted (the legacy path deleted
  first and wrote a best-effort audit after -- a crash there was unrecoverable);
* a PREPARE failure mutates nothing;
* a crash between mutation and COMMIT is recognized as the exact after-state on replay, not re-run;
* an ineligible or drifted pair never reports COMMITTED;
* the snapshot stored at PREPARE is complete enough to invert the merge (typed values, parallel
  edges, non-Entity peers, relationship properties, and the survivor's pre-merge state).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain import merge_eligibility as me
from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator, MergeDrift, pair_key


@pytest.fixture
def live_repo(test_neo4j_repo):
    """The stood-up TEST instance (conftest.test_neo4j_repo), never the operator's real graph."""
    return test_neo4j_repo


@pytest.fixture
def coord(live_repo, tmp_path):
    return MergeCoordinator(
        graph_adapter=CorrelationRepository(live_repo),
        journal=GraphOperationsJournal(db_path=tmp_path / "saga.db"),
    )


@pytest.fixture
def pair(live_repo):
    """survivor + absorbed + a peer Entity and a peer Episodic, with typed props and parallel edges."""
    tag = f"test-merge-coord-{uuidlib.uuid4()}"
    ids = {"s": f"{tag}-s", "a": f"{tag}-a", "p": f"{tag}-p", "ep": f"{tag}-ep"}
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT', summary:'short'})
        CREATE (a:Entity {uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          when: datetime('2026-07-13T10:30:15.123456789Z'),
                          day: date('2026-07-13'),
                          dur: duration({months:2, days:3}),
                          pt: point({x:1.0, y:2.0}),
                          tags: ['x','y']})
        CREATE (p:Entity {uuid:$p, name:'peer', test_tag:$t})
        CREATE (ep:Episodic {uuid:$ep, name:'episode', test_tag:$t})
        CREATE (a)-[:RELATES_TO {weight:0.9, kind:'first'}]->(p)
        CREATE (a)-[:RELATES_TO {weight:0.3, kind:'parallel'}]->(p)
        CREATE (ep)-[:MENTIONS {conf:0.7}]->(a)
        """,
        params={**ids, "t": tag},
    )
    yield ids
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def _absorbed_exists(live_repo, uuid: str) -> bool:
    return int(
        live_repo.execute(
            "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": uuid}
        )[0]["c"]
    ) > 0


# --------------------------------------------------------------------------- happy path

@pytest.mark.online
def test_merge_commits_and_snapshot_is_durable(coord, live_repo, pair):
    res = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert res["merged"] == 1
    op_id = res["op_id"]

    assert coord.journal.get(op_id)["state"] == "COMMITTED"
    assert not _absorbed_exists(live_repo, pair["a"]), "a committed merge absorbs the node"

    # The snapshot survives the node it describes -- that is the entire point.
    body = coord.load_snapshot(op_id)
    absorbed = ms.decode_node(body["absorbed"])
    survivor = ms.decode_node(body["survivor"])

    assert absorbed["uuid"] == pair["a"]
    assert survivor["uuid"] == pair["s"]
    # Survivor's PRE-merge state is captured (its summary before the merge rewrote it).
    assert survivor["properties"]["summary"] == "short"

    # Typed values decode back to driver types, not strings.
    from neo4j.time import Date, DateTime, Duration
    from neo4j.spatial import CartesianPoint

    props = absorbed["properties"]
    assert isinstance(props["when"], DateTime)
    assert isinstance(props["day"], Date)
    assert isinstance(props["dur"], Duration)
    assert isinstance(props["pt"], CartesianPoint)
    assert props["tags"] == ["x", "y"]

    # Every incident relationship instance, including the parallel pair and the non-Entity peer.
    rels = absorbed["relationships"]
    relates = [r for r in rels if r["type"] == "RELATES_TO"]
    mentions = [r for r in rels if r["type"] == "MENTIONS"]
    assert len(relates) == 2, "parallel edges must be preserved as distinct instances"
    assert {r["properties"]["kind"] for r in relates} == {"first", "parallel"}
    assert len(mentions) == 1 and mentions[0]["properties"]["conf"] == 0.7
    assert mentions[0]["peer_labels"] == ["Episodic"], "non-Entity peers must be captured"


# --------------------------------------------------------------------------- fail-closed paths

@pytest.mark.online
def test_prepare_failure_mutates_nothing(coord, live_repo, pair, monkeypatch):
    """If the durable snapshot cannot be committed, the graph MUST be untouched (invariant 3)."""
    def boom(**kwargs):
        raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr(coord.journal, "prepare", boom)

    res = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert res["merged"] == 0
    assert res["reason"] == "PREPARE_FAILED"
    assert _absorbed_exists(live_repo, pair["a"]), "no PREPARED row => no graph mutation"


@pytest.mark.online
def test_ineligible_pair_abstains_before_prepare(coord, live_repo, pair):
    live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) SET n.freshness = 'COMPRESSED'", params={"u": pair["a"]}
    )
    res = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert res["merged"] == 0
    assert res["reason"] == me.NON_ACTIVE_FRESHNESS
    assert _absorbed_exists(live_repo, pair["a"])
    # Nothing was journaled: an ineligible pair never becomes an operation.
    assert coord.journal.list_by_state("PREPARED") == []


@pytest.mark.online
def test_crash_after_mutation_replays_as_committed(coord, live_repo, pair):
    """Crash between the graph mutation and COMMITTED: reconcile must recognize its OWN work."""
    real_commit = coord.journal.mark_committed
    calls = {"n": 0}

    def crash_once(op_id):
        calls["n"] += 1
        raise RuntimeError("crash before COMMITTED")

    coord.journal.mark_committed = crash_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)

    # The graph mutation DID land; the journal row is still PREPARED.
    assert not _absorbed_exists(live_repo, pair["a"])
    prepared = coord.journal.list_by_state("PREPARED")
    assert len(prepared) == 1

    # Reconcile: the survivor carries this op's marker, so replay is a no-op that commits.
    coord.journal.mark_committed = real_commit  # type: ignore[method-assign]
    out = coord._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out == {"replayed": 1, "drifted": 0, "failed": 0}
    assert coord.journal.get(prepared[0]["op_id"])["state"] == "COMMITTED"
    assert coord.journal.list_by_state("PREPARED") == []


@pytest.mark.online
def test_drifted_pair_is_quarantined_not_committed(coord, live_repo, pair, monkeypatch):
    """A PREPARED op whose graph was changed by someone else must NEEDS_REVIEW, never COMMIT."""
    # Force a crash right after PREPARE, before the mutation.
    monkeypatch.setattr(
        coord.graph_adapter, "merge_entity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("neo4j outage")),
    )
    with pytest.raises(RuntimeError):
        coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)

    prepared = coord.journal.list_by_state("PREPARED")
    assert len(prepared) == 1
    op_id = prepared[0]["op_id"]
    assert _absorbed_exists(live_repo, pair["a"]), "mutation failed, so the node must remain"

    # Now a DIFFERENT writer deletes the absorbed node out from under the prepared op.
    live_repo.execute("MATCH (n:Entity {uuid:$u}) DETACH DELETE n", params={"u": pair["a"]})

    monkeypatch.undo()
    out = coord._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out["drifted"] == 1 and out["replayed"] == 0
    assert coord.journal.get(op_id)["state"] == "NEEDS_REVIEW"


@pytest.mark.online
def test_unresolved_operation_fences_the_pair(coord, live_repo, pair, monkeypatch):
    """While a merge is PREPARED/NEEDS_REVIEW, a competing merge of the same pair cannot prepare."""
    monkeypatch.setattr(
        coord.graph_adapter, "merge_entity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("neo4j outage")),
    )
    with pytest.raises(RuntimeError):
        coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    monkeypatch.undo()
    assert len(coord.journal.list_by_state("PREPARED")) == 1

    # A second attempt at the SAME pair -- even with survivor/absorbed swapped -- is fenced.
    res = coord.merge(survivor_uuid=pair["a"], absorbed_uuid=pair["s"], similarity=0.97)
    assert res["merged"] == 0
    assert res["reason"] == "PREPARE_FAILED"
    assert _absorbed_exists(live_repo, pair["a"])


@pytest.mark.online
def test_pair_key_is_order_independent(pair):
    assert pair_key(pair["s"], pair["a"]) == pair_key(pair["a"], pair["s"])


@pytest.mark.online
def test_concurrent_merges_into_same_survivor_both_commit(coord, live_repo, pair):
    """Two merges into the SAME survivor must both COMMIT (regression: false NEEDS_REVIEW).

    The absorbed uuid + the survivor's merged_from lineage already identify one exact absorption,
    and the pair fence stops a competing merge of the SAME pair. Fingerprinting on
    `last_merge_op_id` (a survivor-global 'who touched me last' stamp) was over-strict: a second
    merge into the same survivor overwrote the stamp, so the FIRST op's after-state check failed
    and a successful merge was quarantined.
    """
    tag = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.test_tag AS t", params={"u": pair["s"]}
    )[0]["t"]
    second = f"{tag}-a2"
    live_repo.execute(
        "CREATE (b:Entity {uuid:$u, name:'absorbed 2', test_tag:$t, namespace:'default', "
        "freshness:'ACTIVE', scope:'PERSISTENT'})",
        params={"u": second, "t": tag},
    )

    r1 = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    r2 = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=second, similarity=0.97)

    assert r1["merged"] == 1 and r2["merged"] == 1
    assert coord.journal.get(r1["op_id"])["state"] == "COMMITTED"
    assert coord.journal.get(r2["op_id"])["state"] == "COMMITTED", (
        "the first merge must not be invalidated by a later merge into the same survivor"
    )
    assert coord.journal.list_by_state("NEEDS_REVIEW") == []


@pytest.mark.online
def test_replay_after_another_merge_touched_survivor_is_not_false_drift(coord, live_repo, pair):
    """A crashed op whose merge SUCCEEDED must still replay as COMMITTED; and while it is
    unresolved the survivor is fenced (invariant 14), so no competing merge can pile onto it.

    Sequence: op A absorbs ``a`` and crashes before COMMITTED, leaving a PREPARED row that fences
    the survivor -> a second merge into the SAME survivor is refused (PREPARE_FAILED) -> reconcile A:
    its absorption is intact and recorded in the survivor's lineage, so A COMMITs (never a false
    NEEDS_REVIEW) and releases the fence -> the second merge now proceeds, overwriting the
    survivor-global last_merge_op_id, and A stays COMMITTED -- the pair-specific fingerprint does not
    false-drift on a later, unrelated absorption into the same survivor.
    """
    tag = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.test_tag AS t", params={"u": pair["s"]}
    )[0]["t"]
    second = f"{tag}-a2"
    live_repo.execute(
        "CREATE (b:Entity {uuid:$u, name:'absorbed 2', test_tag:$t, namespace:'default', "
        "freshness:'ACTIVE', scope:'PERSISTENT'})",
        params={"u": second, "t": tag},
    )

    real_commit = coord.journal.mark_committed
    coord.journal.mark_committed = lambda op_id: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    coord.journal.mark_committed = real_commit  # type: ignore[method-assign]

    op_a = coord.journal.list_by_state("PREPARED")[0]["op_id"]
    assert not _absorbed_exists(live_repo, pair["a"]), "op A's mutation landed"

    # While op A is unresolved it fences the survivor (invariant 14): a competing merge into the
    # SAME survivor is refused, never allowed to mutate a node with an in-flight operation.
    fenced = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=second, similarity=0.97)
    assert fenced["merged"] == 0
    assert fenced["reason"] == "PREPARE_FAILED"

    # Reconcile A: it succeeded, so it COMMITs (never a false NEEDS_REVIEW) and releases the fence.
    out = coord._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out["drifted"] == 0, "op A succeeded; reconcile must commit it, not quarantine it"
    assert coord.journal.get(op_a)["state"] == "COMMITTED"

    # Fence released: the second merge now proceeds, overwriting the survivor-global
    # last_merge_op_id. A must stay COMMITTED -- the pair-specific fingerprint does not false-drift
    # on a later, unrelated absorption into the same survivor.
    r2 = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=second, similarity=0.97)
    assert r2["merged"] == 1
    assert coord.journal.get(op_a)["state"] == "COMMITTED"


@pytest.mark.online
def test_oversized_snapshot_abstains_and_never_truncates(coord, live_repo, pair, monkeypatch):
    """Invariant 15: a snapshot over the size ceiling ABSTAINS; it is never truncated to fit."""
    from menhir.services.merge_coordinator import MERGE_SNAPSHOT_TOO_LARGE

    monkeypatch.setattr(ms, "MAX_SNAPSHOT_BYTES", 200)  # force the limit

    res = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)

    assert res["merged"] == 0
    assert res["reason"] == MERGE_SNAPSHOT_TOO_LARGE
    assert res["diagnostics"]["snapshot_bytes"] > 200
    # Abstained BEFORE prepare: no journal row, no mutation.
    assert coord.journal.list_by_state("PREPARED") == []
    assert _absorbed_exists(live_repo, pair["a"])


@pytest.mark.online
def test_mutation_gate_abstention_fails_terminally_and_releases_the_pair(
    coord, live_repo, pair, monkeypatch
):
    """A benign mutation-gate abstention is terminal FAILED, NOT a quarantine.

    The graph is untouched, so there is nothing to adjudicate and nothing to replay. Marking it
    NEEDS_REVIEW would fence the pair forever over a node that merely became ineligible; leaving it
    PREPARED would make reconciliation retry a pair that will never be eligible again. FAILED
    releases the fence, so a later legitimate merge of the same pair can still proceed.
    """
    # Force the repository to abstain at its own mutation gate without touching the graph.
    real_merge = coord.graph_adapter.merge_entity
    monkeypatch.setattr(
        coord.graph_adapter, "merge_entity",
        lambda *a, **k: {"merged": 0, "reason": "ELIGIBILITY_CHANGED_AT_MUTATION"},
    )

    res = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert res["merged"] == 0
    assert res["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"

    op_id = res["op_id"]
    assert coord.journal.get(op_id)["state"] == "FAILED"
    assert coord.journal.list_by_state("NEEDS_REVIEW") == []
    assert _absorbed_exists(live_repo, pair["a"]), "an abstention must not mutate"

    # The fence is RELEASED: the same pair can be merged again once it is genuinely eligible.
    monkeypatch.setattr(coord.graph_adapter, "merge_entity", real_merge)
    again = coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    assert again["merged"] == 1, "FAILED must not fence the pair"
    assert coord.journal.get(again["op_id"])["state"] == "COMMITTED"


# --------------------------------------------------------------------------- chained provenance

@pytest.fixture
def chain(live_repo):
    """A legacy project-scan structure row plus two agent-written duplicates to absorb into it."""
    tag = f"test-merge-chain-{uuidlib.uuid4()}"
    ids = {"s": f"{tag}-s", "a": f"{tag}-a", "b": f"{tag}-b", "c": f"{tag}-c"}
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'legacy structure row', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          content:'Directory: src/example',
                          // No `sources` list: written before the property existed.
                          source:'project-scan', source_confidence:0.9})
        CREATE (a:Entity {uuid:$a, name:'agent duplicate', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          source:'claude-code', source_confidence:0.7})
        CREATE (b:Entity {uuid:$b, name:'another duplicate', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          source:'codex', source_confidence:0.5})
        CREATE (c:Entity {uuid:$c, name:'third duplicate', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          source:'opencode', source_confidence:0.5})
        """,
        params={**ids, "t": tag},
    )
    yield ids
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


@pytest.mark.online
def test_absorbing_an_already_merged_node_keeps_all_its_contributors(coord, live_repo, chain):
    """Defect: the absorbed node's `sources` was never read, only its `source` -- which by
    construction holds just the LOWEST-tier contributor. So absorbing a node that had itself merged
    dropped every other writer it had accumulated, permanently and silently.
    """
    # Build up `a` first: it absorbs `b` and `c`, so it carries three contributors of its own.
    assert coord.merge(survivor_uuid=chain["a"], absorbed_uuid=chain["b"], similarity=0.97)["merged"]
    assert coord.merge(survivor_uuid=chain["a"], absorbed_uuid=chain["c"], similarity=0.97)["merged"]
    intermediate = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.sources AS sources", params={"u": chain["a"]}
    )[0]["sources"]
    assert intermediate == ["claude-code", "codex", "opencode"]

    # Now absorb that merged node into the structure row.
    assert coord.merge(survivor_uuid=chain["s"], absorbed_uuid=chain["a"], similarity=0.97)["merged"]

    row = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.sources AS sources, n.source AS source, "
        "n.source_confidence AS conf, n.corroboration AS corroboration",
        params={"u": chain["s"]},
    )[0]
    assert row["sources"] == ["project-scan", "claude-code", "codex", "opencode"], (
        "the absorbed node's non-primary contributors were dropped"
    )
    assert row["corroboration"] == 4
    assert row["source"] == "codex"          # first-listed of the lowest-tier contributors
    assert row["conf"] == 0.5                # and no merge in the chain ever raised it


@pytest.mark.online
def test_a_merged_structure_row_is_still_recognised_as_structure(coord, live_repo, chain):
    """A legacy structure row's `source` stops saying 'project-scan' the moment it absorbs a
    lower-tier duplicate. The recognition predicate reads the preserved `sources` list for exactly
    that reason -- without it the row surfaces in recall as though it were a memory.
    """
    from menhir.domain.structural_memory import legacy_structural_memory_cypher

    assert coord.merge(survivor_uuid=chain["s"], absorbed_uuid=chain["a"], similarity=0.97)["merged"]

    row = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.source AS source, n.sources AS sources",
        params={"u": chain["s"]},
    )[0]
    assert row["source"] == "claude-code", "precondition: `source` no longer names project-scan"
    assert "project-scan" in row["sources"]

    recognised = live_repo.execute(
        f"MATCH (n:Entity {{uuid:$u}}) RETURN ({legacy_structural_memory_cypher('n')}) AS structural",
        params={"u": chain["s"]},
    )[0]["structural"]
    assert recognised is True, "a merged legacy structure row must still be recognised as structure"
