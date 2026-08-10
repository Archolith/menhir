"""Correction semantics — LIVE Neo4j (Phase A). Validates the monotone write Cypher parses and behaves.

Proves against a real store (not just query-string text): (1) an ordinary and a correction of the same
slot at equal valid_at fold to the corrected value; (2) an idempotent ordinary re-write NEVER downgrades
a persisted correction (monotone ordinary->correction). Requires the test Neo4j (MENHIR_TEST_NEO4J_URI).
"""

from __future__ import annotations

import pytest

from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository


@pytest.fixture
def live_repo(test_neo4j_repo):
    MemoryGraphAdapter(neo4j=test_neo4j_repo).activate_scalar_state()
    return TypedAssertionRepository(test_neo4j_repo)


def _mk(repo, *, entity, episode):
    repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": entity})
    repo.execute("MERGE (e:Episodic {uuid:$u})", {"u": episode})


def _a(*, entity, episode, span_start, span_end, value, semantics, stated_span):
    return TypedAssertion(
        subject_uuid=entity, subject_display="user", attribute="weight", scope="",
        value_kind="measurement", unit="kg", operation="absolute", value=value,
        stated_span=stated_span, span_start=span_start, span_end=span_end, episode_uuid=episode,
        valid_at="2026-07-20T00:00:00+00:00", learned_at="2026-07-20T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1", absolute_semantics=semantics,
    )


@pytest.mark.online
def test_correction_persists_and_folds_over_ordinary(live_repo, test_neo4j_repo):
    ent, ep = "ent-weight", "ep-weight"
    _mk(test_neo4j_repo, entity=ent, episode=ep)
    # two claims, different spans (different source_keys) -> both persist and compete at the fold.
    live_repo.record_assertion(_a(entity=ent, episode=ep, span_start=0, span_end=13, value=180,
                        semantics="ordinary", stated_span="I weigh 180 kg"))
    live_repo.record_assertion(_a(entity=ent, episode=ep, span_start=20, span_end=44, value=182,
                        semantics="correction", stated_span="Actually I weigh 182 kg"))
    rows = live_repo.materializable_assertions_for_entity(ent)
    assert {r["absolute_semantics"] for r in rows} == {"ordinary", "correction"}
    folded = fold_assertions(rows)
    assert len(folded.states) == 1 and folded.states[0].value == 182   # correction wins the equal-time tie


@pytest.mark.online
def test_ordinary_rewrite_does_not_downgrade_a_persisted_correction(live_repo, test_neo4j_repo):
    ent, ep = "ent-mono", "ep-mono"
    _mk(test_neo4j_repo, entity=ent, episode=ep)
    corr = _a(entity=ent, episode=ep, span_start=0, span_end=23, value=182,
              semantics="correction", stated_span="Actually I weigh 182 kg")
    live_repo.record_assertion(corr)
    # idempotent re-write of the SAME claim (same span/value) but with the label dropped to ordinary.
    ordinary_rewrite = _a(entity=ent, episode=ep, span_start=0, span_end=23, value=182,
                          semantics="ordinary", stated_span="Actually I weigh 182 kg")
    assert ordinary_rewrite.assertion_key == corr.assertion_key   # same node
    live_repo.record_assertion(ordinary_rewrite)
    rows = [r for r in live_repo.materializable_assertions_for_entity(ent)]
    assert len(rows) == 1
    assert rows[0]["absolute_semantics"] == "correction"          # monotone: never downgraded
