"""Live proof for the G14 bridge, slice 1 (plan menhir-observation-nodes-and-view-authority-recall,
10.G/G14): a :TypedAssertion may ground to a :TurnEvidence anchor -- the production ADR-0001 declarant
foundation -- not only to an :Episodic. When the grounding anchor is a :TurnEvidence the write draws
(te)-[:FOUNDS]->(a) so the 10.G basis gate can trace the head to an admitted user statement.

Online (:7688) because it exercises the atomic grounding Cypher against real node labels/edges a
FakeNeo4j cannot evaluate (G7). The change is INERT for every current Episodic-sourced write (proven by
the control case: still GROUNDS, no FOUNDS, binding_pending stays False).
"""

from __future__ import annotations

import os
import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository


def _assertion(*, subject_uuid: str, episode_uuid: str, ns: str, span: str) -> TypedAssertion:
    return TypedAssertion(
        subject_uuid=subject_uuid, subject_display="user", attribute="owned", scope="",
        value_kind="count", unit="", operation="absolute", value=20,
        stated_span=span, span_start=0, span_end=len(span), episode_uuid=episode_uuid,
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1", namespace=ns,
    )


@pytest.mark.online
def test_turnevidence_anchor_grounds_and_founds(test_neo4j_repo):
    repo = TypedAssertionRepository(test_neo4j_repo)
    te_repo = TurnEvidenceRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})

    # a captured user turn is the grounding anchor (NO :Episodic exists for it -- the real Turn box).
    span = "I own 20 rare coins"
    rec_te = te_repo.record_turn_evidence(
        text=span, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="s1", prompt_id="p-live-1")
    turn_id = rec_te["turn_id"]
    assert turn_id

    rec = repo.record_assertion(_assertion(subject_uuid=ent, episode_uuid=turn_id, ns=ns, span=span))
    aid = rec["assertion_id"]
    assert rec["binding_pending"] is False           # a TurnEvidence anchor + entity fully binds
    assert rec["binding_mismatch"] is False

    # the declarant-foundation edge exists, drawn from the admitting TurnEvidence.
    founds = test_neo4j_repo.execute(
        "MATCH (t:TurnEvidence {turn_id:$tid})-[:FOUNDS]->(a:TypedAssertion {assertion_id:$aid}) "
        "RETURN t.declarant AS declarant, t.role AS role",
        {"tid": turn_id, "aid": aid})
    assert len(founds) == 1 and founds[0]["declarant"] == "user"
    # entity provenance still drawn; no Episodic GROUNDS (there is no episode).
    assert test_neo4j_repo.execute(
        "MATCH (:Entity {uuid:$e})-[:HAS_ASSERTION]->(a:TypedAssertion {assertion_id:$aid}) RETURN a",
        {"e": ent, "aid": aid})
    assert test_neo4j_repo.execute(
        "MATCH (:Episodic)-[:GROUNDS]->(a:TypedAssertion {assertion_id:$aid}) RETURN a",
        {"aid": aid}) == []


@pytest.mark.online
def test_episodic_anchor_unchanged_no_founds(test_neo4j_repo):
    # CONTROL: the legacy/fixture Episodic path is byte-identical -- GROUNDS drawn, NO FOUNDS edge.
    repo = TypedAssertionRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    test_neo4j_repo.execute(
        "MERGE (e:Episodic {uuid:$u, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0",
        {"u": ep, "ns": ns},
    )

    rec = repo.record_assertion(
        _assertion(subject_uuid=ent, episode_uuid=ep, ns=ns, span="I own 20 rare coins"))
    aid = rec["assertion_id"]
    assert rec["binding_pending"] is False
    assert test_neo4j_repo.execute(
        "MATCH (:Episodic {uuid:$e})-[:GROUNDS]->(a:TypedAssertion {assertion_id:$aid}) RETURN a",
        {"e": ep, "aid": aid})
    assert test_neo4j_repo.execute(
        "MATCH (:TurnEvidence)-[:FOUNDS]->(a:TypedAssertion {assertion_id:$aid}) RETURN a",
        {"aid": aid}) == []


