"""Live proof (:7688) that the evidence-projection Cypher actually does what its unit tests claim.

WHY ONLINE. `tests/test_evidence_projection.py` asserts on the query STRING -- that it contains
`p.content = t.text`, `MERGE (p)-[:ADMITTED_ON]->(t)`, and so on. Every one of those would pass with
Cypher that does not parse, or that parses and behaves differently: MERGE-on-a-property semantics,
the ON CREATE / uuid idempotency trick, and the `role`/`declarant` filter are all things a string
assertion cannot check. This file executes the query against a real Neo4j and asserts on the GRAPH.

The idempotency check in particular is unfalsifiable offline: it depends on MERGE matching an
existing node by `evidence_projection_of` and on our uuid NOT being written the second time.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.self_identity import self_subject_endpoint_for_claim
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository


def _seed_turn(raw, *, ns, text, role="user", declarant="user", occurred_at=None):
    return TurnEvidenceRepository(raw).record_turn_evidence(
        text=text, role=role, declarant=declarant, namespace=ns,
        source_kind="claude_code_hook", session_id="s",
        occurred_at=occurred_at,
        prompt_id=f"p-{uuidlib.uuid4().hex[:8]}")["turn_id"]


def _project(raw, turn_id, *, ns, projection_uuid=None):
    """Through the ADAPTER, the way ingest reaches it -- not by hand-constructing the repository."""
    return MemoryGraphAdapter(neo4j=raw).create_evidence_projection(
        turn_evidence_uuid=turn_id,
        projection_uuid=projection_uuid or str(uuidlib.uuid4()),
        name=f"evidence-projection-{turn_id}",
        session_id="s", user_id="u", namespace=ns,
    )


@pytest.mark.online
def test_projects_the_turn_text_verbatim(test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    text = "I have 25 postcards in the blue album"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text=text)

    puid = _project(test_neo4j_repo, tid, ns=ns)
    assert puid

    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $u}) RETURN p.content AS content, p.source AS source, "
        "p.is_evidence_projection AS flag, p.processing_state AS state, "
        "p.evidence_projection_of AS of, p.source_confidence AS conf",
        params={"u": puid})
    assert len(rows) == 1
    row = rows[0]
    # The point of the whole feature: the USER's words, not a paraphrase.
    assert row["content"] == text
    assert row["source"] == "user"
    assert row["flag"] is True
    assert row["state"] == "PENDING"      # or it never enriches and yields no entities
    assert row["of"] == tid
    assert row["conf"] == 1.0


@pytest.mark.online
def test_the_projection_is_linked_to_its_turn(test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 25 postcards")
    puid = _project(test_neo4j_repo, tid, ns=ns)

    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $u})-[:ADMITTED_ON]->(t:TurnEvidence) RETURN t.turn_id AS tid",
        params={"u": puid})
    assert [r["tid"] for r in rows] == [tid]


@pytest.mark.online
def test_atomic_claim_authorizes_exact_projection_once(test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    text = "I have 25 postcards"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text=text)
    puid = _project(test_neo4j_repo, tid, ns=ns)
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    claimed = adapter.claim_pending_episode(
        puid,
        max_attempts=3,
        worker_id="worker-a",
        lease_seconds=60,
    )

    assert claimed is not None
    assert claimed["subject_endpoint_eligible"] is True
    assert claimed["turn_evidence_count"] == 1
    assert claimed["turn_evidence_text"] == text
    endpoint = self_subject_endpoint_for_claim(claimed)
    assert endpoint is not None
    assert endpoint.episode_uuid == puid
    assert endpoint.turn_evidence_uuid == tid

    # The eligibility receipt and lease are one statement. A concurrent/stale worker cannot
    # independently claim the same projection and obtain a second authority decision.
    assert adapter.claim_pending_episode(
        puid,
        max_attempts=3,
        worker_id="worker-b",
        lease_seconds=60,
    ) is None


@pytest.mark.online
def test_atomic_claim_refuses_projection_with_multiple_turn_links(test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 25 postcards")
    other_tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 26 postcards")
    puid = _project(test_neo4j_repo, tid, ns=ns)
    test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $p}), (t:TurnEvidence {turn_id: $t}) "
        "MERGE (p)-[:ADMITTED_ON]->(t)",
        params={"p": puid, "t": other_tid},
    )

    claimed = MemoryGraphAdapter(neo4j=test_neo4j_repo).claim_pending_episode(
        puid,
        max_attempts=3,
        worker_id="worker-a",
        lease_seconds=60,
    )

    assert claimed is not None
    assert claimed["turn_evidence_count"] == 2
    assert claimed["subject_endpoint_eligible"] is False
    assert self_subject_endpoint_for_claim(claimed) is None


@pytest.mark.online
def test_a_second_memory_citing_the_same_turn_projects_nothing_new(test_neo4j_repo):
    """THE CHECK THAT CANNOT BE MADE OFFLINE. Two memories citing one turn must yield ONE projection.
    This depends on MERGE matching by `evidence_projection_of` and on our uuid not being written the
    second time -- both real database behaviors, not string properties."""
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 25 postcards")

    first = _project(test_neo4j_repo, tid, ns=ns, projection_uuid="proj-first")
    second = _project(test_neo4j_repo, tid, ns=ns, projection_uuid="proj-second")

    assert first == "proj-first"
    assert second is None, "second call must report no creation, or it re-enqueues enrichment"

    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN count(p) AS c", params={"t": tid})
    assert rows[0]["c"] == 1, "a second projection would duplicate every entity from this turn"

    # and the survivor is the ORIGINAL, not silently rewritten by the second call
    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN p.uuid AS u", params={"t": tid})
    assert rows[0]["u"] == "proj-first"


@pytest.mark.online
def test_projection_carries_replay_source_time(test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(
        test_neo4j_repo,
        ns=ns,
        text="I have 25 postcards",
        occurred_at="2023-11-30T20:25:00Z",
    )

    puid = _project(test_neo4j_repo, tid, ns=ns)
    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $u}) RETURN toString(p.reference_time) AS reference_time",
        params={"u": puid},
    )

    assert rows[0]["reference_time"].startswith("2023-11-30T20:25:00")


@pytest.mark.online
@pytest.mark.parametrize("role,declarant", [
    ("assistant", "assistant"),   # the model restating a user fact
    ("user", "assistant"),        # role says user but the declarant does not
    ("assistant", "user"),        # and the converse
])
def test_refuses_anything_not_user_declared(test_neo4j_repo, role, declarant):
    """ADR 0001's precision boundary, executed rather than grepped: an assistant restating a user's
    fact must never become evidence for it."""
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="You have 25 postcards", role=role,
                     declarant=declarant)
    assert _project(test_neo4j_repo, tid, ns=ns) is None
    rows = test_neo4j_repo.execute(
        "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN count(p) AS c", params={"t": tid})
    assert rows[0]["c"] == 0


@pytest.mark.online
def test_a_missing_turn_creates_nothing(test_neo4j_repo):
    """MATCH-only on the turn: a bogus id must draw nothing rather than MERGE a stub into existence."""
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    assert _project(test_neo4j_repo, f"no-such-turn-{uuidlib.uuid4().hex[:8]}", ns=ns) is None


@pytest.mark.online
def test_the_projection_is_absent_from_ordinary_memory_listing(test_neo4j_repo):
    """ADR 0001's boundary, end to end: the projection must not surface where curated memories do.
    Runs the SHARED predicate (non_structural_memory_cypher) against the database rather than
    inspecting its text, and proves it does not also hide ordinary episodes."""
    from menhir.domain.structural_memory import non_structural_memory_cypher

    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    tid = _seed_turn(test_neo4j_repo, ns=ns, text="I have 25 postcards")
    puid = _project(test_neo4j_repo, tid, ns=ns)

    ordinary = str(uuidlib.uuid4())
    MemoryGraphAdapter(neo4j=test_neo4j_repo).create_pending_episode(
        episode_uuid=ordinary, name="ordinary", content="User owns 25 postcards",
        session_id="s", user_id="u", source="claude-code", source_confidence=0.7, namespace=ns)

    rows = test_neo4j_repo.execute(
        f"MATCH (n:Episodic) WHERE n.namespace = $ns AND {non_structural_memory_cypher('n')} "
        "RETURN n.uuid AS uuid", params={"ns": ns})
    visible = {r["uuid"] for r in rows}
    assert ordinary in visible, "the filter must not hide ordinary episodes (coalesce, not `= false`)"
    assert puid not in visible, "raw turn text leaked into a curated-memory listing"
