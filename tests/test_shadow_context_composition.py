"""Unit tests for Stage 1 shadow-mode context composition
(.agent/plans/menhir-context-composition-production-integration.md).

Pure-logic tests against fake llm/graph_adapter/graphiti_client -- no live Neo4j, no real
LLM. Every status-vocabulary path is tested explicitly (not just the happy path): this
stage's entire purpose is to keep failures visible, so a test suite that only covers
"selected" would miss the point.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.services.shadow_context_composition import (
    ShadowCandidateFact,
    ShadowCandidateLabels,
    ShadowRankedHypothesis,
    build_shadow_trace,
    compose_shadow_prediction,
    run_shadow_composition_with_timeout,
    shadow_trace_to_details,
    snapshot_candidate_facts,
)

_REF = "2026-07-16T12:00:00+00:00"


def _fact(
    fact_uuid="f1", fact_text="Rachel moved to Chicago", source_name="Rachel",
    target_name="Chicago", valid_at=None, invalid_at=None, created_at=None, expired_at=None,
) -> ShadowCandidateFact:
    return ShadowCandidateFact(
        fact_uuid=fact_uuid, fact_text=fact_text,
        source_uuid="s-" + fact_uuid, source_name=source_name,
        target_uuid="t-" + fact_uuid, target_name=target_name,
        valid_at=valid_at, invalid_at=invalid_at, created_at=created_at, expired_at=expired_at,
        retrieval_sources=frozenset({"literal_name"}),
    )


class _StubLLM:
    """Configurable stub implementing the two LLM methods compose_shadow_prediction /
    _select_eligible_candidate call. classify_response / tie_response may be a raw string,
    None (call failed), or an Exception instance (raises)."""

    def __init__(self, classify_response=None, tie_response=None):
        self.classify_response = classify_response
        self.tie_response = tie_response
        self.classify_calls = 0
        self.tie_calls = 0

    async def classify_shadow_context(self, episode_body, candidates):
        self.classify_calls += 1
        if isinstance(self.classify_response, Exception):
            raise self.classify_response
        return self.classify_response

    async def break_shadow_tie(self, episode_body, tied_candidates):
        self.tie_calls += 1
        if isinstance(self.tie_response, Exception):
            raise self.tie_response
        return self.tie_response


def _hyp_response(facet="Housing", family="Current residence", confidence=0.9, labels=None):
    return json.dumps({
        "message_hypotheses": [
            {"shadow_facet": facet, "shadow_state_family": family, "confidence": confidence},
        ],
        "candidate_labels": labels or [],
    })


# ---------------------------------------------------------------------------
# snapshot_candidate_facts
# ---------------------------------------------------------------------------

class TestSnapshotCandidateFacts:
    @pytest.mark.asyncio
    async def test_empty_namespace_returns_empty_no_error(self):
        candidates, error = await snapshot_candidate_facts(
            object(), object(), namespace="", episode_body="hello",
        )
        assert candidates == []
        assert error is None

    @pytest.mark.asyncio
    async def test_empty_episode_body_returns_empty_no_error(self):
        candidates, error = await snapshot_candidate_facts(
            object(), object(), namespace="ns", episode_body="   ",
        )
        assert candidates == []
        assert error is None

    @pytest.mark.asyncio
    async def test_no_candidate_entities_found_returns_empty_no_error(self):
        class NoDriverClient:
            search_scored = None  # explicit: no semantic search available either

        candidates, error = await snapshot_candidate_facts(
            NoDriverClient(), object(), namespace="ns", episode_body="nothing matches",
        )
        assert candidates == []
        assert error is None

    @pytest.mark.asyncio
    async def test_fact_edge_fetch_failure_is_reported_as_error(self):
        class FakeRecords:
            records = [{"uuid": "e1", "name": "Rachel"}]

        class FakeDriver:
            async def execute_query(self, *_args, **_kwargs):
                return FakeRecords()

        class FakeClients:
            driver = FakeDriver()

        class FakeGraphitiClient:
            client = type("C", (), {"clients": FakeClients()})()
            search_scored = None

        class FailingGraphAdapter:
            def fetch_candidate_fact_edges(self, uuids):
                raise RuntimeError("neo4j down")

        candidates, error = await snapshot_candidate_facts(
            FakeGraphitiClient(), FailingGraphAdapter(),
            namespace="ns", episode_body="Rachel moved",
        )
        assert candidates == []
        assert error is not None
        assert "fact_edge_fetch_failed" in error

    @pytest.mark.asyncio
    async def test_fact_edge_with_only_one_endpoint_matched_does_not_crash(self):
        """Regression test: a fact-edge is very often matched via only ONE endpoint
        (e.g. "Rachel" hit the literal-name lookup, "Chicago" as the other endpoint
        never did). retrieval_sources.get() on the unmatched side must not be unioned
        against a bare None (frozenset | None raises TypeError) -- this is the normal
        case, not an edge case, so it must never crash snapshot_candidate_facts."""
        class FakeRecords:
            records = [{"uuid": "rachel-uuid", "name": "Rachel"}]

        class FakeDriver:
            async def execute_query(self, *_args, **_kwargs):
                return FakeRecords()

        class FakeClients:
            driver = FakeDriver()

        class FakeGraphitiClient:
            client = type("C", (), {"clients": FakeClients()})()
            search_scored = None

        class OneSidedGraphAdapter:
            def fetch_candidate_fact_edges(self, uuids):
                # source (Rachel) was matched via literal-name; target (Chicago) never
                # appears in retrieval_sources at all.
                return [{
                    "fact_uuid": "f1", "fact_text": "Rachel moved to Chicago",
                    "source_uuid": "rachel-uuid", "source_name": "Rachel",
                    "target_uuid": "chicago-uuid", "target_name": "Chicago",
                    "valid_at": None, "invalid_at": None, "created_at": None, "expired_at": None,
                }]

        candidates, error = await snapshot_candidate_facts(
            FakeGraphitiClient(), OneSidedGraphAdapter(),
            namespace="ns", episode_body="Rachel moved to Chicago",
        )
        assert error is None
        assert len(candidates) == 1
        assert candidates[0].fact_uuid == "f1"
        assert "literal_name" in candidates[0].retrieval_sources

    @pytest.mark.asyncio
    async def test_enforce_excludes_unconfirmed_self_fact_from_shadow_context(self):
        class FakeRecords:
            records = [{"uuid": "chicago-uuid", "name": "Chicago"}]

        class FakeDriver:
            async def execute_query(self, *_args, **_kwargs):
                return FakeRecords()

        class FakeClients:
            driver = FakeDriver()

        class FakeGraphitiClient:
            client = type("C", (), {"clients": FakeClients()})()
            search_scored = None

            def canonical_self_payload_is_authorized(
                self, payload, **kwargs
            ):
                return False

        class EnforcingGraphAdapter:
            canonical_self_binding_mode = "enforce"

            def fetch_candidate_fact_edges(self, uuids):
                return [{
                    "fact_uuid": "f-self",
                    "fact_text": "user lives in Chicago",
                    "source_uuid": self_uuid_for_namespace("ns"),
                    "source_name": "user",
                    "target_uuid": "chicago-uuid",
                    "target_name": "Chicago",
                    "edge_source_uuid": self_uuid_for_namespace("ns"),
                    "edge_target_uuid": "chicago-uuid",
                    "edge_source_name": "user",
                    "edge_target_name": "Chicago",
                    "predicate": "LIVES_IN",
                    "self_authority_payload_json": None,
                    "valid_at": None,
                    "invalid_at": None,
                    "created_at": None,
                    "expired_at": None,
                }]

        candidates, error = await snapshot_candidate_facts(
            FakeGraphitiClient(),
            EnforcingGraphAdapter(),
            namespace="ns",
            episode_body="Chicago",
        )
        assert error is None
        assert candidates == []


# ---------------------------------------------------------------------------
# compose_shadow_prediction — status vocabulary
# ---------------------------------------------------------------------------

class TestComposeShadowPredictionStatuses:
    @pytest.mark.asyncio
    async def test_candidate_query_failed_short_circuits(self):
        pred = await compose_shadow_prediction(
            _StubLLM(), episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[], candidate_query_error="boom",
        )
        assert pred.status == "candidate_query_failed"
        assert pred.failure_stage == "candidate_retrieval"
        assert pred.error == "boom"

    @pytest.mark.asyncio
    async def test_no_candidates_abstains(self):
        pred = await compose_shadow_prediction(
            _StubLLM(), episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[], candidate_query_error=None,
        )
        assert pred.status == "abstained_no_candidates"
        assert pred.selected_fact_uuid is None

    @pytest.mark.asyncio
    async def test_llm_call_failure_is_metadata_generation_failed(self):
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response=None), episode_uuid="e1", namespace="ns",
            episode_body="hi", reference_time=_REF, candidates=[_fact()], candidate_query_error=None,
        )
        assert pred.status == "metadata_generation_failed"

    @pytest.mark.asyncio
    async def test_llm_exception_is_metadata_generation_failed(self):
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response=RuntimeError("api down")), episode_uuid="e1",
            namespace="ns", episode_body="hi", reference_time=_REF,
            candidates=[_fact()], candidate_query_error=None,
        )
        assert pred.status == "metadata_generation_failed"
        assert "api down" in (pred.error or "")

    @pytest.mark.asyncio
    async def test_malformed_json_response(self):
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response="not json at all"), episode_uuid="e1",
            namespace="ns", episode_body="hi", reference_time=_REF,
            candidates=[_fact()], candidate_query_error=None,
        )
        assert pred.status == "malformed_llm_response"

    @pytest.mark.asyncio
    async def test_single_eligible_candidate_is_selected(self):
        candidate = _fact(created_at="2026-01-01T00:00:00+00:00")
        response = _hyp_response(labels=[
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ])
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response=response), episode_uuid="e1", namespace="ns",
            episode_body="Rachel moved to Chicago", reference_time=_REF,
            candidates=[candidate], candidate_query_error=None,
        )
        assert pred.status == "selected"
        assert pred.selected_fact_uuid == "f1"
        assert pred.llm_tie_breaker_fired is False
        assert len(pred.message_hypotheses) == 1
        assert pred.message_hypotheses[0].shadow_facet == "Housing"

    @pytest.mark.asyncio
    async def test_no_matching_label_abstains_no_eligible(self):
        candidate = _fact(created_at="2026-01-01T00:00:00+00:00")
        # Label facet doesn't match the message hypothesis's facet.
        response = _hyp_response(facet="Housing", labels=[
            {"fact_uuid": "f1", "shadow_facet": "Mortgage", "shadow_state_family": "Current residence"},
        ])
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response=response), episode_uuid="e1", namespace="ns",
            episode_body="hi", reference_time=_REF,
            candidates=[candidate], candidate_query_error=None,
        )
        assert pred.status == "abstained_no_eligible_candidates"
        assert pred.selected_fact_uuid is None
        assert len(pred.rejected) == 1
        assert pred.rejected[0].reason == "shadow_label_mismatch"

    @pytest.mark.asyncio
    async def test_not_known_at_reference_time_is_rejected(self):
        # expired_at before reference_time -> was_known_at() is False.
        candidate = _fact(
            created_at="2020-01-01T00:00:00+00:00",
            expired_at="2020-06-01T00:00:00+00:00",
        )
        response = _hyp_response(labels=[
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ])
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response=response), episode_uuid="e1", namespace="ns",
            episode_body="hi", reference_time=_REF,
            candidates=[candidate], candidate_query_error=None,
        )
        assert pred.status == "abstained_no_eligible_candidates"
        assert pred.rejected[0].reason == "not_known_at_reference_time"

    @pytest.mark.asyncio
    async def test_two_eligible_candidates_triggers_tie_break_and_selects(self):
        c1 = _fact(fact_uuid="f1", created_at="2026-01-01T00:00:00+00:00")
        c2 = _fact(fact_uuid="f2", created_at="2026-01-01T00:00:00+00:00")
        labels = [
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
            {"fact_uuid": "f2", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ]
        response = _hyp_response(labels=labels)
        llm = _StubLLM(
            classify_response=response,
            tie_response=json.dumps({"selected_fact_uuid": "f2"}),
        )
        pred = await compose_shadow_prediction(
            llm, episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[c1, c2], candidate_query_error=None,
        )
        assert pred.status == "selected"
        assert pred.selected_fact_uuid == "f2"
        assert pred.llm_tie_breaker_fired is True
        assert llm.tie_calls == 1

    @pytest.mark.asyncio
    async def test_tie_break_null_selection_abstains(self):
        """The genuine-tie suite's core finding (Phase 5 item 4): the tie-breaker must
        be ABLE to decline rather than being forced to guess."""
        c1 = _fact(fact_uuid="f1", created_at="2026-01-01T00:00:00+00:00")
        c2 = _fact(fact_uuid="f2", created_at="2026-01-01T00:00:00+00:00")
        labels = [
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
            {"fact_uuid": "f2", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ]
        response = _hyp_response(labels=labels)
        llm = _StubLLM(
            classify_response=response,
            tie_response=json.dumps({"selected_fact_uuid": None}),
        )
        pred = await compose_shadow_prediction(
            llm, episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[c1, c2], candidate_query_error=None,
        )
        assert pred.status == "abstained_tie"
        assert pred.selected_fact_uuid is None
        assert pred.llm_tie_breaker_fired is True

    @pytest.mark.asyncio
    async def test_tie_break_call_failure_abstains_tie_not_crash(self):
        c1 = _fact(fact_uuid="f1", created_at="2026-01-01T00:00:00+00:00")
        c2 = _fact(fact_uuid="f2", created_at="2026-01-01T00:00:00+00:00")
        labels = [
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
            {"fact_uuid": "f2", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ]
        response = _hyp_response(labels=labels)
        llm = _StubLLM(classify_response=response, tie_response=None)
        pred = await compose_shadow_prediction(
            llm, episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[c1, c2], candidate_query_error=None,
        )
        assert pred.status == "abstained_tie"


# ---------------------------------------------------------------------------
# run_shadow_composition_with_timeout
# ---------------------------------------------------------------------------

class TestRunShadowCompositionWithTimeout:
    @pytest.mark.asyncio
    async def test_timeout_produces_timed_out_status(self):
        class SlowLLM(_StubLLM):
            async def classify_shadow_context(self, episode_body, candidates):
                await asyncio.sleep(10)
                return "{}"

        pred = await run_shadow_composition_with_timeout(
            SlowLLM(), episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[_fact()], candidate_query_error=None,
            candidate_retrieval_ms=0, timeout_s=0.05,
        )
        assert pred.status == "timed_out"
        assert pred.failure_stage == "shadow_processing"

    @pytest.mark.asyncio
    async def test_normal_completion_within_timeout(self):
        response = _hyp_response(labels=[
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ])
        candidate = _fact(created_at="2026-01-01T00:00:00+00:00")
        pred = await run_shadow_composition_with_timeout(
            _StubLLM(classify_response=response), episode_uuid="e1", namespace="ns",
            episode_body="hi", reference_time=_REF, candidates=[candidate],
            candidate_query_error=None, candidate_retrieval_ms=5, timeout_s=5.0,
        )
        assert pred.status == "selected"
        assert pred.candidate_retrieval_ms == 5


# ---------------------------------------------------------------------------
# build_shadow_trace / shadow_trace_to_details
# ---------------------------------------------------------------------------

class TestBuildShadowTrace:
    @pytest.mark.asyncio
    async def test_combines_prediction_with_production_result(self):
        pred = await compose_shadow_prediction(
            _StubLLM(), episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[], candidate_query_error=None,
        )

        class FakeNode:
            name = "Rachel"

        class FakeEdge:
            fact = "Rachel moved to Chicago"

        class FakeResult:
            nodes = [FakeNode()]
            edges = [FakeEdge()]

        trace = build_shadow_trace(pred, FakeResult(), shadow_total_ms=42)
        assert trace.production_extracted_node_names == ("Rachel",)
        assert trace.production_extracted_facts == ("Rachel moved to Chicago",)
        assert trace.shadow_total_ms == 42
        assert trace.prediction is pred  # combined, not copied/mutated

    @pytest.mark.asyncio
    async def test_handles_malformed_production_result_gracefully(self):
        pred = await compose_shadow_prediction(
            _StubLLM(), episode_uuid="e1", namespace="ns", episode_body="hi",
            reference_time=_REF, candidates=[], candidate_query_error=None,
        )
        trace = build_shadow_trace(pred, object(), shadow_total_ms=1)
        assert trace.production_extracted_node_names == ()
        assert trace.production_extracted_facts == ()

    @pytest.mark.asyncio
    async def test_details_dict_is_json_serializable(self):
        c1 = _fact(fact_uuid="f1", created_at="2026-01-01T00:00:00+00:00")
        response = _hyp_response(labels=[
            {"fact_uuid": "f1", "shadow_facet": "Housing", "shadow_state_family": "Current residence"},
        ])
        pred = await compose_shadow_prediction(
            _StubLLM(classify_response=response), episode_uuid="e1", namespace="ns",
            episode_body="hi", reference_time=_REF, candidates=[c1], candidate_query_error=None,
        )
        trace = build_shadow_trace(pred, object(), shadow_total_ms=10)
        details = shadow_trace_to_details(trace)
        json.dumps(details)  # must not raise
        assert details["status"] == "selected"
        assert details["selected_fact_uuid"] == "f1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