@pytest.mark.online
def test_scalar_history_preserves_turn_and_admitted_episode_provenance(test_neo4j_repo):
    """History edges retain TurnEvidence IDs while View MENTIONS uses source Episodic UUIDs."""
    from menhir.infrastructure.view_repository import ViewRepository
    from menhir.services.scalar_state_service import ScalarStateService

    repo = TypedAssertionRepository(test_neo4j_repo)
    te_repo = TurnEvidenceRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep_turn = f"ep-{uuidlib.uuid4().hex[:8]}"
    ep_legacy = f"ep-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    test_neo4j_repo.execute(
        "MERGE (e:Episodic {uuid:$u, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0",
        {"u": ep_turn, "ns": ns})
    test_neo4j_repo.execute(
        "MERGE (e:Episodic {uuid:$u, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0",
        {"u": ep_legacy, "ns": ns})
    turn_id = te_repo.record_turn_evidence(
        text="I own 20 rare coins", role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="history-provenance", prompt_id="history-1",
    )["turn_id"]
    test_neo4j_repo.execute(
        "MATCH (e:Episodic {uuid:$ep}), (t:TurnEvidence {turn_id:$tid}) "
        "MERGE (e)-[:ADMITTED_ON]->(t)", {"ep": ep_turn, "tid": turn_id})

    repo.record_assertion(_assertion(
        subject_uuid=ent, episode_uuid=turn_id, ns=ns, span="I own 20 rare coins"))
    repo.record_assertion(TypedAssertion(
        subject_uuid=ent, subject_display="user", attribute="owned", scope="", value_kind="count",
        unit="", operation="delta", value=5, stated_span="I found 5 more rare coins", span_start=0,
        span_end=24, episode_uuid=ep_legacy, valid_at="2026-07-02T00:00:00+00:00",
        learned_at="2026-07-02T00:00:00+00:00", evidence_tier="agent", perceiver_version="v1",
        namespace=ns))

    views = ViewRepository(test_neo4j_repo)
    result = ScalarStateService(repo, views).rebuild_scalar_history(ent, namespace=ns)
    assert result["complete"] is True
    view = views.fetch_scalar_history(
        subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="", namespace=ns)
    assert view is not None
    assert view["entry_count"] == 2
    entries = views.list_scalar_history_entries(view_uuid=view["uuid"], namespace=ns)
    by_id = {entry["assertion_id"]: entry for entry in entries["entries"]}
    assert any(entry.get("turn_id") == turn_id for entry in by_id.values())
    assert any(entry.get("episode_uuid") == ep_turn for entry in by_id.values())
    assert any(entry.get("episode_uuid") == ep_legacy and not entry.get("turn_id")
               for entry in by_id.values())

    chain = test_neo4j_repo.execute(
        "MATCH (v:Entity {uuid:$view})-[:HISTORY_ENTRY]->(a:TypedAssertion), "
        "(t:TurnEvidence {turn_id:$tid})-[:FOUNDS]->(a), "
        "(ep:Episodic {uuid:$ep})-[:ADMITTED_ON]->(t), "
        "(ep)-[:MENTIONS]->(v) "
        "RETURN count(DISTINCT a) AS assertions, count(DISTINCT v) AS views",
        {"view": view["uuid"], "tid": turn_id, "ep": ep_turn})
    assert chain and chain[0]["assertions"] == 1 and chain[0]["views"] == 1

    legacy = test_neo4j_repo.execute(
        "MATCH (ep:Episodic {uuid:$ep})-[:GROUNDS]->(a:TypedAssertion), "
        "(v:Entity {uuid:$view})-[:HISTORY_ENTRY]->(a) "
        "RETURN count(a) AS grounded",
        {"ep": ep_legacy, "view": view["uuid"]})
    assert legacy and legacy[0]["grounded"] == 1


