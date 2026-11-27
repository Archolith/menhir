"""#905, #906, #878: what a twin link authorizes, and the post-publish steps it leads to (offline).

#905: the publish records the extraction outcome with the link. A link to an empty twin finalizes
READY only for a completing outcome; a collapse (or a pre-#905 link with no outcome) is classified
by the scheduler like any failure, and finalize-from-link clears the link and its empty twin under
the claim fence and re-extracts.

#906: finalize-from-link rebuilds the artifact from the stored names, contents and facts, so the
post-publish steps (self-tier link, edge-fact repair, correlation) run on real text.

#878: the edge-fact repair is best-effort, counts what it wrote, and stamps provenance on an
all-original episode without an LLM.

Every test here fails on 4794b41f. The Neo4j counterexamples are in
tests/test_905_906_publish_ready_online.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.graphiti_extraction_policy import (
    CombinedExtractionReceipt,
    _extraction_receipt,
    clear_extraction_receipt,
)
from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX
from menhir.infrastructure.publish_fence import ClaimFence, FenceLost, GateFence, MenhirPublishFence
from menhir.services import enrichment_steps
from menhir.services.enrichment_steps import (
    CombinedExtractionCollapsedError,
    EnrichmentContext,
    finalize_from_link,
    stamp_and_finalize,
)
from menhir.services.scheduler_tasks import retry_process_candidate

pytestmark = pytest.mark.unit

Q = "q-905"
TWIN = "twin-905"
COLLAPSE_ERROR = (
    "combined_extraction_collapsed episode_id=q-905 retryable=true raw_entities=2 raw_edges=1"
)
RELATIONLESS_ERROR = "relationless_extraction episode_id=q-905 retryable=false raw_entities=1"


# --------------------------------------------------------------------------- helpers
class _RowsNeo4j:
    """Returns ``rows`` for every query and records the calls."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list[dict]:
        self.calls.append((query, dict(params or {})))
        return [dict(r) for r in self.rows]


def _artifact_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "resolved_episode_uuid": TWIN,
        "entity_uuids": [],
        "edge_uuids": [],
        "entities": [],
        "edges": [],
        "extraction_outcome": "collapsed",
        "policy_empty_reason": None,
        "self_bound": False,
        "completed": False,
        "namespace": "default",
        "fence_namespace_key": "default",
    }
    row.update(overrides)
    return row


def _repo_artifact(**overrides: Any) -> dict[str, Any] | None:
    """The real repository mapping over a stubbed row (the Cypher decides ``completed``)."""
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _RowsNeo4j([_artifact_row(**overrides)])
    return repo.linked_twin_artifact(Q)


def _adapter(artifact: dict[str, Any] | None) -> MagicMock:
    adapter = MagicMock()
    adapter.linked_twin_artifact.return_value = artifact
    adapter.clear_uncompleted_twin_link.return_value = True
    adapter.mark_episode_ready.return_value = True
    adapter.mark_episode_failed.return_value = True
    adapter.update_episode_processing.return_value = True
    adapter.stamp_ingest_metadata.return_value = SimpleNamespace(nodes_touched=1, edges_touched=1)
    adapter.update_edge_facts.return_value = 1
    adapter.list_structure_projects.return_value = []
    return adapter


def _ctx(adapter: Any, claimed: dict[str, object] | None = None, **overrides: Any) -> EnrichmentContext:
    values: dict[str, Any] = dict(
        episode_uuid=Q,
        claimed={"resolved_episode_uuid": TWIN, "content": "Alice manages Bob at Acme", **(claimed or {})},
        started=0.0,
        processing_attempts=1,
        worker_id="w-1",
        graph_adapter=adapter,
        graphiti_client=MagicMock(),
        lifecycle_service=None,
        llm=None,
        ingest_gate=MagicMock(),
        processing_steps_total=5,
        settings_record_revisions=False,
        ready_warning_ms=10**9,
        graphiti_add_episode_timeout_s=1.0,
        graphiti_episode_max_estimated_tokens=0,
        get_queue_depth=lambda: 0,
    )
    values.update(overrides)
    return EnrichmentContext(**values)


