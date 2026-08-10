"""Live proof for Phase 2 (decision 7.B/10.B, gotchas G1/G3): the evidence log is APPEND-ONLY and
idempotent ONLY by source_key -- observations are NEVER collapsed by value.

Plan menhir-observation-nodes-and-view-authority-recall, section 2/G1/G3: the bug that started this work
was a node going stale because same-valued states were conflated. The typed log makes value-collapse
structurally impossible -- source_key = build_source_key(episode_uuid, span_start, span_end, ordinal)
includes the EPISODE, so two distinct sources at the same value are two distinct observation nodes, and
the fold treats several same-valued absolutes as AGREEMENT (never AMBIGUOUS_ANCHOR). These execute the
real persist + fold against the TEST instance (:7688); a FakeNeo4j cannot evaluate the durable identity
keying or the multi-row fold (G7), so they are online. NOTE: Phase 2 surfacing of these nodes FOR RECALL
is Phase 4a (the observation lane); this proves the LOG shape the lane will rest on.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository


@pytest.fixture
def live_repo(test_neo4j_repo):
    MemoryGraphAdapter(neo4j=test_neo4j_repo).activate_scalar_state()
    return TypedAssertionRepository(test_neo4j_repo)


def _entity_and_episode(repo, *, entity, episode):
    repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": entity})
    repo.execute("MERGE (e:Episodic {uuid:$u})", {"u": episode})


def _obs(*, entity, episode, operation, value, stated_span, when):
    return TypedAssertion(
        subject_uuid=entity, subject_display="user", attribute="owned", scope="",
        value_kind="count", unit="", operation=operation, value=value,
        stated_span=stated_span, span_start=0, span_end=len(stated_span),
        episode_uuid=episode, valid_at=when, learned_at=when,
        evidence_tier="agent", perceiver_version="v1",
    )


@pytest.mark.online
def test_full_trajectory_keeps_distinct_nodes_and_folds_to_computed_head(live_repo, test_neo4j_repo):
    # 20 (absolute) -> 37 (absolute) -> gave 17 away (delta -17). Three DURABLE observation nodes; the
    # fold anchors on the latest absolute (37) and applies the -17 -> head 20. The earlier absolute (20)
    # stays a durable node but is NOT a live fold contributor (an excluded prior anchor, not collapsed).
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    e0, e1, e2 = (f"ep-{uuidlib.uuid4().hex[:8]}" for _ in range(3))
    for ep in (e0, e1, e2):
        _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)

    live_repo.record_assertion(_obs(entity=ent, episode=e0, operation="absolute", value=20,
                                    stated_span="I own 20 rare coins", when="2026-07-01T00:00:00+00:00"))
    live_repo.record_assertion(_obs(entity=ent, episode=e1, operation="absolute", value=37,
                                    stated_span="now I have 37 rare coins", when="2026-07-02T00:00:00+00:00"))
    live_repo.record_assertion(_obs(entity=ent, episode=e2, operation="delta", value=-17,
                                    stated_span="I gave 17 rare coins away", when="2026-07-03T00:00:00+00:00"))

    rows = live_repo.materializable_assertions_for_entity(ent)
    assert len(rows) == 3, rows                                  # no value-collapse: 3 distinct nodes
    assert sorted(r["operation"] for r in rows) == ["absolute", "absolute", "delta"]
    assert any(r["operation"] == "delta" for r in rows)          # the delta(-17) observation exists

    folded = fold_assertions(rows)
    assert len(folded.states) == 1 and not folded.abstentions, folded
    s = folded.states[0]
    assert s.value == 20, s                                      # 37 + (-17), the register head
    assert s.anchor_value == 37 and s.delta_total == -17.0, s
    assert len(s.contributor_ids) == 2, s                        # anchor(37) + delta(-17); old 20 excluded


@pytest.mark.online
def test_two_distinct_same_value_episodes_are_two_nodes_not_ambiguous(live_repo, test_neo4j_repo):
    # G3: two DISTINCT episodes that both say "37" are two legitimate observations (source_key includes
    # the episode), and the fold treats same value at the same time as AGREEMENT, never AMBIGUOUS_ANCHOR.
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    e0, e1 = (f"ep-{uuidlib.uuid4().hex[:8]}" for _ in range(2))
    for ep in (e0, e1):
        _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)

    when = "2026-07-02T00:00:00+00:00"
    live_repo.record_assertion(_obs(entity=ent, episode=e0, operation="absolute", value=37,
                                    stated_span="I have 37 rare coins", when=when))
    live_repo.record_assertion(_obs(entity=ent, episode=e1, operation="absolute", value=37,
                                    stated_span="I own 37 rare coins", when=when))

    rows = live_repo.materializable_assertions_for_entity(ent)
    assert len(rows) == 2, rows                                  # two distinct observation nodes
    folded = fold_assertions(rows)
    assert not folded.abstentions, folded                        # same value => agreement, not ambiguous
    assert len(folded.states) == 1 and folded.states[0].value == 37, folded
