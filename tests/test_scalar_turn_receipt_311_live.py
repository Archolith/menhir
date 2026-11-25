"""#311: a scalar View built from a captured turn that no Episodic admitted yet carries the
TurnEvidence turn_id as its receipt, so it has live provenance and is recall-visible. A finalized
admitting episode becomes the receipt; a non-finalized one is resolved back to the turn by the
write path. A turn that is not usable View evidence (captured before finalization existed) keeps
the old empty receipt, so it cannot abort the rebuild. Online: needs the test Neo4j instance."""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.recall_visibility import default_recall_visibility_cypher
from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.view_repository import ViewRepository
from menhir.services.scalar_state_service import ScalarStateService

pytestmark = pytest.mark.online

SPAN = "I own 20 rare coins"


def _turn_assertion(raw, repo, ns, ent, *, attribute="owned", prompt=""):
    turn_id = TurnEvidenceRepository(raw).record_turn_evidence(
        text=SPAN, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="s311",
        prompt_id=f"p-{ns}{prompt}")["turn_id"]
    repo.record_assertion(TypedAssertion(
        subject_uuid=ent, subject_display="user", attribute=attribute, scope="",
        value_kind="count", unit="", operation="absolute", value=20, stated_span=SPAN,
        span_start=0, span_end=len(SPAN), episode_uuid=turn_id,
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1", namespace=ns))
    return turn_id


def _setup(raw):
    ns = f"ns311-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent311-{uuidlib.uuid4().hex[:8]}"
    raw.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    repo = TypedAssertionRepository(raw)
    turn_id = _turn_assertion(raw, repo, ns, ent)
    return ns, ent, turn_id, repo


def _rebuild(raw, repo, ns, ent):
    views = ViewRepository(raw)
    ScalarStateService(repo, views).rebuild_scalar_state(ent, namespace=ns)
    return views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="", namespace=ns)


def _visible(raw, view_uuid: str) -> bool:
    rows = raw.execute(
        f"MATCH (n:Entity {{uuid:$u}}) RETURN {default_recall_visibility_cypher('n')} AS ok",
        {"u": view_uuid})
    return bool(rows and rows[0]["ok"])


def _receipt(raw, view_uuid: str) -> list[str]:
    rows = raw.execute("MATCH (n:Entity {uuid:$u}) RETURN n.episode_uuids AS eps", {"u": view_uuid})
    return list(rows[0]["eps"] or [])


def _mentions(raw, view_uuid: str) -> set[str]:
    rows = raw.execute(
        "MATCH (e)-[:MENTIONS]->(:Entity {uuid:$u}) RETURN coalesce(e.uuid, e.turn_id) AS id",
        {"u": view_uuid})
    return {r["id"] for r in rows}


def test_turn_only_view_receipt_is_the_turn_and_is_recall_visible(test_neo4j_repo) -> None:
    raw = test_neo4j_repo
    ns, ent, turn_id, repo = _setup(raw)

    rows = repo.assertions_for_entity(ent, materializable_only=True, namespace=ns)
    assert [r["episode_uuid"] for r in rows] == [turn_id]
    assert [r["source_episode_uuid"] for r in rows] == [""]  # still no admitted episode

    view = _rebuild(raw, repo, ns, ent)
    assert view is not None
    assert _receipt(raw, view["uuid"]) == [turn_id]
    assert _mentions(raw, view["uuid"]) == {turn_id}
    assert _visible(raw, view["uuid"])


def test_admission_moves_the_receipt_to_the_episode(test_neo4j_repo) -> None:
    raw = test_neo4j_repo
    ns, ent, turn_id, repo = _setup(raw)
    ep = f"ep311-{uuidlib.uuid4().hex[:8]}"
    raw.execute(
        "MATCH (t:TurnEvidence {turn_id:$tid}) "
        "MERGE (e:Episodic {uuid:$ep, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0 "
        "MERGE (e)-[:ADMITTED_ON]->(t)",
        {"tid": turn_id, "ep": ep, "ns": ns})

    rows = repo.assertions_for_entity(ent, materializable_only=True, namespace=ns)
    assert [r["episode_uuid"] for r in rows] == [ep]

    view = _rebuild(raw, repo, ns, ent)
    assert view is not None
    assert _receipt(raw, view["uuid"]) == [ep]
    assert _mentions(raw, view["uuid"]) == {ep}
    assert _visible(raw, view["uuid"])


def test_view_built_before_admission_stays_visible_after_it(test_neo4j_repo) -> None:
    raw = test_neo4j_repo
    ns, ent, turn_id, repo = _setup(raw)
    before = _rebuild(raw, repo, ns, ent)
    assert before is not None and _receipt(raw, before["uuid"]) == [turn_id]

    ep = f"ep311-{uuidlib.uuid4().hex[:8]}"
    raw.execute(
        "MATCH (t:TurnEvidence {turn_id:$tid}) "
        "MERGE (e:Episodic {uuid:$ep, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0 "
        "MERGE (e)-[:ADMITTED_ON]->(t)",
        {"tid": turn_id, "ep": ep, "ns": ns})
    after = _rebuild(raw, repo, ns, ent)
    assert after is not None
    assert _receipt(raw, after["uuid"]) == [ep]  # authoritative replace, not a union
    assert set(_receipt(raw, after["uuid"])) == _mentions(raw, after["uuid"])
    assert _visible(raw, after["uuid"])


def test_non_finalized_admitter_resolves_back_to_the_turn(test_neo4j_repo) -> None:
    # Review F2: production Episodics are not finalized evidence; the write path resolves such
    # an admitter to its turn, so the receipt stays the turn and the View stays visible.
    raw = test_neo4j_repo
    ns, ent, turn_id, repo = _setup(raw)
    ep = f"ep311-{uuidlib.uuid4().hex[:8]}"
    raw.execute(
        "MATCH (t:TurnEvidence {turn_id:$tid}) "
        "MERGE (e:Episodic {uuid:$ep, namespace:$ns, group_id:$ns}) "
        "MERGE (e)-[:ADMITTED_ON]->(t)",
        {"tid": turn_id, "ep": ep, "ns": ns})
    view = _rebuild(raw, repo, ns, ent)
    assert view is not None
    assert _receipt(raw, view["uuid"]) == [turn_id]
    assert _visible(raw, view["uuid"])


def test_legacy_unfinalized_turn_does_not_abort_the_rebuild(test_neo4j_repo) -> None:
    # Review F1: a turn captured before evidence finalization existed is not usable View
    # evidence. Using it as the receipt made the write gate refuse and aborted the whole entity
    # rebuild; it must keep the old empty receipt and leave the other slots unaffected.
    raw = test_neo4j_repo
    ns, ent, legacy, repo = _setup(raw)
    raw.execute(
        "MATCH (t:TurnEvidence {turn_id:$tid}) "
        "REMOVE t.evidence_finalized, t.evidence_generation", {"tid": legacy})
    fresh = _turn_assertion(raw, repo, ns, ent, attribute="visited", prompt="-2")

    rows = {r["attribute"]: r for r in repo.assertions_for_entity(
        ent, materializable_only=True, namespace=ns)}
    assert rows["owned"]["episode_uuid"] == ""
    assert rows["visited"]["episode_uuid"] == fresh

    views = ViewRepository(raw)
    ScalarStateService(repo, views).rebuild_scalar_state(ent, namespace=ns)  # must not raise
    other = views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="visited", scope="", value_kind="count", unit="",
        namespace=ns)
    assert other is not None
    assert _receipt(raw, other["uuid"]) == [fresh]
    assert _visible(raw, other["uuid"])
