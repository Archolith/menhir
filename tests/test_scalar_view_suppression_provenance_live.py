"""Live proof that Step-7 View suppression joins the RECALL-CANDIDATE uuid space.

Regression for the Step 7c uuid-space defect (plan menhir-scalar-view-enablement-sequence.md).
`_plan_view_authority_suppression` passes RECALL candidate uuids -- `:Entity` semantic-memory nodes
recall surfaced -- into `suppressible_provenance`. The original query matched them against
`TypedAssertion.episode_uuid` (an `:Episodic` uuid): a DISJOINT space, so `... IN $uuids` returned
zero rows on every current-state query and the six-gate authority decision never ran. The fix bridges
the candidate `:Entity` to the typed-assertion log through the shared source episode
`(:Episodic)-[:MENTIONS]->(:Entity)` and returns the ENTITY uuid, so suppression stays in recall's
candidate space.

The offline suite cannot catch this: its FakeNeo4j routes cypher by substring and never interprets
the join, so it reported the wrong-space query as working. These tests run the REAL query against the
stood-up TEST instance (:7688) and assert the durable behavior a fake cannot -- deterministically
seeded (no LLM), so the graph is fixed and the outcome is exact.

Runs only with `--run-online`; the autouse `force_all_tests_onto_test_neo4j` fixture guarantees the
target is never prod.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.scalar_view_authority import QueryIntent
from menhir.domain.scalar_view_suppression import plan_view_suppression
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository

OLD = "2026-07-01T00:00:00Z"
NOW = "2026-07-15T00:00:00Z"

_SEED = """
CREATE (self:Entity {uuid:$subj, group_id:$ns, name:'user'})
CREATE (staleEp:Episodic {uuid:$stale_ep, group_id:$ns, source:'chat_message'})
CREATE (currEp:Episodic  {uuid:$curr_ep,  group_id:$ns, source:'chat_message'})
// The recall candidates: consolidated :Entity value memories (a DISJOINT uuid space from episodes).
CREATE (staleMem:Entity {uuid:$stale_mem, group_id:$ns, name:'user owns 20 rare coins'})
CREATE (currMem:Entity  {uuid:$curr_mem,  group_id:$ns, name:'user owns 37 rare coins'})
CREATE (staleEp)-[:MENTIONS]->(staleMem)
CREATE (currEp)-[:MENTIONS]->(currMem)
// Two distinct grounded absolutes for the same slot (no SUPERSEDES edge: cross-episode replacement).
CREATE (staleA:TypedAssertion {namespace:$ns, binding_pending:false, operation:'absolute',
    episode_uuid:$stale_ep, subject_uuid:$subj, attribute:'owned', scope:'',
    value_kind:'count', unit:'', value_json:'20', evidence_tier:'user',
    valid_at: datetime($old), superseded:false})
CREATE (currA:TypedAssertion {namespace:$ns, binding_pending:false, operation:'absolute',
    episode_uuid:$curr_ep, subject_uuid:$subj, attribute:'owned', scope:'',
    value_kind:'count', unit:'', value_json:'37', evidence_tier:'user',
    valid_at: datetime($now), superseded:false})
// The sole current scalar_state View for the slot (authority value comes from the LATEST assertion).
CREATE (v:Entity {view_kind:'scalar_state', view_subject_uuid:$subj, group_id:$ns,
    ss_attribute:'owned', ss_scope:'', ss_kind:'count', ss_unit:'', view_current:true})
"""


@pytest.fixture
def seeded(test_neo4j_repo):
    ns = f"va-{uuidlib.uuid4().hex[:8]}"
    ids = {
        "ns": ns,
        "subj": f"self-{uuidlib.uuid4().hex[:8]}",
        "stale_ep": f"ep-stale-{uuidlib.uuid4().hex[:8]}",
        "curr_ep": f"ep-curr-{uuidlib.uuid4().hex[:8]}",
        "stale_mem": f"mem-stale-{uuidlib.uuid4().hex[:8]}",
        "curr_mem": f"mem-curr-{uuidlib.uuid4().hex[:8]}",
    }
    test_neo4j_repo.execute(_SEED, {**ids, "old": OLD, "now": NOW})
    return test_neo4j_repo, ids


@pytest.mark.online
def test_candidate_entity_uuid_bridges_to_slot_via_source_episode(seeded):
    # THE fix: passing the recall candidate ENTITY uuid (not an episode uuid) surfaces exactly one
    # provenance row, keyed back in ENTITY space, with the authority value from the LATEST assertion.
    repo, ids = seeded
    rows = TypedAssertionRepository(repo).suppressible_provenance(
        namespace=ids["ns"], candidate_uuids=[ids["stale_mem"], ids["curr_mem"]]
    )
    by_cand = {r["candidate_uuid"]: r for r in rows}
    # stale candidate: its source episode grounds the stale absolute -> a suppressible row.
    assert ids["stale_mem"] in by_cand, rows
    r = by_cand[ids["stale_mem"]]
    assert r["candidate_uuid"] == ids["stale_mem"]          # entity space, NOT episode
    assert str(r["view_value"]) == "37" and str(r["stale_value"]) == "20"
    assert r["evidence_tier"] == "user"
    assert r["is_sole_current"] is True and r["view_current"] is True
    assert r["attribute"] == "owned" and r["value_kind"] == "count"


@pytest.mark.online
def test_episode_uuid_space_no_longer_matches(seeded):
    # Discriminator vs the OLD defect: the pre-fix query matched episode uuids. Passing an episode
    # uuid as a "candidate" must now surface NOTHING (candidates are entities, joined via MENTIONS).
    repo, ids = seeded
    rows = TypedAssertionRepository(repo).suppressible_provenance(
        namespace=ids["ns"], candidate_uuids=[ids["stale_ep"], ids["curr_ep"]]
    )
    assert rows == [], rows


@pytest.mark.online
def test_end_to_end_current_state_query_suppresses_stale_candidate(seeded):
    # Full path repo -> planner: a current-state query suppresses the STALE candidate entity and
    # leaves the current one (same-value / not-strictly-older -> gate drops it), proving the six-gate
    # decision now actually RUNS (it never fired under the disjoint-uuid defect).
    repo, ids = seeded
    rows = TypedAssertionRepository(repo).suppressible_provenance(
        namespace=ids["ns"], candidate_uuids=[ids["stale_mem"], ids["curr_mem"]]
    )
    plan = plan_view_suppression(
        "How many rare coins do I own right now?", rows, {ids["stale_mem"], ids["curr_mem"]}
    )
    assert plan.intent is QueryIntent.CURRENT_STATE
    assert plan.suppressed_uuids == frozenset({ids["stale_mem"]}), plan.decisions


@pytest.mark.online
def test_historical_query_suppresses_nothing(seeded):
    # The authority gate stays advisory for a non-current query even with the same provenance rows.
    repo, ids = seeded
    rows = TypedAssertionRepository(repo).suppressible_provenance(
        namespace=ids["ns"], candidate_uuids=[ids["stale_mem"], ids["curr_mem"]]
    )
    plan = plan_view_suppression(
        "How many rare coins did I used to own?", rows, {ids["stale_mem"], ids["curr_mem"]}
    )
    assert plan.intent is QueryIntent.PREVIOUS_VALUE and not plan.any_suppressed
