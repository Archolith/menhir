"""Oracle wiring regression tests: P1 (#572) similarity/alpha, P2 (#569, CF-310/311) cue matching."""

from __future__ import annotations

import pytest

from menhir.domain.oracle_combiner import FAMILY_ALPHA, LogSpaceOracleCombiner
from menhir.domain.oracles import CandidateMemory, QueryContext
from menhir.domain.query_intent import IntentConfidence, TaskIntent, classify_intent
from menhir.domain.recall import ScoredMemory
from menhir.domain.retrieval_tuning import RetrievalScoreKind, RetrievalTuningConfig
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.domain.scalar_view_authority import QueryIntent
from menhir.domain.scalar_view_suppression import authority_query_intent
from menhir.domain.temporal import TemporalQuery
from menhir.domain.temporal_intent import classify_temporal_intent
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.recall_policies import _oracle_similarity
from menhir.services.recall_service import RecallService
from menhir.services.retrieval_oracles import default_oracles
from menhir.services.scoring_service import ScoringService


# --- P1a: one cosine -> one oracle similarity ---------------------------------

def test_oracle_similarity_source_prior_passthrough_and_rrf_halved() -> None:
    assert _oracle_similarity(0.8, RetrievalScoreKind.SOURCE_PRIOR) == pytest.approx(0.8)
    assert _oracle_similarity(0.8, RetrievalScoreKind.GRAPHITI_RRF) == pytest.approx(0.4)
    # the string form of the kind (as stored on candidate dicts) behaves identically
    assert _oracle_similarity(0.8, RetrievalScoreKind.SOURCE_PRIOR.value) == pytest.approx(0.8)


def _scored(uuid: str, sim: float, kind: RetrievalScoreKind) -> ScoredMemory:
    bd = RelevanceBreakdown(
        semantic_similarity=sim, adjacency_bonus=0.0, recency_bonus=0.0, prominence_bonus=0.0,
        conflict_bonus=0.0, type_boost=1.0, preset="default", alpha=1.0, beta=0.0, gamma=0.0, delta=0.0,
    )
    return ScoredMemory(
        uuid=uuid, name=uuid, content="recall floor behavior", scope="PERSISTENT",
        memory_type="SEMANTIC", final_score=sim, breakdown=bd, retrieval_score=sim,
        retrieval_score_kind=kind,
    )


@pytest.mark.asyncio
async def test_apply_frontier_feeds_equal_similarity_for_equal_cosine_source_prior(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
) -> None:
    """The oracle sees the SAME similarity for an observation-lane and a scalar-history
    candidate (both SOURCE_PRIOR); a GRAPHITI_RRF candidate with the same value is halved."""
    from menhir.services import assertion_pipeline as ap

    seen: dict[str, float] = {}
    orig_run = ap.AssertionPipeline.run

    async def spy(self, qctx, cmems):
        for c in cmems:
            seen[c.id] = c.metadata["similarity"]
        return await orig_run(self, qctx, cmems)

    monkeypatch.setattr(ap.AssertionPipeline, "run", spy)
    service = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
                            scoring_service=ScoringService(), read_only=True)
    scored = [
        _scored("obs", 0.8, RetrievalScoreKind.SOURCE_PRIOR),
        _scored("hist", 0.8, RetrievalScoreKind.SOURCE_PRIOR),
        _scored("rrf", 0.8, RetrievalScoreKind.GRAPHITI_RRF),
    ]
    tuning = RetrievalTuningConfig(enable_assertion_shadow=False, enable_oracle_ranking=True)
    await service._apply_frontier("recall floor behavior", "tenant", scored, {}, tuning)
    assert seen["obs"] == pytest.approx(seen["hist"])
    assert seen["obs"] == pytest.approx(0.8)
    assert seen["rrf"] == pytest.approx(0.4)


# --- P1b: intent alpha and the exact-hit counterexample -----------------------

def test_intent_family_alpha_is_below_semantic() -> None:
    assert FAMILY_ALPHA["intent"] == 0.4
    assert FAMILY_ALPHA["intent"] < FAMILY_ALPHA["semantic"]


@pytest.mark.asyncio
async def test_exact_semantic_hit_outranks_offtopic_test_evidence_candidate() -> None:
    q = QueryContext(text="which test covers the recall floor", intent="current")
    cands = [
        CandidateMemory(id="offtopic", content="unrelated lunch menu",
                        metadata={"similarity": 0.1, "evidence_kinds": ("test",)}),
        CandidateMemory(id="exact", content="the recall floor is a rank cut",
                        metadata={"similarity": 0.95}),
    ]
    # sanity: the query really carries an evidence-lookup intent that favours tests
    hits, conf = classify_intent(q.text)
    assert conf is IntentConfidence.HIGH and hits[0].intent is TaskIntent.EVIDENCE_LOOKUP
    grouped = await OracleExecutor(default_oracles()).evaluate(q, cands)
    intent_of = {cid: [r for r in rs if r.source_family == "intent"][0] for cid, rs in grouped.items()}
    # the counterexample premise: intent really does favour the off-topic candidate
    assert intent_of["offtopic"].probability > intent_of["exact"].probability
    ranked, _ = LogSpaceOracleCombiner().rank(q, grouped)
    assert ranked[0] == "exact"