def _claim() -> ClaimFence:
    return ClaimFence(
        q_uuid=Q, worker_id="w-1", claim_started_at="2026-01-01T00:00:00Z",
        namespace_key="default", reserved_twin_uuid="reserved-2",
    )


def _collapsed_receipt() -> CombinedExtractionReceipt:
    return CombinedExtractionReceipt(episode_key=Q, raw_entity_count=2, raw_edge_count=1)


class _Ingest:
    def __init__(self) -> None:
        self.requeued: list[str] = []

    def get_queue_depth(self) -> int:
        return 0

    def get_context_window_retry_attempts(self) -> int:
        return 6

    def get_llm_session_window_seconds(self) -> int:
        return 900

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        self.requeued.append(episode_uuid)
        return True


def _failed_row(
    error: str, attempts: int = 1, *, transient_retries: int = 0,
    completed_at: str = "2000-01-01T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "uuid": Q,
        "processing_attempts": attempts,
        "processing_error": error,
        "processing_completed_at": completed_at,
        "transient_retries": transient_retries,
    }


def _retry(adapter: Any, error: str, attempts: int = 1, **row: Any) -> tuple[str, _Ingest]:
    ingest = _Ingest()
    with patch("menhir.services.scheduler_tasks.record_failure_event"):
        action = asyncio.run(retry_process_candidate(
            adapter, ingest, _failed_row(error, attempts, **row), 3, datetime.now(timezone.utc)
        ))
    return action, ingest


class _FakeTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any]) -> list[dict]:
        self.calls.append((query, dict(params)))
        return [{"linked": 1, "written": 0}]


def _link_params(fence: MenhirPublishFence, nodes: list[str], edges: list[str]) -> dict[str, Any]:
    tx = _FakeTx()
    asyncio.run(fence.after_write(
        tx, episode_uuid="reserved-2", entity_node_uuids=nodes, entity_edge_uuids=edges,
        previous_episode_uuids=[],
    ))
    return next(p for q, p in tx.calls if "q.resolved_episode_uuid = $episode_uuid" in q)


# --------------------------------------------------------------------------- #905: the publish
def _receipt_fence(receipt: CombinedExtractionReceipt) -> MenhirPublishFence:
    return MenhirPublishFence(
        _claim(), GateFence(epoch=0),
        extraction_outcome_provider=lambda nodes, edges: enrichment_steps.link_extraction_outcome(
            receipt, Q, persisted=bool(nodes or edges)
        ),
    )


def test_publish_records_a_collapse_with_the_link() -> None:
    params = _link_params(_receipt_fence(_collapsed_receipt()), [], [])
    assert params["outcome"] == "collapsed"
    assert params["policy_empty_reason"] is None


def test_publish_records_extracted_empty_and_policy_empty_outcomes() -> None:
    assert _link_params(_receipt_fence(_collapsed_receipt()), ["n1"], [])["outcome"] == "extracted"
    empty = CombinedExtractionReceipt(episode_key=Q)
    assert _link_params(_receipt_fence(empty), [], [])["outcome"] == "empty"
    policy = CombinedExtractionReceipt(
        episode_key=Q, raw_entity_count=1, assistant_self_only_relationless=True
    )
    params = _link_params(_receipt_fence(policy), [], [])
    assert (params["outcome"], params["policy_empty_reason"]) == ("policy_empty", "assistant_self_only")


def test_publish_without_a_provider_records_no_outcome_for_an_empty_twin() -> None:
    fence = MenhirPublishFence(_claim(), GateFence(epoch=0))
    assert _link_params(fence, [], [])["outcome"] is None
    assert _link_params(fence, [], ["e1"])["outcome"] == "extracted"


def test_with_extras_keeps_the_outcome_provider() -> None:
    fence = _receipt_fence(_collapsed_receipt())
    assert fence.with_extras().extraction_outcome_provider is fence.extraction_outcome_provider


