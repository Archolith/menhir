"""Live proof for Phase 1B (decision 7.F/10.F): the durable event/delta log lives in the ONE typed
assertion log -- an absolute plus signed deltas persist as distinct source-keyed :TypedAssertion rows
and fold to the computed head, each delta retaining narratable context (G8).

Plan menhir-observation-nodes-and-view-authority-recall, section 10.A/10.F: rather than a separate
durable :Event node, atomic changes are `operation='delta'` rows in the SAME log as absolutes, so
"I own 20, sold 5, dad gave me 15" folds to 30 in one place. This test drives the REAL persist path
(`record_assertion`, MERGE on assertion_key) and the REAL fold against the TEST instance (:7688) -- the
autouse `force_all_tests_onto_test_neo4j` fixture guarantees the target is never prod. A FakeNeo4j cannot
evaluate the durable MERGE/idempotency or the multi-row fold (G7), so it must be online.
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
    # bound provenance so the assertion materializes (an unbound row never folds).
    repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": entity})
    repo.execute("MERGE (e:Episodic {uuid:$u})", {"u": episode})


def _obs(*, entity, episode, operation, value, stated_span, when, span_start=0, span_end=None):
    # one durable observation over the `owned` count slot. Distinct episode + span => distinct
    # source_key => a distinct observation node (never value-collapsed).
    return TypedAssertion(
        subject_uuid=entity, subject_display="user", attribute="owned", scope="",
        value_kind="count", unit="", operation=operation, value=value,
        stated_span=stated_span, span_start=span_start,
        span_end=(span_end if span_end is not None else len(stated_span)),
        episode_uuid=episode, valid_at=when, learned_at=when,
        evidence_tier="agent", perceiver_version="v1",
    )


@pytest.mark.online
def test_absolute_plus_deltas_fold_to_head_with_context(live_repo, test_neo4j_repo):
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    e0, e1, e2 = (f"ep-{uuidlib.uuid4().hex[:8]}" for _ in range(3))
    for ep in (e0, e1, e2):
        _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)

    # I own 20 (absolute) -> sold 5 (delta -5) -> dad gave me 15 (delta +15). Head = 20 - 5 + 15 = 30.
    live_repo.record_assertion(_obs(entity=ent, episode=e0, operation="absolute", value=20,
                                    stated_span="I own 20 rare coins",
                                    when="2026-07-01T00:00:00+00:00"))
    live_repo.record_assertion(_obs(entity=ent, episode=e1, operation="delta", value=-5,
                                    stated_span="I sold 5 rare coins",
                                    when="2026-07-02T00:00:00+00:00"))
    live_repo.record_assertion(_obs(entity=ent, episode=e2, operation="delta", value=15,
                                    stated_span="my dad gave me 15 rare coins",
                                    when="2026-07-03T00:00:00+00:00"))

    rows = live_repo.materializable_assertions_for_entity(ent)
    # THREE durable observations, not value-collapsed; one absolute + two deltas.
    assert len(rows) == 3, rows
    assert sorted(r["operation"] for r in rows) == ["absolute", "delta", "delta"]
    # each observation retains its narratable context (G8: reason/counterparty in the stated_span).
    spans = {r["stated_span"] for r in rows}
    assert spans == {"I own 20 rare coins", "I sold 5 rare coins", "my dad gave me 15 rare coins"}

    folded = fold_assertions(rows)
    assert len(folded.states) == 1, folded.states
    s = folded.states[0]
    assert s.value == 30, s                      # 20 - 5 + 15
    assert s.anchor_value == 20 and s.delta_total == 10.0, s
    assert len(s.contributor_ids) == 3, s        # anchor + both applied deltas


@pytest.mark.online
def test_reingest_is_idempotent_by_source_key(live_repo, test_neo4j_repo):
    # Re-processing the SAME utterances (identical episode + span => identical source_key) is a no-op
    # MERGE: no new observation nodes, head unchanged. (G1/G3: idempotency is by source_key, never value.)
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    e0, e1 = (f"ep-{uuidlib.uuid4().hex[:8]}" for _ in range(2))
    for ep in (e0, e1):
        _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)

    obs = [
        _obs(entity=ent, episode=e0, operation="absolute", value=20,
             stated_span="I own 20 rare coins", when="2026-07-01T00:00:00+00:00"),
        _obs(entity=ent, episode=e1, operation="delta", value=15,
             stated_span="my dad gave me 15 rare coins", when="2026-07-03T00:00:00+00:00"),
    ]
    for a in obs:
        live_repo.record_assertion(a)
    for a in obs:  # ingest the identical sources a second time
        live_repo.record_assertion(a)

    rows = live_repo.materializable_assertions_for_entity(ent)
    assert len(rows) == 2, rows                   # no duplicate observation nodes
    folded = fold_assertions(rows)
    assert folded.states[0].value == 35, folded   # 20 + 15, stable across re-ingest
