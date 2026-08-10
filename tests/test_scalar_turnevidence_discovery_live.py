"""Live proof for the G14 bridge, slice 2 (plan menhir-observation-nodes-and-view-authority-recall,
G14/F2): scalar-consolidation discovery reads :TurnEvidence when Turn evidence exists, so the
typed-scalar path finds user input in a Turn-capturing production box (it previously matched
:Episodic 'user:' ONLY and saw nothing). Discovery yields the `turn_id` as the row `uuid`, which the
slice-1 grounding Cypher resolves as a :TurnEvidence anchor -- so the composed path grounds assertions
to the declarant foundation.

Online (:7688) because it exercises the real ScalarConsolidationWatermark cursor + the global
evidence_exists() switch a FakeNeo4j cannot evaluate (G7).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

PV = "v1"


def _seed_turn(raw, *, ns, text, session="s", occurred_at=None):
    return TurnEvidenceRepository(raw).record_turn_evidence(
        text=text, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id=session,
        occurred_at=occurred_at,
        prompt_id=f"p-{uuidlib.uuid4().hex[:8]}")["turn_id"]


@pytest.mark.online
def test_switch_prefers_turnevidence_and_yields_turn_id(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I own 20 rare coins")

    # evidence_exists() is True -> scalar discovery reads TurnEvidence.
    assert ns in adapter.list_scalar_dirty_namespaces(perceiver_version=PV)
    rows = adapter.load_next_scalar_batch(ns, perceiver_version=PV)
    assert len(rows) == 1
    row = rows[0]
    assert row["uuid"] == tid                        # the grounding anchor is the turn_id (slice 1)
    assert row["content"] == "I own 20 rare coins"   # raw prompt text, no 'user:' prefix
    # Live hooks omit occurred_at, so valid_at falls back to recorded_at and the assertion folds on the
    # FIRST pass; null would fall to learned_fallback > as_of and be excluded as "future" -> no View.
    assert row["valid_at"] and row["valid_at"] == row["cursor_at"]
    assert row["cursor_at"]                           # recorded_at is the monotonic work-discovery key


@pytest.mark.online
def test_replay_source_time_drives_validity_but_not_cursor(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(
        test_neo4j_repo,
        ns=ns,
        text="I own 25 rare coins",
        occurred_at="2023-11-30T20:25:00Z",
    )

    row = adapter.load_next_scalar_batch(ns, perceiver_version=PV)[0]

    assert row["uuid"] == tid
    assert row["valid_at"].startswith("2023-11-30T20:25:00")
    assert row["cursor_at"] > row["valid_at"]
    assert row["valid_at"] != row["cursor_at"]


@pytest.mark.online
def test_falls_back_to_episodic_when_no_turn_evidence(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    # ONLY a legacy `user:`-prefixed Episodic -- no TurnEvidence anywhere.
    test_neo4j_repo.execute(
        "CREATE (e:Episodic {uuid:$u, group_id:$ns, content:'user: I own 20 rare coins', "
        "created_at: datetime()})",
        {"u": f"ep-{uuidlib.uuid4().hex[:8]}", "ns": ns})

    assert ns in adapter.list_scalar_dirty_namespaces(perceiver_version=PV)
    rows = adapter.load_next_scalar_batch(ns, perceiver_version=PV)
    assert len(rows) == 1
    assert rows[0]["content"] == "user: I own 20 rare coins"   # Episodic path (prefix intact)


@pytest.mark.online
def test_cursor_advances_and_is_resumable(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    _seed_turn(test_neo4j_repo, ns=ns, text="I own 20 rare coins")

    rows = adapter.load_next_scalar_batch(ns, perceiver_version=PV)
    assert len(rows) == 1
    cur = rows[0]
    adapter.advance_scalar_cursor(
        ns, cursor_at=str(cur["cursor_at"]), cursor_uuid=str(cur["uuid"]),
        perceiver_version=PV, at="2026-07-01T00:00:00+00:00")

    # cursor consumed the only turn -> namespace no longer dirty, next page empty.
    assert ns not in adapter.list_scalar_dirty_namespaces(perceiver_version=PV)
    assert adapter.load_next_scalar_batch(ns, perceiver_version=PV) == []

    # a NEWER turn re-dirties the namespace and is the only row returned (resume past the cursor).
    tid2 = _seed_turn(test_neo4j_repo, ns=ns, text="I own 37 rare coins")
    assert ns in adapter.list_scalar_dirty_namespaces(perceiver_version=PV)
    rows2 = adapter.load_next_scalar_batch(ns, perceiver_version=PV)
    assert [r["uuid"] for r in rows2] == [tid2]

    # a perceiver_version bump resets the cursor -> both turns re-selected.
    assert len(adapter.load_next_scalar_batch(ns, perceiver_version="v2")) == 2