# --- P2: whole-word cues (CF-310/311) -----------------------------------------

_NO_CUE = [
    "which api used the old auth token",
    "which cli installed the hook",
    "distill the notes",
    "what do you know about the deploy",
    "recurrent job schedule",
    "transformer config",
]


@pytest.mark.parametrize("text", _NO_CUE)
def test_substring_lookalikes_do_not_trigger_history_or_current(text: str) -> None:
    hits, conf = classify_intent(text)
    assert conf is IntentConfidence.LOW
    assert [h.intent for h in hits] == [TaskIntent.UNDERSTAND_SYSTEM]
    ti = classify_temporal_intent(text)
    assert ti.query is TemporalQuery.CURRENT_BELIEF
    assert ti.include_invalidated is False
    assert ti.cue is None
    assert authority_query_intent(text) is QueryIntent.AMBIGUOUS


@pytest.mark.parametrize(
    "text,intent,temporal,cue,authority",
    [
        ("how much did I pay", TaskIntent.RECALL_PERSONAL_HISTORY, TemporalQuery.CURRENT_BELIEF, None,
         QueryIntent.AMBIGUOUS),
        ("I used to live in Boston", TaskIntent.RECALL_PERSONAL_HISTORY, TemporalQuery.AS_KNOWN_AT,
         "used to", QueryIntent.PREVIOUS_VALUE),
        ("what is my rent now", TaskIntent.UNDERSTAND_SYSTEM, TemporalQuery.CURRENT_BELIEF, "now",
         QueryIntent.CURRENT_STATE),
        ("is the token still valid", TaskIntent.VERIFY_CURRENTNESS, TemporalQuery.CURRENT_BELIEF, None,
         QueryIntent.AMBIGUOUS),
        ("previously we used postgres", TaskIntent.UNDERSTAND_SYSTEM, TemporalQuery.AS_KNOWN_AT,
         "previously", QueryIntent.PREVIOUS_VALUE),
    ],
)
def test_real_cues_still_trigger(text, intent, temporal, cue, authority) -> None:
    hits, _ = classify_intent(text)
    assert intent in [h.intent for h in hits] or (
        intent is TaskIntent.UNDERSTAND_SYSTEM and hits[0].intent is intent)
    ti = classify_temporal_intent(text)
    assert ti.query is temporal
    assert ti.cue == cue
    assert authority_query_intent(text) is authority


def test_intent_cues_fire_on_real_first_person_and_still() -> None:
    assert classify_intent("how much did I pay")[1] is IntentConfidence.HIGH
    assert classify_intent("I used to live in Boston")[1] is IntentConfidence.HIGH
    assert classify_intent("is the token still valid")[1] is IntentConfidence.HIGH


# --- P2 through recall: unresolved conflict refused for lookalike queries -----

@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["which api used the old auth token", "which service handles the auth token"])
async def test_unresolved_conflict_refused_for_lookalike_queries(
    stub_graphiti_client, stub_memory_graph_adapter, query,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.stale_anchored_memories = lambda **kwargs: []
    service = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=adapter,
                            scoring_service=ScoringService(), read_only=True)
    tuning = RetrievalTuningConfig(enable_assertion_shadow=False, enable_warden_gate=True,
                                   enable_intent_lens=True, enable_contradiction_interrupt=True)
    stub_graphiti_client.search_scored_results = [("c1", "config", 0.85)]
    adapter.candidate_metadata = [dict(
        uuid="c1", name="config", content="Authored configuration control", namespace="tenant",
        scope="PERSISTENT", type="SEMANTIC", freshness="ACTIVE",
        conflict_group_id="conflict-1", conflict_status="unresolved")]
    adapter.candidate_provenance_rows = [dict(
        uuid="c1", evidence_node_kinds=["git"], anchor_projects=["menhir"], episode_sources=[])]
    adapter.temporal_fact_rows = []
    result = await service.recall(query, namespace="tenant", file_context_project="menhir",
                                  include_invalidated=True, source_memory_limit=0, update_access=False,
                                  tuning=tuning, include_warden_status=True)
    assert result.results == []
    assert result.warden_status.refused == 1