def test_outcome_agrees_with_stamp_and_finalize_on_a_collapse() -> None:
    """The receipt that makes stamp_and_finalize raise is the one the link records as collapsed."""
    receipt = _collapsed_receipt()
    outcome = enrichment_steps.link_extraction_outcome(receipt, Q, persisted=False)
    token = _extraction_receipt.set(receipt)
    try:
        adapter = _adapter(None)
        result = SimpleNamespace(episode=SimpleNamespace(uuid=TWIN), nodes=[], edges=[], episodic_edges=[])
        with pytest.raises(CombinedExtractionCollapsedError):
            asyncio.run(stamp_and_finalize(_ctx(adapter, {"resolved_episode_uuid": None}), result))
    finally:
        _extraction_receipt.reset(token)
        clear_extraction_receipt()
    assert outcome["outcome"] == "collapsed"
    adapter.mark_episode_ready.assert_not_called()


# --------------------------------------------------------------------------- #905: the retry job
def test_collapse_then_scheduler_retry_requeues_instead_of_reconciling() -> None:
    adapter = _adapter(_repo_artifact())

    action, ingest = _retry(adapter, COLLAPSE_ERROR)

    assert action == "requeued"
    assert ingest.requeued == [Q]
    adapter.mark_episode_ready.assert_not_called()
    adapter.stamp_ingest_metadata.assert_not_called()


def test_relationless_collapse_stays_a_visible_terminal_failure() -> None:
    adapter = _adapter(_repo_artifact())

    action, ingest = _retry(adapter, RELATIONLESS_ERROR)

    assert action == "terminal"
    assert ingest.requeued == []
    adapter.mark_episode_ready.assert_not_called()


def test_legacy_empty_link_without_an_outcome_is_not_reconciled() -> None:
    adapter = _adapter(_repo_artifact(extraction_outcome=None, completed=False))
    assert _retry(adapter, COLLAPSE_ERROR)[0] == "requeued"
    adapter.mark_episode_ready.assert_not_called()


def test_completed_link_with_a_terminal_error_and_attempts_left_is_requeued() -> None:
    """(d) The original error no longer applies once the publish is done."""
    adapter = _adapter(_repo_artifact(
        extraction_outcome="policy_empty", policy_empty_reason="claimless_question", completed=True,
    ))
    action, ingest = _retry(adapter, "Failed to extract JSON from LLM response")  # a terminal error
    assert action == "requeued"
    assert ingest.requeued == [Q]
    adapter.mark_episode_ready.assert_not_called()
    adapter.stamp_ingest_metadata.assert_not_called()
    adapter.record_retention_sources.assert_not_called()


TRANSIENT_ERROR = "Failed to load model"


def _completed_adapter() -> MagicMock:
    return _adapter(_repo_artifact(extraction_outcome="extracted", completed=True))


def test_completed_link_at_the_transient_cap_is_not_requeued() -> None:
    """(a) A from-link failure graded transient refunds its attempt; the transient cap stops it."""
    from menhir.infrastructure.episode_lifecycle import TRANSIENT_RETRY_CAP

    action, ingest = _retry(
        _completed_adapter(), TRANSIENT_ERROR, transient_retries=TRANSIENT_RETRY_CAP
    )
    assert action == "exhausted"
    assert ingest.requeued == []