@pytest.mark.online
@pytest.mark.skipif(
    not os.getenv("MENHIR_TEST_NEO4J_URI"),
    reason="requires an explicit MENHIR_TEST_NEO4J_URI test instance",
)
def test_exact_lme_postcard_history_replay_and_provenance(test_neo4j_repo):
    """The exact two-delta postcard fixture is history-only, replayable, and fully grounded."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    repo = TypedAssertionRepository(test_neo4j_repo)
    te_repo = TurnEvidenceRepository(test_neo4j_repo)
    adapter = MemoryGraphAdapter(test_neo4j_repo)
    namespace = f"postcard-{uuidlib.uuid4().hex[:8]}"
    subject = "lme-01493427"
    source_episodes = [
        f"{subject}-episode-{uuidlib.uuid4().hex[:8]}",
        f"{subject}-episode-{uuidlib.uuid4().hex[:8]}",
    ]
    test_neo4j_repo.execute(
        "MERGE (n:Entity {uuid:$subject}) "
        "SET n.name='user', n.group_id=$namespace, n.namespace=$namespace, n.test_tag=$namespace",
        {"subject": subject, "namespace": namespace},
    )
    test_neo4j_repo.execute(
        "UNWIND $episodes AS episode "
        "CREATE (e:Episodic {uuid:episode, group_id:$namespace, namespace:$namespace, "
        "test_tag:$namespace, evidence_finalized:true, evidence_generation:0})",
        {"episodes": source_episodes, "namespace": namespace},
    )

    turns = []
    for ordinal, value in enumerate((17, 25), start=1):
        turn = te_repo.record_turn_evidence(
            text=f"I have {value} postcards",
            role="user", declarant="user", namespace=namespace,
            source_kind="claude_code_hook", session_id=f"{subject}-session-{ordinal}",
            prompt_id=f"{subject}-prompt-{ordinal}",
        )["turn_id"]
        turns.append(turn)
    test_neo4j_repo.execute(
        "UNWIND $pairs AS pair "
        "MATCH (e:Episodic {uuid:pair.episode}), (t:TurnEvidence {turn_id:pair.turn}) "
        "MERGE (e)-[:ADMITTED_ON]->(t)",
        {"pairs": [
            {"episode": episode, "turn": turn}
            for episode, turn in zip(source_episodes, turns, strict=True)
        ]},
    )

    assertion_ids = []
    for value, valid_at, turn in zip(
        (17, 25),
        ("2023-08-11T00:00:00+00:00", "2023-11-30T00:00:00+00:00"),
        turns,
        strict=True,
    ):
        span = f"I have {value} postcards"
        rec = repo.record_assertion(TypedAssertion(
            subject_uuid=subject, subject_display="user", attribute="postcard_count",
            scope="collection", value_kind="count", unit="postcards", operation="delta",
            value=value, stated_span=span, span_start=0, span_end=len(span), episode_uuid=turn,
            valid_at=valid_at, learned_at=valid_at, evidence_tier="user", perceiver_version="v1",
            namespace=namespace,
        ))
        assert rec["binding_pending"] is False
        assertion_ids.append(rec["assertion_id"])

    svc = adapter.scalar_state_service(scalar_history_enabled=True)
    first = svc.rebuild_scalar_projections(
        subject, namespace=namespace, history_enabled=True)
    assert first["complete"] is True
    assert first["state"]["abstained"][0]["reason"] == "no_anchor"
    assert adapter.fetch_current_scalar_view_for_slot(
        subject_uuid=subject, attribute="postcard_count", scope="collection",
        value_kind="count", unit="postcards", namespace=namespace,
    ) is None
    view = adapter.fetch_scalar_history(
        subject_uuid=subject, attribute="postcard_count", scope="collection",
        value_kind="count", unit="postcards", namespace=namespace,
    )
    assert view is not None and view["entry_count"] == 2
    current_views = test_neo4j_repo.execute(
        "MATCH (v:Entity {view_kind:'scalar_history', view_subject_uuid:$subject, "
        "group_id:$namespace}) WHERE coalesce(v.view_current,true) "
        "RETURN count(v) AS current_views",
        {"subject": subject, "namespace": namespace},
    )
    assert current_views[0]["current_views"] == 1

    edges = test_neo4j_repo.execute(
        "MATCH (v:Entity {uuid:$view})-[he:HISTORY_ENTRY]->(a:TypedAssertion) "
        "RETURN count(he) AS edge_count, collect(he.ordinal) AS ordinals, "
        "collect(a.assertion_id) AS assertion_ids",
        {"view": view["uuid"]},
    )[0]
    assert edges["edge_count"] == 2
    assert sorted(edges["ordinals"]) == [0, 1]
    assert set(edges["assertion_ids"]) == set(assertion_ids)

    provenance = test_neo4j_repo.execute(
        "MATCH (v:Entity {uuid:$view})-[:HISTORY_ENTRY]->(a:TypedAssertion) "
        "MATCH (t:TurnEvidence {turn_id:a.episode_uuid})-[:FOUNDS]->(a) "
        "MATCH (ep:Episodic)-[:ADMITTED_ON]->(t) "
        "MATCH (ep)-[:MENTIONS]->(v) "
        "RETURN count(DISTINCT a) AS assertions, count(DISTINCT t) AS turns, "
        "count(DISTINCT ep) AS episodes, count(DISTINCT v) AS views",
        {"view": view["uuid"]},
    )[0]
    assert provenance == {"assertions": 2, "turns": 2, "episodes": 2, "views": 1}

    entries = adapter.list_scalar_history_entries(
        view_uuid=view["uuid"], namespace=namespace)
    ordered = sorted(entries["entries"], key=lambda entry: entry["ordinal"])
    assert entries["total"] == 2
    assert [int(entry["value"]) for entry in ordered] == [17, 25]
    assert [entry["operation"] for entry in ordered] == ["delta", "delta"]
    assert [entry["valid_at"][:10] for entry in ordered] == ["2023-08-11", "2023-11-30"]

    second = svc.rebuild_scalar_projections(
        subject, namespace=namespace, history_enabled=True)
    assert second["complete"] is True
    assert test_neo4j_repo.execute(
        "MATCH (v:Entity {uuid:$view})-[he:HISTORY_ENTRY]->() RETURN count(he) AS edges",
        {"view": view["uuid"]},
    )[0]["edges"] == 2


@pytest.mark.online
def test_turnevidence_anchor_without_entity_stays_pending(test_neo4j_repo):
    # a TurnEvidence anchor with NO resolvable entity is still binding_pending (n IS NULL) -- FOUNDS is
    # provenance, not a substitute for subject binding.
    repo = TypedAssertionRepository(test_neo4j_repo)
    te_repo = TurnEvidenceRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    span = "I own 20 rare coins"
    turn_id = te_repo.record_turn_evidence(
        text=span, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="s2", prompt_id="p-live-2")["turn_id"]

    rec = repo.record_assertion(
        _assertion(subject_uuid=f"unbound:{uuidlib.uuid4().hex}", episode_uuid=turn_id, ns=ns, span=span))
    assert rec["binding_pending"] is True


# --- slice 3: the 10.G basis gate reads the FOUNDS foundation via the View's CURRENT_ANCHOR ---

def _rebuilt_view(raw, *, ns, ent):
    from menhir.services.scalar_state_service import ScalarStateService
    from menhir.infrastructure.view_repository import ViewRepository
    views = ViewRepository(raw)
    ScalarStateService(TypedAssertionRepository(raw), views).rebuild_scalar_state(ent, namespace=ns)
    return views, views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="", namespace=ns)


@pytest.mark.online
def test_scalar_view_has_user_foundation_true_for_turnevidence(test_neo4j_repo):
    # a View whose CURRENT_ANCHOR traces to a declarant='user' :TurnEvidence HAS a source foundation --
    # the bridge payoff: an agent-tier extraction of an ADMITTED user statement may lead.
    repo = TypedAssertionRepository(test_neo4j_repo)
    te_repo = TurnEvidenceRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    span = "I own 20 rare coins"
    turn_id = te_repo.record_turn_evidence(
        text=span, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="s3", prompt_id="p-live-3")["turn_id"]
    repo.record_assertion(_assertion(subject_uuid=ent, episode_uuid=turn_id, ns=ns, span=span))

    views, view = _rebuilt_view(test_neo4j_repo, ns=ns, ent=ent)
    assert view is not None                                   # the View materialized
    assert views.scalar_view_has_user_foundation(view_uuid=view["uuid"], namespace=ns) is True
    # namespace isolation: another tenant sees no foundation for this View.
    assert views.scalar_view_has_user_foundation(view_uuid=view["uuid"], namespace="ns-other") is False


@pytest.mark.online
def test_scalar_view_has_no_foundation_for_episodic(test_neo4j_repo):
    # CONTROL: an agent-tier extraction with NO user admission (Episodic fixture, no FOUNDS) has NO
    # foundation -> stays advisory (governance: probabilistic extraction never self-authorizes).
    repo = TypedAssertionRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    test_neo4j_repo.execute(
        "MERGE (e:Episodic {uuid:$u, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0",
        {"u": ep, "ns": ns},
    )
    repo.record_assertion(_assertion(subject_uuid=ent, episode_uuid=ep, ns=ns, span="I own 20 rare coins"))

    views, view = _rebuilt_view(test_neo4j_repo, ns=ns, ent=ent)
    assert view is not None
    assert views.scalar_view_has_user_foundation(view_uuid=view["uuid"], namespace=ns) is False


@pytest.mark.online
def test_current_expiries_and_expiry_user_foundation(test_neo4j_repo):
    # G13 slice A: an EXPIRED slot (a newer `expire` after the absolute) is read from the fold via
    # current_expiries, and its verdict LEADS only when the expire traces to a user :TurnEvidence
    # foundation (assertions_have_user_foundation over the expiry's contributors).
    from menhir.services.scalar_state_service import ScalarStateService
    from menhir.infrastructure.view_repository import ViewRepository
    repo = TypedAssertionRepository(test_neo4j_repo)
    te_repo = TurnEvidenceRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    test_neo4j_repo.execute(
        "MERGE (e:Episodic {uuid:$u, namespace:$ns, group_id:$ns}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0",
        {"u": ep, "ns": ns},
    )
    # older absolute (Episodic-grounded), then a NEWER user-declared expire (TurnEvidence-founded).
    repo.record_assertion(TypedAssertion(
        subject_uuid=ent, subject_display="user", attribute="owned", scope="", value_kind="count",
        unit="", operation="absolute", value=37, stated_span="I own 37 rare coins", span_start=0,
        span_end=19, episode_uuid=ep, valid_at="2026-06-01T00:00:00+00:00",
        learned_at="2026-06-01T00:00:00+00:00", evidence_tier="agent", perceiver_version="v1",
        namespace=ns))
    span = "I used to own rare coins"
    turn_id = te_repo.record_turn_evidence(
        text=span, role="user", declarant="user", namespace=ns,
        source_kind="claude_code_hook", session_id="s5", prompt_id="p-live-5")["turn_id"]
    repo.record_assertion(TypedAssertion(
        subject_uuid=ent, subject_display="user", attribute="owned", scope="", value_kind="count",
        unit="", operation="expire", value=37, stated_span=span, span_start=0, span_end=len(span),
        episode_uuid=turn_id, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier="agent", perceiver_version="v1",
        namespace=ns))

    views = ViewRepository(test_neo4j_repo)
    svc = ScalarStateService(repo, views)
    expiries = svc.current_expiries(ent, namespace=ns)
    assert ("owned", "", "count", "") in expiries           # the slot folded to an Expiry, no View
    exp = expiries[("owned", "", "count", "")]
    assert views.assertions_have_user_foundation(
        assertion_ids=list(exp.contributor_ids), namespace=ns) is True   # user-declared expire
    # namespace isolation.
    assert views.assertions_have_user_foundation(
        assertion_ids=list(exp.contributor_ids), namespace="ns-other") is False
    # and there is NO current View for the slot (expired by design).
    assert views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="", namespace=ns) is None
