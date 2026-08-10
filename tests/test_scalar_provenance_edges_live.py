"""Live proof for Phase 3 (decision 7.D/10.D, G11): a scalar_state View carries DIRECTED, LABELED
provenance edges matching the fold's algebra, atomically rewritten on every rebuild.

Plan menhir-observation-nodes-and-view-authority-recall, section 2/10.D: a COMPUTED head
(37 + (-17) = 20) has no single source, so the View links its fold inputs by label -- CURRENT_ANCHOR
to the anchoring absolute, CONTRIBUTED_TO to each APPLIED delta (a live input), SUPERSEDED_ANCHOR to
excluded prior anchors. The whole managed edge set is rewritten in one transaction (G11), so a rebuild
never leaves two CURRENT_ANCHORs or a stale CONTRIBUTED_TO, and a former live delta is reclassified to
SUPERSEDED_ANCHOR when a newer absolute subsumes it. Online against the TEST instance (:7688); a
FakeNeo4j cannot evaluate the edge rewrite (G7).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.view_repository import ViewRepository
from menhir.services.scalar_state_service import ScalarStateService


@pytest.fixture
def live(test_neo4j_repo):
    MemoryGraphAdapter(neo4j=test_neo4j_repo).activate_scalar_state()
    repo = TypedAssertionRepository(test_neo4j_repo)
    svc = ScalarStateService(repo, ViewRepository(test_neo4j_repo))
    return repo, svc


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


def _edges(repo, subject_uuid):
    rows = repo.execute(
        """
        MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$u})
        WHERE coalesce(v.view_current, true)
        RETURN [(v)-[:CURRENT_ANCHOR]->(a) | a.value]    AS anchor,
               [(v)-[:CONTRIBUTED_TO]->(d) | d.value]    AS contributed,
               [(v)-[:SUPERSEDED_ANCHOR]->(s) | s.value] AS superseded
        """,
        {"u": subject_uuid},
    )
    r = rows[0] if rows else {}
    return (
        sorted(str(x) for x in (r.get("anchor") or [])),
        sorted(str(x) for x in (r.get("contributed") or [])),
        sorted(str(x) for x in (r.get("superseded") or [])),
    )


@pytest.mark.online
def test_edges_match_fold_algebra_and_are_idempotent(live, test_neo4j_repo):
    repo, svc = live
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    e0, e1, e2 = (f"ep-{uuidlib.uuid4().hex[:8]}" for _ in range(3))
    for ep in (e0, e1, e2):
        _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)

    # 20 (absolute) -> 37 (absolute) -> gave 17 (delta -17). Head = 20; anchor 37, live delta -17,
    # the old 20 absolute is an EXCLUDED prior anchor.
    repo.record_assertion(_obs(entity=ent, episode=e0, operation="absolute", value=20,
                               stated_span="I own 20 rare coins", when="2026-07-01T00:00:00+00:00"))
    repo.record_assertion(_obs(entity=ent, episode=e1, operation="absolute", value=37,
                               stated_span="now I have 37 rare coins", when="2026-07-02T00:00:00+00:00"))
    repo.record_assertion(_obs(entity=ent, episode=e2, operation="delta", value=-17,
                               stated_span="I gave 17 rare coins away", when="2026-07-03T00:00:00+00:00"))

    svc.rebuild_scalar_state(ent)
    anchor, contributed, superseded = _edges(test_neo4j_repo, ent)
    assert anchor == ["37"], anchor                # exactly one CURRENT_ANCHOR -> the anchoring absolute
    assert contributed == ["-17"], contributed     # the applied delta is a live contributor
    assert superseded == ["20"], superseded        # the replaced absolute is superseded, not contributing

    # Idempotent + atomic: a second rebuild leaves exactly ONE anchor (no double-anchor, no stale edge).
    svc.rebuild_scalar_state(ent)
    assert _edges(test_neo4j_repo, ent) == (["37"], ["-17"], ["20"])


@pytest.mark.online
def test_edges_drawn_through_production_adapter_wiring(test_neo4j_repo):
    # REGRESSION: the PRODUCTION consolidation path builds the service via
    # graph_adapter.scalar_state_service(), which injects the ADAPTER (not a ViewRepository) as the View
    # sink. rebuild_scalar_state only draws edges when hasattr(_views, "draw_scalar_state_provenance_edges");
    # without the adapter passthrough the guard is False and CURRENT_ANCHOR is SILENTLY skipped -- so a
    # View can never trace to its anchor and the G14/10.G foundation gate can never let it lead. The other
    # tests inject a ViewRepository directly and thus never exercised the real wiring. Assert through it.
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.activate_scalar_state()
    repo = TypedAssertionRepository(test_neo4j_repo)
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)
    repo.record_assertion(_obs(entity=ent, episode=ep, operation="absolute", value=37,
                               stated_span="I own 37 rare coins", when="2026-07-01T00:00:00+00:00"))

    adapter.scalar_state_service().rebuild_scalar_state(ent, namespace=None)

    anchor, _c, _s = _edges(test_neo4j_repo, ent)
    assert anchor == ["37"], anchor                # CURRENT_ANCHOR drawn through the adapter wiring


@pytest.mark.online
def test_new_absolute_reclassifies_a_live_delta_to_superseded(live, test_neo4j_repo):
    repo, svc = live
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    e0, e1, e2, e3 = (f"ep-{uuidlib.uuid4().hex[:8]}" for _ in range(4))
    for ep in (e0, e1, e2, e3):
        _entity_and_episode(test_neo4j_repo, entity=ent, episode=ep)

    repo.record_assertion(_obs(entity=ent, episode=e0, operation="absolute", value=20,
                               stated_span="I own 20 rare coins", when="2026-07-01T00:00:00+00:00"))
    repo.record_assertion(_obs(entity=ent, episode=e1, operation="absolute", value=37,
                               stated_span="now I have 37 rare coins", when="2026-07-02T00:00:00+00:00"))
    repo.record_assertion(_obs(entity=ent, episode=e2, operation="delta", value=-17,
                               stated_span="I gave 17 rare coins away", when="2026-07-03T00:00:00+00:00"))
    svc.rebuild_scalar_state(ent)
    assert _edges(test_neo4j_repo, ent) == (["37"], ["-17"], ["20"])

    # A newer stated absolute (50 @ t4) becomes the anchor; the -17 delta now PRECEDES it -> unapplied ->
    # SUPERSEDED, and there are NO live contributors. Proves the atomic rewrite reclassifies edges.
    repo.record_assertion(_obs(entity=ent, episode=e3, operation="absolute", value=50,
                               stated_span="I now have 50 rare coins", when="2026-07-04T00:00:00+00:00"))
    svc.rebuild_scalar_state(ent)
    anchor, contributed, superseded = _edges(test_neo4j_repo, ent)
    assert anchor == ["50"], anchor
    assert contributed == [], contributed
    assert superseded == ["-17", "20", "37"], superseded   # both old absolutes + the now-unapplied delta
