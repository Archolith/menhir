"""MetricWriteCoordinator against a REAL Neo4j (Plan 2 step 3, online).

The offline saga tests use a fake graph. These prove the parts a fake cannot: that a frozen uuid
actually satisfies the metric_uuid_unique constraint on replay, that a value change supersedes
under the :Metric label, and that a crash-replay against the real graph converges to ONE node
instead of forking. Runs against the stood-up test instance (:7688), never prod.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.services.metric_write_coordinator import MetricDrift, MetricWriteCoordinator


@pytest.fixture
def live_coord(test_neo4j_repo, tmp_path):
    db = tmp_path / "saga.db"
    coord = MetricWriteCoordinator(
        graph_adapter=MemoryGraphAdapter(neo4j=test_neo4j_repo),
        journal=GraphOperationsJournal(db_path=db),
        receipts=MetricReceiptStore(db_path=db),
        telemetry_db_path=db,
    )
    coord.journal._ensure_ready()
    coord.receipts._ensure_ready()
    return coord


@pytest.mark.online
def test_saga_writes_metric_with_committed_receipt(live_coord, test_neo4j_repo):
    ns = f"mc-{uuidlib.uuid4().hex[:8]}"
    res = live_coord.record_run_tally(
        subject="perception", counter="abstained", value=3.0, namespace=ns
    )
    op = res["op_id"]

    assert live_coord.journal.get(op)["state"] == "COMMITTED"
    assert live_coord.receipts.get(op)["receipt_kind"] == "RUN_TALLY"

    rows = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) RETURN n.uuid AS uuid, n.view_value AS v, "
        "n.metric_last_receipt_op_id AS op, n.type AS type",
        {"k": f"{ns}::perception::abstained"},
    )
    assert len(rows) == 1
    assert float(rows[0]["v"]) == 3.0
    assert rows[0]["op"] == op and rows[0]["type"] == "METRIC"


@pytest.mark.online
def test_value_change_supersedes_within_metric_label(live_coord, test_neo4j_repo):
    ns = f"mc-{uuidlib.uuid4().hex[:8]}"
    live_coord.record_telemetry_fold(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich"}, cutoff_id=10, delta_row_ids=[1, 2], delta_count=2,
        namespace=ns,
    )
    live_coord.record_telemetry_fold(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich"}, cutoff_id=20, delta_row_ids=[11], delta_count=1,
        namespace=ns,
    )

    rows = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) "
        "RETURN n.view_value AS v, coalesce(n.view_current, true) AS cur ORDER BY n.view_value",
        {"k": f"{ns}::enrichment::timeout_failed"},
    )
    # Two versions: superseded 2.0 and current 3.0 (accumulated, not recounted).
    assert [float(r["v"]) for r in rows] == [2.0, 3.0]
    assert [r["cur"] for r in rows] == [False, True]

    sup = test_neo4j_repo.execute(
        "MATCH (:Metric {view_key:$k})-[r:SUPERSEDES]->(:Metric) RETURN count(r) AS c",
        {"k": f"{ns}::enrichment::timeout_failed"},
    )
    assert sup[0]["c"] == 1


@pytest.mark.online
def test_repeated_writes_never_false_drift(live_coord, test_neo4j_repo):
    """new -> UNCHANGED -> changed -> UNCHANGED must all COMMIT (regression, 2026-07-13).

    An unchanged write creates no new version, but it DOES create a new receipt, so the node must
    be repointed at it (plan A4). While _write_version's unchanged branch only touched
    last_accessed, the node kept the FIRST op's receipt id -- so the saga's after-state fingerprint
    (which expects THIS op's id) mismatched, raising a FALSE MetricDrift. That parked the op in
    NEEDS_REVIEW, and the write fence then blocked EVERY future write to that key. Same-value
    rewrites are the normal case for a telemetry fold whose count did not move, so this would have
    bricked the metric pipeline per key on first repeat. Offline fakes hid it; only the real graph
    showed it.
    """
    ns = f"mc-{uuidlib.uuid4().hex[:8]}"

    ops = [
        live_coord.record_run_tally(subject="s", counter="c", value=5.0, namespace=ns),  # create
        live_coord.record_run_tally(subject="s", counter="c", value=5.0, namespace=ns),  # unchanged
        live_coord.record_run_tally(subject="s", counter="c", value=9.0, namespace=ns),  # supersede
        live_coord.record_run_tally(subject="s", counter="c", value=9.0, namespace=ns),  # unchanged
    ]
    for res in ops:
        assert live_coord.journal.get(res["op_id"])["state"] == "COMMITTED"
    assert live_coord.journal.list_by_state("NEEDS_REVIEW") == []

    rows = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) RETURN n.view_value AS v, "
        "coalesce(n.view_current, true) AS cur, n.metric_last_receipt_op_id AS op "
        "ORDER BY n.view_value",
        {"k": f"{ns}::s::c"},
    )
    # Exactly two versions: superseded 5.0 and current 9.0 (an unchanged write adds no version).
    assert [(float(r["v"]), r["cur"]) for r in rows] == [(5.0, False), (9.0, True)]
    # The current node points at the LATEST receipt, not the first one that created it.
    current_op = next(r["op"] for r in rows if r["cur"])
    assert current_op == ops[-1]["op_id"]


@pytest.mark.online
def test_crash_replay_against_real_graph_does_not_fork(live_coord, test_neo4j_repo, monkeypatch):
    """A crash before the graph write, then reconcile: exactly ONE :Metric node, frozen uuid."""
    ns = f"mc-{uuidlib.uuid4().hex[:8]}"

    real_record = live_coord.graph_adapter.record_metric
    def boom(**kwargs):
        raise RuntimeError("simulated neo4j outage")
    monkeypatch.setattr(live_coord.graph_adapter, "record_metric", boom)

    with pytest.raises(RuntimeError):
        live_coord.record_run_tally(subject="enrichment", counter="stalled", value=4.0, namespace=ns)

    prepared = live_coord.journal.list_by_state("PREPARED")
    assert len(prepared) == 1
    frozen_uuid = prepared[0]["target_uuid"]
    assert test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) RETURN count(n) AS c", {"k": f"{ns}::enrichment::stalled"}
    )[0]["c"] == 0

    monkeypatch.setattr(live_coord.graph_adapter, "record_metric", real_record)
    out = live_coord._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out["replayed"] == 1 and out["drifted"] == 0

    rows = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) RETURN n.uuid AS uuid, n.view_value AS v",
        {"k": f"{ns}::enrichment::stalled"},
    )
    assert len(rows) == 1, "replay forked a competing node"
    assert rows[0]["uuid"] == frozen_uuid
    assert float(rows[0]["v"]) == 4.0
    assert live_coord.journal.list_by_state("PREPARED") == []


@pytest.mark.online
def test_changed_write_never_creates_a_second_current(live_coord, test_neo4j_repo):
    """A changed Metric write leaves EXACTLY one current version -- verified on the real graph.

    Plan E Phase 2: create-and-supersede is now one atomic statement, so a value change can never
    leave the prior version current alongside the new one. Assert directly that after a superseding
    write there is a single view_current node and exactly one SUPERSEDES edge.
    """
    ns = f"mc-{uuidlib.uuid4().hex[:8]}"
    key = f"{ns}::s::c"
    live_coord.record_run_tally(subject="s", counter="c", value=1.0, namespace=ns)
    r2 = live_coord.record_run_tally(subject="s", counter="c", value=2.0, namespace=ns)
    assert live_coord.journal.get(r2["op_id"])["state"] == "COMMITTED"

    currents = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) WHERE coalesce(n.view_current, true) "
        "RETURN count(n) AS c",
        {"k": key},
    )
    assert currents[0]["c"] == 1, "changed write left more than one current version"


@pytest.mark.online
def test_preexisting_duplicate_current_routes_to_needs_review(live_coord, test_neo4j_repo):
    """A pre-existing two-current graph must NOT be marked COMMITTED (plan E Phase 2 exit).

    The atomic mutation stops a crash from PRODUCING duplicates; the cardinality-aware fingerprint
    is the second gate: if corruption (or a competing writer) has already left two current versions,
    the saga's after-state verification sees current_count=2 != expected 1 and quarantines the op in
    NEEDS_REVIEW rather than papering over it.
    """
    ns = f"mc-{uuidlib.uuid4().hex[:8]}"
    key = f"{ns}::s::c"
    live_coord.record_run_tally(subject="s", counter="c", value=1.0, namespace=ns)

    # Inject a SECOND current version for the same key (simulated corruption / competing writer).
    # Use a map projection (orig{.*, uuid: ...}) so the duplicate is materialized with its NEW uuid in
    # one atomic map value: `SET dup = properties(orig)` would transiently copy orig.uuid, and Neo4j 5's
    # metric_uuid_unique constraint is enforced EAGERLY within the statement, so the transient duplicate
    # trips the constraint before the `dup.uuid = $dup_uuid` override can run.
    test_neo4j_repo.execute(
        "MATCH (orig:Metric {view_key:$k}) WITH orig LIMIT 1 "
        "CREATE (dup:Metric) "
        "SET dup = orig{.*, uuid: $dup_uuid, created_at: datetime()}",
        {"k": key, "dup_uuid": uuidlib.uuid4().hex},
    )
    assert test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) WHERE coalesce(n.view_current, true) RETURN count(n) AS c",
        {"k": key},
    )[0]["c"] == 2

    # A changed write supersedes only one of the two currents, so the after-state still holds two.
    # The cardinality-aware verifier must catch that: _apply raises MetricDrift and parks the op in
    # NEEDS_REVIEW rather than marking a two-current graph COMMITTED.
    with pytest.raises(MetricDrift):
        live_coord.record_run_tally(subject="s", counter="c", value=9.0, namespace=ns)

    review = live_coord.journal.list_by_state("NEEDS_REVIEW")
    assert len(review) == 1
    # Only the initial healthy write committed; the drifted changed-write did NOT add a COMMITTED row.
    assert len(live_coord.journal.list_by_state("COMMITTED")) == 1