def test_completed_link_inside_the_backoff_window_waits() -> None:
    """(b) The failed-retry backoff applies to a completed link too."""
    action, ingest = _retry(
        _completed_adapter(), TRANSIENT_ERROR,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    assert action == "waiting"
    assert ingest.requeued == []


def test_completed_link_without_an_attempt_left_is_parked_as_exhausted() -> None:
    """(c) With a terminal original error too: exhausted, so tier 1 no longer re-admits it."""
    from menhir.services.scheduler_tasks import retry_failed_enrichments

    adapter = _completed_adapter()
    adapter.fetch_failed_episode_retry_candidates.return_value = [
        _failed_row("Failed to extract JSON from LLM response", attempts=3)
    ]
    ingest = _Ingest()
    ingest.get_max_enrichment_attempts = lambda: 3
    with patch("menhir.services.scheduler_tasks.record_failure_event"):
        result = asyncio.run(retry_failed_enrichments(ingest, adapter, failed_retry_limit=5))
    assert result["exhausted"] == 1
    assert ingest.requeued == []
    assert adapter.park_failed_episode_retry.call_args.kwargs["reason"] == "exhausted"
    adapter.mark_episode_ready.assert_not_called()


def test_retry_tier_one_uses_the_completion_predicate_of_linked_twin_artifact() -> None:
    from menhir.infrastructure import episode_lifecycle as el

    repo = EpisodeLifecycleRepository()
    repo.neo4j = _RowsNeo4j([])
    repo.fetch_failed_episode_retry_candidates(limit=5)
    repo.linked_twin_artifact(Q)
    candidate_query, link_query = repo.neo4j.calls[0][0], repo.neo4j.calls[1][0]

    tier = candidate_query.split("THEN 0", 1)[1].split("THEN 1", 1)[0]
    assert el._TWIN_LINK_COMPLETE in tier
    assert f"{el._TWIN_LINK_COMPLETE} AS completed" in link_query
    for outcome in ("'extracted'", "'empty'", "'policy_empty'"):
        assert outcome in el._TWIN_LINK_COMPLETE
    assert "'collapsed'" not in el._TWIN_LINK_COMPLETE
    assert "(g)-[:MENTIONS]->(:Entity)" in el._TWIN_LINK_COMPLETE


def test_tier_one_excludes_an_exhausted_park_but_not_a_terminal_one() -> None:
    """Tier 1 (completed link) carries the exhausted-park guard; the recheck branch is unchanged.

    A terminal park satisfies the guard, so it stays tier 1; an exhausted park fails it and falls
    to tier 2 or 3. The real query is exercised in test_925_failed_retry_parking_live.py.
    """
    from menhir.infrastructure import episode_lifecycle as el

    tier = el._RETRY_TIER
    branch_1 = tier.split("THEN 0", 1)[1].split("THEN 1", 1)[0]
    assert el._HAS_COMPLETED_TWIN_LINK in branch_1
    guard = "coalesce(properties(n)['retry_parked_reason'], '') <> 'exhausted'"
    assert f"{el._HAS_COMPLETED_TWIN_LINK} AND {guard}" in branch_1
    assert "'terminal'" not in branch_1, "only an exhausted park loses tier 1"
    # Tier 2 is unchanged: the recheck branch still follows tier 1.
    assert tier.index("THEN 1") < tier.index("duration({seconds: $park_recheck_s})") < tier.index("THEN 2")


# --------------------------------------------------------------------------- #905: forced retry
def test_collapse_then_forced_retry_clears_the_link_and_re_extracts() -> None:
    adapter = _adapter(_repo_artifact())

    handled = asyncio.run(finalize_from_link(_ctx(adapter)))

    assert handled is False, "the claim must go on to re-extract"
    adapter.clear_uncompleted_twin_link.assert_called_once_with(Q, TWIN)
    adapter.mark_episode_ready.assert_not_called()
    adapter.stamp_ingest_metadata.assert_not_called()


def test_forced_retry_clears_the_link_inside_the_claim_fence() -> None:
    adapter = _adapter(_repo_artifact())
    seen: list[Any] = []

    def fake_fenced_call(neo4j: Any, claim: Any, gate: Any, call: Any) -> Any:
        seen.append((claim, gate))
        return call("tx-executor")

    with patch.object(enrichment_steps, "run_fenced_call", fake_fenced_call),             patch.object(enrichment_steps, "read_gate_fence", lambda *_: GateFence(epoch=4)):
        handled = asyncio.run(finalize_from_link(_ctx(adapter, claim_fence=_claim())))

    assert handled is False
    assert seen == [(_claim(), GateFence(epoch=4))], "the twin delete must run gated"
    assert adapter.clear_uncompleted_twin_link.call_args.kwargs == {"executor": "tx-executor"}


def _erase_in_flight_neo4j() -> SimpleNamespace:
    """The real read_gate_fence / run_fenced_call over a namespace whose erase holds erasing_ops."""

    def tx_execute(query: str, params: dict[str, Any]) -> list[dict]:
        if "AS erasing_ops" not in query:
            return []  # the lock statement
        return [{
            "q_found": True, "state": "ENRICHING", "owner": "w-1", "instant_ok": True,
            "no_marker": True, "epoch": 4, "erasing_ops": 1,
            "resolved": TWIN, "reserved": ["reserved-2"],
        }]

    tx = SimpleNamespace(execute=tx_execute)
    return SimpleNamespace(
        execute=lambda query, params=None, **_: [{"epoch": 4}],  # GATE_CYPHER
        execute_write=lambda work: work(tx),
    )


def test_forced_retry_does_not_delete_the_twin_while_an_erase_is_in_flight() -> None:
    adapter = _adapter(_repo_artifact())
    adapter.neo4j = _erase_in_flight_neo4j()

    with pytest.raises(FenceLost) as lost:
        asyncio.run(finalize_from_link(_ctx(adapter, claim_fence=_claim())))

    assert lost.value.reason == "erasing_ops"
    adapter.clear_uncompleted_twin_link.assert_not_called()


def test_forced_retry_stops_when_the_link_changed_before_the_clear() -> None:
    adapter = _adapter(_repo_artifact())
    adapter.clear_uncompleted_twin_link.return_value = False

    assert asyncio.run(finalize_from_link(_ctx(adapter))) is True
    adapter.mark_episode_ready.assert_not_called()


def test_clear_query_only_drops_an_uncompleted_link_and_deletes_an_uncited_twin() -> None:
    from menhir.infrastructure import episode_lifecycle as el

    repo = EpisodeLifecycleRepository()
    repo.neo4j = _RowsNeo4j([{"cleared": 1, "twins_deleted": 1}])
    assert repo.clear_uncompleted_twin_link(Q, TWIN) is True
    query, params = repo.neo4j.calls[0]
    assert params == {"episode_uuid": Q, "resolved": TWIN}
    assert "n.resolved_episode_uuid = $resolved" in query
    assert f"NOT {el._TWIN_LINK_COMPLETE}" in query
    assert "REMOVE n.resolved_episode_uuid, n.resolved_extraction_outcome" in query
    assert "DETACH DELETE" not in query
    assert "FOREACH (r IN read_context | DELETE r)" in query
    assert "| DELETE x)" in query
    assert "WHERE NOT (type(x) = 'READ_CONTEXT' AND startNode(x) = d)" in query
    assert "NOT EXISTS { MATCH (d)-[:MENTIONS]->(:Entity) }" in query
    assert "d.uuid IN coalesce(r.episodes, [])" in query


# --------------------------------------------------------------------------- #906
def _steps_artifact(**overrides: Any) -> dict[str, Any] | None:
    values: dict[str, Any] = dict(
        entity_uuids=["ent-1", "ent-2"],
        edge_uuids=["edge-syn", "edge-done"],
        entities=[
            {"uuid": "ent-1", "name": "Alice", "content": None},
            {"uuid": "ent-2", "name": "Acme", "content": None},
        ],
        edges=[
            {"uuid": "edge-syn", "name": "Alice -> Acme", "fact": f"{SYNTHETIC_FACT_PREFIX}alice acme",
             "fact_source": None, "source_node_uuid": "ent-1", "target_node_uuid": "ent-2"},
            {"uuid": "edge-done", "name": "WORKS_AT", "fact": "Alice works at Acme",
             "fact_source": "llm_repaired", "source_node_uuid": "ent-1", "target_node_uuid": "ent-2"},
        ],
        extraction_outcome="extracted",
        completed=True,
    )
    values.update(overrides)
    return _repo_artifact(**values)


class _RepairLLM:
    def __init__(self, answer: str = "Alice manages work at Acme") -> None:
        self.answer = answer
        self.calls: list[tuple[str, list]] = []

    async def repair_edge_facts(self, content: str, stubs: list) -> list:
        self.calls.append((content, stubs))
        return [self.answer for _ in stubs]


def _correlation_spy() -> tuple[MagicMock, list[tuple[str, str]]]:
    queries: list[tuple[str, str]] = []

    async def check_correlation(node_uuid: str, query: str, namespace: str) -> Any:
        queries.append((node_uuid, query))
        return SimpleNamespace(results=[], related=0, merged=0, conflicts=0)

    service = MagicMock()
    service.return_value.check_correlation = check_correlation
    return service, queries


def test_epoch_move_between_publish_and_ready_then_from_link_runs_the_steps_on_real_text() -> None:
    llm = _RepairLLM()
    service, queries = _correlation_spy()
    adapter = _adapter(_steps_artifact())
    checks = {"n": 0}

    def epoch_moves_after_the_stamp(neo4j: Any, claim: Any, gate: Any) -> None:
        checks["n"] += 1
        if checks["n"] == 2:  # the check before the edge-fact repair, after metadata stamping
            raise FenceLost("epoch_moved")

    def fenced_call(neo4j: Any, claim: Any, gate: Any, call: Any) -> Any:
        return call(None)

    first = SimpleNamespace(
        episode=SimpleNamespace(uuid=TWIN),
        nodes=[SimpleNamespace(uuid="ent-1", name="Alice"), SimpleNamespace(uuid="ent-2", name="Acme")],
        edges=[SimpleNamespace(uuid="edge-syn", name="Alice -> Acme", fact=f"{SYNTHETIC_FACT_PREFIX}alice acme")],
        episodic_edges=[],
    )
    with patch.object(enrichment_steps, "check_fence", epoch_moves_after_the_stamp), \
            patch.object(enrichment_steps, "run_fenced_call", fenced_call), \
            patch.object(enrichment_steps, "read_gate_fence", lambda *_: GateFence(epoch=1)), \
            patch("menhir.services.correlation_service.CorrelationService", service), \
            patch.object(enrichment_steps, "record_mcp_event"), \
            patch.object(enrichment_steps, "record_lifecycle_event"):
        clear_extraction_receipt()
        ctx = _ctx(adapter, {"resolved_episode_uuid": None}, llm=llm, claim_fence=_claim(),
                   gate_fence=GateFence(epoch=0))
        with pytest.raises(FenceLost):
            asyncio.run(stamp_and_finalize(ctx, first))
        assert llm.calls == [] and queries == [], "the steps were skipped by the epoch move"

        # The requeued claim carries the link: from-link must run the same steps on stored text.
        handled = asyncio.run(finalize_from_link(_ctx(adapter, llm=llm, claim_fence=_claim())))

    assert handled is True
    assert sorted(q for _u, q in queries) == ["Acme", "Alice"]
    assert len(llm.calls) == 1
    content, stubs = llm.calls[0]
    assert "Alice manages Bob" in content
    assert stubs == [{"source": "Alice", "target": "Acme", "relation": "Alice -> Acme"}]
    updates = adapter.update_edge_facts.call_args.args[0]
    assert updates == [{"uuid": "edge-syn", "fact": "Alice manages work at Acme", "fact_source": "llm_repaired"}]
    assert adapter.mark_episode_ready.call_count == 1


def test_from_link_does_not_relabel_an_edge_an_earlier_attempt_repaired() -> None:
    adapter = _adapter(_steps_artifact(edges=[
        {"uuid": "edge-done", "name": "WORKS_AT", "fact": "Alice works at Acme",
         "fact_source": "llm_repaired", "source_node_uuid": "ent-1", "target_node_uuid": "ent-2"},
    ], edge_uuids=["edge-done"]))
    service, _queries = _correlation_spy()
    with patch("menhir.services.correlation_service.CorrelationService", service), \
            patch.object(enrichment_steps, "record_mcp_event"):
        assert asyncio.run(finalize_from_link(_ctx(adapter, llm=_RepairLLM()))) is True
    adapter.update_edge_facts.assert_not_called()


def test_from_link_links_self_tiers_when_the_publish_bound_a_self() -> None:
    adapter = _adapter(_steps_artifact(self_bound=True))
    service, _queries = _correlation_spy()
    with patch("menhir.services.correlation_service.CorrelationService", service), \
            patch.object(enrichment_steps, "record_mcp_event"):
        asyncio.run(finalize_from_link(_ctx(adapter, inferred_self_mode="observe")))
    adapter.link_self_tiers.assert_called_once()


def test_from_link_of_a_policy_empty_link_keeps_its_label() -> None:
    adapter = _adapter(_repo_artifact(
        extraction_outcome="policy_empty", policy_empty_reason="claimless_question", completed=True,
    ))
    with patch.object(enrichment_steps, "record_mcp_event"):
        assert asyncio.run(finalize_from_link(_ctx(adapter))) is True
    assert adapter.mark_episode_ready.call_args.kwargs["policy_empty_reason"] == "claimless_question"


# --------------------------------------------------------------------------- #878
def _extracted(edges: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        episode=SimpleNamespace(uuid=TWIN), nodes=[SimpleNamespace(uuid="n1", name="", content="")],
        edges=edges, episodic_edges=[],
    )


def test_edge_fact_write_error_does_not_fail_the_episode() -> None:
    clear_extraction_receipt()
    adapter = _adapter(None)
    adapter.update_edge_facts.side_effect = RuntimeError("neo4j deadlock")
    edge = SimpleNamespace(uuid="e1", name="a -> b", fact=f"{SYNTHETIC_FACT_PREFIX}a b")
    with patch.object(enrichment_steps, "record_mcp_event"):
        asyncio.run(stamp_and_finalize(
            _ctx(adapter, {"resolved_episode_uuid": None}, llm=_RepairLLM()), _extracted([edge])
        ))
    adapter.update_edge_facts.assert_called_once()
    assert adapter.mark_episode_ready.call_count == 1


def test_fence_loss_before_the_repair_still_reaches_the_worker() -> None:
    clear_extraction_receipt()
    adapter = _adapter(None)
    checks = {"n": 0}

    def lose_at_repair(neo4j: Any, claim: Any, gate: Any) -> None:
        checks["n"] += 1
        if checks["n"] == 2:
            raise FenceLost("epoch_moved")

    with patch.object(enrichment_steps, "check_fence", lose_at_repair), \
            patch.object(enrichment_steps, "run_fenced_call", lambda n, c, g, call: call(None)):
        with pytest.raises(FenceLost):
            asyncio.run(stamp_and_finalize(
                _ctx(adapter, {"resolved_episode_uuid": None}, claim_fence=_claim(),
                     gate_fence=GateFence(epoch=0)),
                _extracted([SimpleNamespace(uuid="e1", name="r", fact="ok")]),
            ))
    adapter.update_edge_facts.assert_not_called()


def test_rejected_model_output_is_counted_as_a_fallback(caplog: pytest.LogCaptureFixture) -> None:
    adapter = _adapter(None)
    edge = SimpleNamespace(uuid="e1", name="a -> b", fact=f"{SYNTHETIC_FACT_PREFIX}a b")
    with caplog.at_level(logging.INFO, logger=enrichment_steps.logger.name):
        asyncio.run(enrichment_steps._repair_synthetic_edge_facts(
            _ctx(adapter, llm=_RepairLLM("two\nlines")), [edge], "a b"
        ))
    assert adapter.update_edge_facts.call_args.args[0][0]["fact_source"] == "synthetic_fallback"
    assert "1 synthetic, 0 repaired, 1 fallback" in caplog.text


def test_all_original_edges_get_provenance_without_an_llm() -> None:
    adapter = _adapter(None)
    asyncio.run(enrichment_steps._repair_synthetic_edge_facts(
        _ctx(adapter, llm=None), [SimpleNamespace(uuid="e1", name="r", fact="Alice knows Bob")], "x"
    ))
    adapter.update_edge_facts.assert_called_once_with(
        [{"uuid": "e1", "fact": "Alice knows Bob", "fact_source": "original"}]
    )


# --------------------------------------------------------------------------- #906: scheduler path
@pytest.mark.asyncio
async def test_finished_link_failed_row_reaches_ready_only_through_the_worker(
    stub_memory_graph_adapter: Any, stub_graphiti_client: Any, stub_llm_adapter: Any,
) -> None:
    from menhir.services import IngestService

    adapter = stub_memory_graph_adapter
    adapter._correlation = MagicMock()  # the stub has no correlation repository; the service is spied
    q = adapter.create_pending_episode(
        episode_uuid="q-906", name="ep-906", content="Alice manages Bob at Acme",
        session_id="s-1", user_id="u-1", source="unit-test", source_confidence=0.5,
    )
    row = adapter.pending_episode_rows[q]
    row["processing_state"] = "FAILED"
    row["processing_error"] = "Failed to extract JSON from LLM response"
    row["resolved_episode_uuid"] = TWIN
    artifact = _steps_artifact(edges=[_steps_artifact()["edge_details"]["edge-syn"]],
                               edge_uuids=["edge-syn"])
    adapter.completed_episode_artifacts[q] = artifact

    # 1. The scheduler: requeue only. Nothing is stamped or marked READY here.
    ingest = _Ingest()
    with patch("menhir.services.scheduler_tasks.record_failure_event"):
        action = await retry_process_candidate(
            adapter, ingest, {**_failed_row(row["processing_error"]), "uuid": q}, 3,
            datetime.now(timezone.utc),
        )
    assert action == "requeued" and ingest.requeued == [q]
    assert row["processing_state"] == "FAILED"
    assert not getattr(adapter, "stamp_calls", [])

    # 2. The worker's claim returns the link; finalize-from-link runs every post-publish step.
    async def _no_extraction(**_: object) -> object:
        raise AssertionError("a finished link must not be extracted again")

    stub_graphiti_client.add_episode = _no_extraction
    llm = _RepairLLM()
    service, queries = _correlation_spy()
    ingest_service = IngestService(
        graphiti_client=stub_graphiti_client, graph_adapter=adapter, llm=stub_llm_adapter,
    )
    with patch("menhir.services.correlation_service.CorrelationService", service), \
            patch.object(type(stub_llm_adapter), "repair_edge_facts", llm.repair_edge_facts, create=True):
        await ingest_service._process_pending_episode(q)

    assert row["processing_state"] == "READY"
    assert sorted(query for _u, query in queries) == ["Acme", "Alice"]
    assert len(llm.calls) == 1
    assert adapter.edge_fact_updates == [
        {"uuid": "edge-syn", "fact": "Alice manages work at Acme", "fact_source": "llm_repaired"}
    ]


def test_from_link_with_a_missing_retention_source_raises_and_does_not_mark_ready() -> None:
    """_record_retention_fenced re-raises a tenancy ValueError while the fence still holds."""
    adapter = _adapter(_steps_artifact())
    adapter.record_retention_sources.side_effect = ValueError(
        "Retention provenance missing for 1 extracted entities"
    )
    service, _queries = _correlation_spy()
    with patch("menhir.services.correlation_service.CorrelationService", service),             patch.object(enrichment_steps, "record_mcp_event"):
        with pytest.raises(ValueError, match="Retention provenance missing"):
            asyncio.run(finalize_from_link(_ctx(adapter)))
    adapter.record_retention_sources.assert_called_once()
    adapter.mark_episode_ready.assert_not_called()
