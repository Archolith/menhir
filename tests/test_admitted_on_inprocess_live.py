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

import asyncio
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


@pytest.fixture
def ingest(graph):
    """The REAL IngestService, so tests drive `queue_episode_for_enrichment` itself.

    The first version of this file called `create_pending_episode` and `link_episode_admission`
    separately -- testing the two helpers with IDEAL arguments rather than the caller production
    executes. That hid a live defect: the production call site passed no `namespace`, and
    `link_episode_admission` treats None as "do not filter", so an episode in namespace A could
    draw ADMITTED_ON to a TurnEvidence in B. The helper was correct; its caller was not, and a
    test of the helper could never see it.

    Same lesson as CF-79: testing the right function with the right arguments is not enough when
    the argument production supplies IS the defect.
    """
    from unittest.mock import MagicMock

    from menhir.services.ingest_service import IngestService

    svc = IngestService(graphiti_client=MagicMock(), graph_adapter=graph, llm=MagicMock())
    # Enrichment off: this file is about the durable write and its provenance edge, and a real
    # graphiti round trip would need an LLM.
    svc.configure(enrichment_enabled=False)
    return svc


def _queue(ingest, *, text: str, namespace: str, session_id: str, turn_id: str | None,
           source: str = "claude-code") -> str:
    from menhir.domain.session import new_session

    result = asyncio.run(
        ingest.queue_episode_for_enrichment(
            text,
            new_session(user_id="u", session_id=session_id),
            source,
            namespace=namespace,
            turn_evidence_uuid=turn_id,
        )
    )
    return result.episode_id


# ---------------------------------------------------------------------------
# 1. The normal production path -- through queue_episode_for_enrichment
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_the_queue_path_draws_the_edge_without_any_endpoint_call(
    test_neo4j_repo, graph, ingest
) -> None:
    """Capture a turn, queue a memory, require the edge -- with nothing calling
    `/api/episode-admission`.

    Drives `queue_episode_for_enrichment`, the function production runs, rather than the two
    repository helpers it calls.
    """
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    session_id = f"sess-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id=session_id
    )

    episode_uuid = _queue(
        ingest, text="I own 25 postcards", namespace=namespace,
        session_id=session_id, turn_id=turn_id,
    )

    assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [turn_id], (
        "the queue path did not draw ADMITTED_ON -- the memory is unauditable"
    )


@pytest.mark.online
def test_a_user_tier_memory_queued_this_way_is_re_evaluable(
    test_neo4j_repo, graph, ingest
) -> None:
    """The property that actually matters, measured by the CF-17 audit tool itself.

    CF-17's residue question returned 60/60 UNEVALUABLE because this edge was absent. A memory
    written through the real path must be re-checkable against today's gate -- that is what
    "future evaluability" means, and it is the metric worth tracking rather than an edge count.
    """
    from scripts.audit_cf17_apex_residue import fetch_apex_claims, reevaluate

    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    session_id = f"sess-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id=session_id
    )

    episode_uuid = _queue(
        ingest, text="I own 25 postcards", namespace=namespace,
        session_id=session_id, turn_id=turn_id, source="user",
    )

    rows = [r for r in fetch_apex_claims(test_neo4j_repo, 50) if r["uuid"] == episode_uuid]
    assert rows, "the audit did not see the newly queued apex memory"
    bucket, reason = reevaluate(rows[0])
    assert bucket == "still_granted", f"a newly written apex memory is {bucket}: {reason}"


# ---------------------------------------------------------------------------
# 2. Fail-closed identity, through the same path
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_the_queue_path_cannot_pair_across_namespaces(test_neo4j_repo, graph, ingest) -> None:
    """The defect this rewrite exposed, pinned at the layer that had it.

    A NON-user source is the dangerous case: no admission gate runs, so `turn_evidence_uuid` is
    taken at face value. Before the fix this drew `episode in A -[:ADMITTED_ON]-> turn in B`,
    reproduced directly against a real graph. The helper already refused it when given a
    namespace; the caller was passing None, which means "do not filter".
    """
    ns_a = f"ns-a-{uuidlib.uuid4().hex[:6]}"
    ns_b = f"ns-b-{uuidlib.uuid4().hex[:6]}"
    turn_b = _capture_turn(
        test_neo4j_repo, text="B private turn", namespace=ns_b, session_id="sess-b"
    )

    episode_a = _queue(
        ingest, text="A memory", namespace=ns_a, session_id="sess-a", turn_id=turn_b
    )

    assert _admitted_on_edges(test_neo4j_repo, episode_a) == [], (
        "a memory paired with another namespace's captured turn through the queue path"
    )


@pytest.mark.online
def test_the_queue_path_still_pairs_across_sessions_in_one_namespace(
    test_neo4j_repo, graph, ingest
) -> None:
    """The boundary is the NAMESPACE, not the session -- asserted so the test above is not
    satisfied by a rule that refuses everything.

    A memory written in a later session about an earlier turn is legitimate; the admission gate
    treats a session mismatch as a grounding signal, not an identity violation.
    """
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id="sess-early"
    )

    episode_uuid = _queue(
        ingest, text="I own 25 postcards", namespace=namespace,
        session_id="sess-later", turn_id=turn_id,
    )

    assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [turn_id]


# ---------------------------------------------------------------------------
# 3. Vacuity: removing the INLINE link must fail the queue-path test
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_disabling_the_inline_link_fails_the_queue_path_e2e(
    test_neo4j_repo, graph, ingest, monkeypatch
) -> None:
    """Disconnect the link INSIDE the write path and require the queue-path assertion to fail.

    The stub reports success, which is the exact shape CF-229 had: `linked: true`, every test
    green, and zero edges on the live graph. What catches that is a graph read after the real
    call, and this proves the file has one.
    """
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"
    turn_id = _capture_turn(
        test_neo4j_repo, text="I own 25 postcards", namespace=namespace, session_id="s"
    )

    monkeypatch.setattr(
        type(graph), "link_episode_admission", lambda *a, **kw: True, raising=True
    )
    episode_uuid = _queue(
        ingest, text="I own 25 postcards", namespace=namespace, session_id="s", turn_id=turn_id
    )

    assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [], (
        "precondition: the disconnected link must draw nothing"
    )
    with pytest.raises(AssertionError):
        assert _admitted_on_edges(test_neo4j_repo, episode_uuid) == [turn_id]
