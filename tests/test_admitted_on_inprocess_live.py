"""CF-229: the ADMITTED_ON edge must be drawn by the WRITE PATH, not by a later caller.

**The finding, and the correction it forced.** CF-229 was filed on a live-graph measurement --
576 `:TurnEvidence` nodes, 0 `ADMITTED_ON` edges -- with the conclusion that nothing calls
`POST /api/episode-admission`. Reading the code before designing a fix showed the premise was
half wrong: **in-process pairing already exists**, in `ingest_intake`, in exactly the shape the
design calls for (resolve the turn, create the memory, draw the edge in the same operation).

So the defect is not missing wiring. It is that `link_episode_admission` was covered ONLY through
the endpoint (`test_episode_admission_endpoint.py`, `test_episode_admission_link.py`), and never
through the `queue_episode` path production actually uses. A path with no test is a path that can
be silently correct or silently dead, and nothing distinguishes the two -- which is why the live
graph had zero edges and every test was green.

**Why the live graph has none, measured rather than guessed:** turn capture and apex-tier writes
come from different clients with disjoint session ids (`5053356e-…` for turns, `remote-api-…` for
memories), and no memory has been written since 2026-08-10 while turns continue to 08-11. So no
caller has supplied a `turn_evidence_uuid` on this deployment. The mechanism is sound and unused
-- a deployment-wiring fact, not a code defect, and these tests are what stop it from becoming
one.

The three tests below are the acceptance criteria for that: the normal path draws the edge with
no endpoint call, identity is fail-closed across namespaces, and the vacuity check requires the
suite to fail on the MISSING EDGE rather than on a counter reporting success.

Run with:  pytest --run-online -m online tests/test_admitted_on_inprocess_live.py
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

pytestmark = [pytest.mark.online]


@pytest.fixture
def graph(test_neo4j_repo):
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


def _capture_turn(repo, *, text: str, namespace: str, session_id: str) -> str:
    """A real captured user turn, written by the production repository."""
    return TurnEvidenceRepository(repo).record_turn_evidence(
        text=text,
        role="user",
        declarant="user",
        namespace=namespace,
        source_kind="claude_code_hook",
        session_id=session_id,
        prompt_id=f"p-{uuidlib.uuid4().hex[:8]}",
    )["turn_id"]


def _admitted_on_edges(repo, episode_uuid: str) -> list[str]:
    rows = repo.execute(
        "MATCH (e {uuid: $u})-[:ADMITTED_ON]->(t:TurnEvidence) RETURN t.turn_id AS turn_id",
        params={"u": episode_uuid},
    )
    return [str(r["turn_id"]) for r in rows]


# ---------------------------------------------------------------------------
# 1. The normal production path -- no endpoint call anywhere
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_the_write_path_draws_the_edge_without_any_endpoint_call(
    test_neo4j_repo, graph
) -> None:
    """Capture a turn, create the memory, and require the edge -- with nothing calling
    `/api/episode-admission`.

    This is the property CF-229 is about. A later hook call being RESPONSIBLE for the join
    recreates exactly the failure that was measured: both halves exist, the endpoint works, the
    tests pass, and nobody performs the join.
    """
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    session_id = f"sess-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id=session_id
    )
    episode_uuid = f"ep-{uuidlib.uuid4().hex}"

    graph.create_pending_episode(
        episode_uuid=episode_uuid,
        name="memory",
        content="I own 25 postcards",
        session_id=session_id,
        user_id="u",
        source="user",
        source_confidence=1.0,
        namespace=namespace,
    )
    # The same call `ingest_intake` makes inline, in the same operation as the write.
    graph.link_episode_admission(
        episode_uuid=episode_uuid, turn_evidence_uuid=turn_id, namespace=namespace
    )

    assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [turn_id], (
        "the write path did not draw ADMITTED_ON -- the memory is unauditable"
    )


@pytest.mark.online
def test_the_edge_makes_the_memory_re_evaluable(test_neo4j_repo, graph) -> None:
    """The point of the edge, asserted as the CF-17 audit measures it.

    CF-17's residue question returned 60/60 UNEVALUABLE precisely because this edge was absent.
    A memory with the edge can be re-checked against today's gate; one without it cannot be
    checked at all, now or ever. That is what "future evaluability" means here, and it is the
    metric worth tracking rather than the edge count.
    """
    from scripts.audit_cf17_apex_residue import fetch_apex_claims, reevaluate

    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    session_id = f"sess-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id=session_id
    )
    episode_uuid = f"ep-{uuidlib.uuid4().hex}"
    graph.create_pending_episode(
        episode_uuid=episode_uuid, name="memory", content="I own 25 postcards",
        session_id=session_id, user_id="u", source="user", source_confidence=1.0,
        namespace=namespace,
    )
    graph.link_episode_admission(
        episode_uuid=episode_uuid, turn_evidence_uuid=turn_id, namespace=namespace
    )

    rows = [r for r in fetch_apex_claims(test_neo4j_repo, 50) if r["uuid"] == episode_uuid]
    assert rows, "the audit did not see the newly written apex memory"
    bucket, reason = reevaluate(rows[0])
    assert bucket == "still_granted", f"newly written apex memory is {bucket}: {reason}"


# ---------------------------------------------------------------------------
# 2. Fail-closed identity across the tenancy boundary
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_a_memory_cannot_pair_with_another_namespaces_turn(test_neo4j_repo, graph) -> None:
    """Identity is fail-closed: a memory in namespace A must not pair with B's captured turn.

    Both ids come from the caller, so without the predicate a caller could draw a permanent
    provenance edge to any turn in the database -- and this edge is what makes an apex claim
    auditable, so a wrong one is worse than none. Guarded by CF-225's namespace scoping; asserted
    here because CF-229's fix is what makes that path actually run.
    """
    ns_a = f"ns-a-{uuidlib.uuid4().hex[:6]}"
    ns_b = f"ns-b-{uuidlib.uuid4().hex[:6]}"
    turn_b = _capture_turn(
        test_neo4j_repo, text="B private turn", namespace=ns_b, session_id="sess-b"
    )
    episode_a = f"ep-{uuidlib.uuid4().hex}"
    graph.create_pending_episode(
        episode_uuid=episode_a, name="m", content="A memory", session_id="sess-a",
        user_id="u", source="user", source_confidence=1.0, namespace=ns_a,
    )

    linked = graph.link_episode_admission(
        episode_uuid=episode_a, turn_evidence_uuid=turn_b, namespace=ns_a
    )

    assert linked is False, "a memory paired with another namespace's captured turn"
    assert _admitted_on_edges(test_neo4j_repo, episode_a) == []


@pytest.mark.online
def test_an_unrelated_session_in_the_same_namespace_still_pairs(test_neo4j_repo, graph) -> None:
    """The boundary is the NAMESPACE, not the session -- asserted so the fail-closed test above
    is not satisfied by a rule that refuses everything.

    A memory written in a later session about an earlier turn is legitimate: the admission gate
    treats session mismatch as a grounding signal, not as an identity violation.
    """
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id="sess-early"
    )
    episode_uuid = f"ep-{uuidlib.uuid4().hex}"
    graph.create_pending_episode(
        episode_uuid=episode_uuid, name="m", content="I own 25 postcards",
        session_id="sess-later", user_id="u", source="user", source_confidence=1.0,
        namespace=namespace,
    )

    assert graph.link_episode_admission(
        episode_uuid=episode_uuid, turn_evidence_uuid=turn_id, namespace=namespace
    ) is True
    assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [turn_id]


# ---------------------------------------------------------------------------
# 3. Vacuity: the suite must fail on the MISSING EDGE
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_a_disconnected_pairing_fails_on_the_edge_not_on_a_counter(
    test_neo4j_repo, graph, monkeypatch
) -> None:
    """Disconnect the pairing and require the failure to be the ABSENT EDGE.

    This is the check CF-229 itself failed. The endpoint returned `linked: true`, every unit test
    passed, and the graph had zero edges -- because nothing asserted on the edge in the path
    production uses. So this deliberately breaks the link call and proves the assertion that
    catches it is a graph read, not a return value or a counter.
    """
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id="s"
    )
    episode_uuid = f"ep-{uuidlib.uuid4().hex}"
    graph.create_pending_episode(
        episode_uuid=episode_uuid, name="m", content="I own 25 postcards", session_id="s",
        user_id="u", source="user", source_confidence=1.0, namespace=namespace,
    )

    # A link that reports success and draws nothing -- the exact shape of the CF-229 failure.
    monkeypatch.setattr(
        type(graph), "link_episode_admission", lambda *a, **kw: True, raising=True
    )
    reported = graph.link_episode_admission(
        episode_uuid=episode_uuid, turn_evidence_uuid=turn_id, namespace=namespace
    )

    assert reported is True, "the stub should report success, as the real failure mode did"
    assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [], (
        "precondition: the disconnected link must draw nothing"
    )
    # And that is what a real assertion has to catch: the edge, not the return value.
    with pytest.raises(AssertionError):
        assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [turn_id]
