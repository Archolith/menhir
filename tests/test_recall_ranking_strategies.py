"""Recall ranking strategies (reranker_only / oracle_only / oracle_combined), offline.

Counterexample tests for the rerank-as-oracle work: strategy equivalences, warden invariance,
head-before-tail, combiner arithmetic, executor batch failure modes, trace, config, redaction.
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import UTC, datetime

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
)
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.temporal_lens import TemporalLens
from menhir.explorer.recall_lab import RecallLabTuning, _redact_allowlist
from menhir.services import cross_encoder_rerank as cer
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.recall_service import RecallService
from menhir.services.rerank_oracle import CrossEncoderRerankOracle
from menhir.services.retrieval_oracles import SemanticOracle
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.unit
QUERY = "what colour is the sky"
ALL_ROLES = ("relevant", "current", "historical", "conflict", "blocked", "rerank")
STRATEGIES = ("reranker_only", "oracle_only", "oracle_combined")


# --------------------------------------------------------------------------- harness


class FakeScorer:
    label = "fake"
    device = "cpu"

    def __init__(self, by_name: dict[str, float]):
        self.by_name = by_name
        self.calls: list[list[str]] = []

    def score(self, query, passages):
        self.calls.append(list(passages))
        return [next(v for k, v in self.by_name.items() if p.startswith(k)) for p in passages]


def _install(monkeypatch, by_name: dict[str, float]) -> FakeScorer:
    scorer = FakeScorer(by_name)
    monkeypatch.setattr(cer, "load_reranker", lambda model_dir, device: scorer)
    return scorer


def _break_model(monkeypatch) -> None:
    def broken(model_dir, device):
        raise cer.RerankUnavailable("no model")

    monkeypatch.setattr(cer, "load_reranker", broken)


def _meta(uuid, name, **extra):
    row = {
        "uuid": uuid, "name": name, "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": f"{name} content", "summary": f"{name} summary",
        "last_accessed": datetime.now(UTC), "edge_count": 3,
        "freshness": "ACTIVE", "user_flagged": False,
    }
    row.update(extra)
    return row


# (uuid, name, RRF score, extra metadata)
POOL3 = [("entity-1", "Alpha", 0.85, {}), ("entity-2", "Beta", 0.65, {}), ("entity-3", "Gamma", 0.05, {})]


async def _recall(client, adapter, tuning, pool=POOL3, *, trace=False, **kw):
    client.search_scored_results = [(u, n, s) for u, n, s, _ in pool]
    adapter.candidate_metadata = [_meta(u, n, **extra) for u, n, _, extra in pool]
    service = RecallService(graphiti_client=client, graph_adapter=adapter,
                            scoring_service=ScoringService(), rerank_model_dir="m")
    return await service.recall(QUERY, tuning=tuning, trace=trace, **kw)


def _ids(res):
    return [r.uuid for r in res.results]


def _scores(res):
    return {r.uuid: r.final_score for r in res.results}


def _only_rerank_weights(**over):
    base = {f"oracle_weight_{role}": 0.0 for role in ALL_ROLES}
    base["oracle_weight_rerank"] = 1.0
    base.update(over)
    return base


# --------------------------------------------------------------------------- 1


@pytest.mark.asyncio
async def test_default_strategy_is_reranker_only_and_matches_default_config(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Breaks if the default strategy changes or reranker_only stops being the pre-strategy path."""
    assert RetrievalTuningConfig().ranking_strategy == "reranker_only"
    _install(monkeypatch, {"Gamma": 6.0, "Beta": 2.0, "Alpha": -3.0})
    default = await _recall(stub_graphiti_client, stub_memory_graph_adapter, RetrievalTuningConfig())
    explicit = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                             RetrievalTuningConfig(ranking_strategy="reranker_only"))
    assert _ids(default) == ["entity-3", "entity-2", "entity-1"]  # pure cross-encoder order
    assert _ids(explicit) == _ids(default)
    assert _scores(explicit) == _scores(default)
    assert explicit.note == default.note
    assert _scores(default)["entity-3"] == pytest.approx(cer.sigmoid(6.0))


@pytest.mark.asyncio
async def test_default_strategy_model_failure_matches_default_config(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Breaks if explicit reranker_only handles a model failure differently from the default."""
    _break_model(monkeypatch)
    default = await _recall(stub_graphiti_client, stub_memory_graph_adapter, RetrievalTuningConfig())
    explicit = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                             RetrievalTuningConfig(ranking_strategy="reranker_only"))
    assert _ids(default) == ["entity-1", "entity-2"]
    assert _ids(explicit) == _ids(default) and explicit.note == default.note
    assert _scores(explicit) == pytest.approx(_scores(default))  # recency term reads the clock
    assert "rerank unavailable (RerankUnavailable)" in (default.note or "")


# --------------------------------------------------------------------------- 2


@pytest.mark.asyncio
async def test_combined_with_rerank_only_weight_reproduces_reranker_only(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Other oracles disagree with the logits (RRF order opposes logit order, entity-1 and
    entity-4 carry a currentness signal, entity-2/entity-4 tie on logit). With every role weight
    0 except rerank, combined must still order by logit. Breaks if another role leaks into the
    combined score, or the head is not ordered by the rerank role."""
    pool = [
        ("entity-1", "Alpha", 0.85, {"created_at": "2024-01-01T00:00:00Z"}),
        ("entity-2", "Beta", 0.65, {}),
        ("entity-3", "Gamma", 0.05, {}),
        ("entity-4", "Delta", 0.40, {"created_at": "2024-01-01T00:00:00Z"}),
    ]
    _install(monkeypatch, {"Gamma": 6.0, "Beta": 2.0, "Delta": 2.0, "Alpha": -3.0})
    baseline = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                             RetrievalTuningConfig(), pool)
    combined = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter,
        RetrievalTuningConfig(ranking_strategy="oracle_combined", **_only_rerank_weights()), pool,
    )
    assert _ids(baseline) == ["entity-3", "entity-2", "entity-4", "entity-1"]
    assert _ids(combined) == _ids(baseline)
    assert _scores(combined) == _scores(baseline)


@pytest.mark.asyncio
async def test_combined_default_weights_can_differ_from_reranker_only(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Guards the test above against being vacuous: with a huge relevant weight and no rerank
    weight, combined must NOT follow the logits, so the weights really steer the order."""
    _install(monkeypatch, {"Gamma": 6.0, "Beta": 2.0, "Alpha": -3.0})
    res = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter,
        RetrievalTuningConfig(ranking_strategy="oracle_combined",
                              **_only_rerank_weights(oracle_weight_rerank=0.0, oracle_weight_relevant=10.0)),
    )
    assert _ids(res) != ["entity-3", "entity-2", "entity-1"]
    assert _ids(res)[0] == "entity-1"  # highest semantic relevance leads when logits carry no weight


# --------------------------------------------------------------------------- 3


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_wrong_scope_refused_in_every_strategy_before_the_cap(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch, strategy,
):
    """entity-1 is in another project, holds the top logit and the top RRF score. Every strategy
    must refuse it; with a cap of one the survivor is the next candidate (refusal precedes the cap).
    Breaks if the warden gate stops running for a strategy, or the cap is applied first."""
    pool = [
        ("entity-1", "Alpha", 0.85, {"project": "other"}),
        ("entity-2", "Beta", 0.65, {}),
        ("entity-3", "Gamma", 0.60, {}),
    ]
    _install(monkeypatch, {"Alpha": 9.0, "Beta": 2.0, "Gamma": -3.0})
    uncapped = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                             RetrievalTuningConfig(ranking_strategy=strategy), pool,
                             file_context_project="mine")
    assert "entity-1" not in _ids(uncapped)
    assert _ids(uncapped)  # something survives
    capped = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter,
        RetrievalTuningConfig(ranking_strategy=strategy, rerank_max_results=1), pool,
        file_context_project="mine", limit=1,
    )
    assert _ids(capped) == ["entity-2"]


@pytest.mark.asyncio
async def test_wrong_scope_candidate_is_present_when_the_query_has_no_project(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Control for the refusal test: without a query project the same candidate is kept (and leads)."""
    pool = [("entity-1", "Alpha", 0.85, {"project": "other"}), ("entity-2", "Beta", 0.65, {})]
    _install(monkeypatch, {"Alpha": 9.0, "Beta": 2.0})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, RetrievalTuningConfig(), pool)
    assert _ids(res)[0] == "entity-1"


# --------------------------------------------------------------------------- 4


@pytest.mark.asyncio
@pytest.mark.parametrize("weights", [
    {"oracle_weight_rerank": 0.0, "oracle_weight_relevant": 10.0},
    {"oracle_weight_rerank": 0.0, "oracle_weight_relevant": 10.0, "oracle_weight_current": 10.0,
     "oracle_weight_historical": 10.0},
])
async def test_combined_tail_never_outranks_head(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch, weights,
):
    """rerank_top_n=1: only the first candidate is scored. The tail candidate has far better
    combiner evidence (the trace shows its combiner rank ahead), yet must follow the head.
    Breaks if combined mode sorts by combiner rank alone."""
    pool = [("entity-1", "Alpha", 0.30, {}), ("entity-2", "Beta", 0.90, {})]
    scorer = _install(monkeypatch, {"Alpha": -8.0, "Beta": 5.0})
    res = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter,
        RetrievalTuningConfig(ranking_strategy="oracle_combined", rerank_top_n=1, **weights),
        pool, trace=True,
    )
    assert len(scorer.calls) == 1 and len(scorer.calls[0]) == 1
    head_id = next(u for u, n, _, _ in pool if scorer.calls[0][0].startswith(n))
    tail_id = next(u for u, *_ in pool if u != head_id)
    assert _ids(res) == [head_id, tail_id]
    rows = {r.candidate_id: r for r in res.trace.ranking}
    # The counterexample is real: the combiner itself prefers the tail.
    assert rows[tail_id].combiner_rank < rows[head_id].combiner_rank


# --------------------------------------------------------------------------- 5 & 6 (combiner)


def _random_z(rng: random.Random, with_rerank: bool) -> dict[str, float]:
    z = {r: rng.uniform(-3.0, 6.0) for r in ("relevant", "current", "historical", "conflict", "blocked")}
    if with_rerank:
        z["rerank"] = rng.uniform(-12.0, 12.0)
    return z


@pytest.mark.parametrize("intent", [TemporalLens.CURRENT, TemporalLens.HISTORICAL, TemporalLens.ANY])
@pytest.mark.parametrize("with_rerank", [False, True])
def test_ranking_score_without_weights_equals_unit_weights_exactly(intent, with_rerank):
    """Breaks if the no-weights default stops being exactly 1.0 per role (or the sum order changes)."""
    rng = random.Random(20261109)
    q = QueryContext(text="x", intent=intent)
    unit = dict.fromkeys(ALL_ROLES, 1.0)
    for _ in range(200):
        z = _random_z(rng, with_rerank)
        assert LogSpaceOracleCombiner._ranking_score(q, z) == LogSpaceOracleCombiner._ranking_score(q, z, unit)


def _grouped(with_rerank: bool, logit: float = 4.5) -> dict[str, list[OracleResult]]:
    def base(cid: str, sim: float, extra: list[OracleResult]) -> list[OracleResult]:
        return [
            OracleResult(oracle="semantic", probability=sim, source_family="semantic"),
            OracleResult(oracle="temporal", probability=0.8, confidence=0.8, target=OracleTarget.CURRENTNESS,
                         source_family="temporal"),
            OracleResult(oracle="scope", probability=1.0, polarity=OraclePolarity.CONTRADICT,
                         target=OracleTarget.SCOPE, source_family="scope") if cid == "b" else
            OracleResult.neutral("scope"),
            *extra,
        ]

    rr = lambda v: [OracleResult(oracle="rr", probability=0.9, source_family="rerank", log_odds=v)]  # noqa: E731
    return {
        "a": base("a", 0.9, rr(logit) if with_rerank else []),
        "b": base("b", 0.4, rr(-2.0) if with_rerank else []),
    }


@pytest.mark.parametrize("intent", [TemporalLens.CURRENT, TemporalLens.HISTORICAL, TemporalLens.ANY])
@pytest.mark.parametrize("with_rerank", [False, True])
def test_contributions_sum_to_ranking_score_with_non_unit_weights(intent, with_rerank):
    """Breaks if packet.contributions drift from the score that orders candidates (e.g. weights
    applied to one but not the other)."""
    weights = {"relevant": 2.5, "current": 0.3, "historical": 4.0, "conflict": 0.0, "blocked": 7.0, "rerank": 0.5}
    combiner = LogSpaceOracleCombiner(weights)
    _, packets = combiner.rank(QueryContext(text="x", intent=intent), _grouped(with_rerank))
    for packet in packets.values():
        assert packet.ranking_score is not None
        assert sum(packet.contributions.values()) == pytest.approx(packet.ranking_score)
        assert ("rerank" in packet.contributions) is with_rerank
    if with_rerank:
        assert packets["a"].contributions["rerank"] == pytest.approx(0.5 * 4.5)  # weight applied once


def test_rank_orders_by_ranking_score_and_weights_change_the_order():
    """Weights must steer the order: rerank dominates at weight 10, relevance dominates at 0."""
    q = QueryContext(text="x", intent=TemporalLens.ANY)
    grouped = {
        "lowsem_highlogit": [OracleResult(oracle="s", probability=0.1),
                             OracleResult(oracle="rr", probability=0.99, source_family="rerank", log_odds=8.0)],
        "highsem_lowlogit": [OracleResult(oracle="s", probability=1.0),
                             OracleResult(oracle="rr", probability=0.1, source_family="rerank", log_odds=-2.0)],
    }
    ranked_rr, _ = LogSpaceOracleCombiner({"rerank": 10.0}).rank(q, grouped)
    ranked_rel, _ = LogSpaceOracleCombiner({"rerank": 0.0, "relevant": 10.0}).rank(q, grouped)
    assert ranked_rr[0] == "lowsem_highlogit"
    assert ranked_rel[0] == "highsem_lowlogit"


@pytest.mark.parametrize("logit", [11.0, -7.5])
def test_rerank_result_feeds_only_the_rerank_role_and_is_uncapped(logit):
    """Breaks if a log_odds result is routed into relevant/blocked or hits the family cap (3.0)."""
    results = [OracleResult(oracle="rr", probability=0.99, source_family="rerank", log_odds=logit)]
    q = QueryContext(text="x")
    _, packets = LogSpaceOracleCombiner().rank(q, {"c": results})
    z = packets["c"].role_logits
    assert z["relevant"] == 0 and z["blocked"] == 0
    assert z["rerank"] == logit
    assert abs(z["rerank"]) > 3.0 or logit < 0


def test_packet_without_a_rerank_result_has_no_rerank_role():
    _, packets = LogSpaceOracleCombiner().rank(
        QueryContext(text="x"), {"c": [OracleResult(oracle="s", probability=0.5)]})
    assert "rerank" not in packets["c"].role_logits
    assert "rerank" not in packets["c"].contributions


def test_contradicting_or_non_finite_log_odds_never_reach_any_role():
    """A CONTRADICT-polarity or nan log_odds result must not create a rerank logit nor a penalty."""
    results = [
        OracleResult(oracle="a", probability=0.9, polarity=OraclePolarity.CONTRADICT, log_odds=9.0,
                     source_family="rerank"),
        OracleResult(oracle="b", probability=0.9, log_odds=float("nan"), source_family="rerank"),
    ]
    _, packets = LogSpaceOracleCombiner().rank(QueryContext(text="x"), {"c": results})
    z = packets["c"].role_logits
    assert "rerank" not in z
    assert z["relevant"] == 0 and z["blocked"] == 0 and z["current"] == 0


# --------------------------------------------------------------------------- 7 (executor)


class _SlowBatch:
    name = "slow_batch"

    async def evaluate_batch(self, query, candidates):
        await asyncio.sleep(5)
        return {}


class _BoomBatch:
    name = "boom_batch"

    async def evaluate_batch(self, query, candidates):
        raise KeyError("nope")


class _PartialBatch:
    name = "partial_batch"

    async def evaluate_batch(self, query, candidates):
        first = candidates[0]
        return {first.id: OracleResult(oracle=self.name, probability=0.7, log_odds=0.85,
                                       source_family="rerank")}


def _cands():
    return [CandidateMemory(id=i, content=f"the sky is {i}") for i in ("c2", "c1", "c3")]


@pytest.mark.asyncio
@pytest.mark.parametrize("oracle,note", [
    (_SlowBatch(), "timeout"),
    (_BoomBatch(), "error: KeyError"),
])
async def test_batch_oracle_failure_is_neutral_for_every_candidate(oracle, note):
    """Breaks if a batch failure raises, drops candidates, or is not MISSING (missing != falsity)."""
    ex = OracleExecutor([SemanticOracle()], batch_oracles=[oracle], batch_timeout_s=0.02)
    out = await ex.evaluate(QueryContext(text="sky"), _cands())
    assert list(out) == ["c1", "c2", "c3"]
    for results in out.values():
        mine = [r for r in results if r.oracle == oracle.name]
        assert len(mine) == 1
        assert mine[0].polarity is OraclePolarity.MISSING and mine[0].note == note
        assert mine[0].log_odds is None


@pytest.mark.asyncio
async def test_batch_oracle_omitted_ids_are_not_scored_and_per_candidate_oracles_unaffected():
    qc = QueryContext(text="sky")
    cands = _cands()
    plain = await OracleExecutor([SemanticOracle()]).evaluate(qc, cands)
    with_batch = await OracleExecutor(
        [SemanticOracle()], batch_oracles=[_PartialBatch()]).evaluate(qc, cands)
    scored = [cid for cid, rs in with_batch.items() if any(r.log_odds == 0.85 for r in rs)]
    assert len(scored) == 1
    for cid, rs in with_batch.items():
        mine = next(r for r in rs if r.oracle == "partial_batch")
        if cid not in scored:
            assert mine.polarity is OraclePolarity.MISSING and mine.note == "not scored"
        # The semantic result is exactly what it is without any batch oracle.
        assert [r for r in rs if r.oracle == "semantic"] == plain[cid]
        assert [r.oracle for r in rs] == ["semantic", "partial_batch"]  # stable priority order


@pytest.mark.asyncio
async def test_batch_oracle_output_is_deterministic():
    ex = OracleExecutor([SemanticOracle()], batch_oracles=[_PartialBatch(), _BoomBatch()])
    first = await ex.evaluate(QueryContext(text="sky"), _cands())
    second = await ex.evaluate(QueryContext(text="sky"), _cands())
    assert first == second
    assert list(first) == sorted(first)


# --------------------------------------------------------------------------- 8


@pytest.mark.asyncio
async def test_model_runs_once_under_combined_and_never_under_oracle_only(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Breaks if combined re-runs inference inside the combiner, or oracle_only loads the model."""
    scorer = _install(monkeypatch, {"Gamma": 6.0, "Beta": 2.0, "Alpha": -3.0})
    await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                  RetrievalTuningConfig(ranking_strategy="oracle_combined"))
    assert len(scorer.calls) == 1

    scorer.calls.clear()
    only = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                         RetrievalTuningConfig(ranking_strategy="oracle_only"))
    assert scorer.calls == []
    off = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        RetrievalTuningConfig(enable_cross_encoder_rerank=False))
    assert scorer.calls == []
    assert _ids(only) == _ids(off) == ["entity-1", "entity-2"]
    assert _scores(only) == pytest.approx(_scores(off))  # recency term reads the clock


# --------------------------------------------------------------------------- 9


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_oracle_marks_non_finite_logits_missing(bad):
    """Breaks if a nan/inf logit becomes SUPPORT with log_odds (it would dominate or poison ranking)."""
    scorer = FakeScorer({"A": bad, "B": 1.5})
    oracle = CrossEncoderRerankOracle(model_dir="m", device="cpu", top_n=5,
                                      loader=lambda d, dev: scorer)
    cands = [CandidateMemory(id="a", content="x", metadata={"name": "A", "summary": "s"}),
             CandidateMemory(id="b", content="x", metadata={"name": "B", "summary": "s"})]
    results, note = await oracle.score_batch(QueryContext(text="q"), cands)
    assert note is None
    assert results["a"].polarity is OraclePolarity.MISSING and results["a"].log_odds is None
    assert results["b"].polarity is OraclePolarity.SUPPORT and results["b"].log_odds == 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["reranker_only", "oracle_combined"])
async def test_nan_logit_in_recall_is_unscored_not_leading(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch, strategy,
):
    """entity-1 (passes the floor) has a nan logit: it must be tail, behind the scored head, with
    a finite score. entity-3 (below the floor) with a nan logit is cut like any unscored tail."""
    tuning = RetrievalTuningConfig(ranking_strategy=strategy)
    _install(monkeypatch, {"Alpha": float("nan"), "Beta": 2.0, "Gamma": -3.0})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, tuning)
    assert _ids(res) == ["entity-2", "entity-3", "entity-1"]
    assert all(math.isfinite(s) for s in _scores(res).values())

    _install(monkeypatch, {"Alpha": 1.0, "Beta": 2.0, "Gamma": float("nan")})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, tuning)
    assert _ids(res) == ["entity-2", "entity-1"]  # entity-3 is below the RRF floor and unscored


# --------------------------------------------------------------------------- 10


@pytest.mark.asyncio
async def test_combined_model_failure_matches_reranker_only_failure(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """Breaks if oracle_combined fails differently from reranker_only when the model is missing."""
    _break_model(monkeypatch)
    ro = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                       RetrievalTuningConfig(ranking_strategy="reranker_only"))
    oc = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                       RetrievalTuningConfig(ranking_strategy="oracle_combined"))
    assert _ids(oc) == _ids(ro) == ["entity-1", "entity-2"]
    assert "rerank unavailable" in (oc.note or "")
    assert _scores(oc) == pytest.approx(_scores(ro))  # recency term reads the clock


# --------------------------------------------------------------------------- 11


@pytest.mark.asyncio
async def test_trace_ranking_rows_combined(stub_graphiti_client, stub_memory_graph_adapter, monkeypatch):
    """Breaks if the trace rows lose rank order/strategy/logit alignment or contributions stop
    summing to the score."""
    _install(monkeypatch, {"Alpha": 1.0, "Beta": 2.0, "Gamma": -3.0})
    pool = [("entity-1", "Alpha", 0.85, {}), ("entity-2", "Beta", 0.65, {}), ("entity-3", "Gamma", 0.50, {})]
    res = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter,
        RetrievalTuningConfig(ranking_strategy="oracle_combined", rerank_top_n=2), pool,
        trace=True,
    )
    rows = res.trace.ranking
    assert [r.candidate_id for r in rows] == _ids(res)
    assert [r.rank for r in rows] == list(range(1, len(rows) + 1))
    assert {r.strategy for r in rows} == {"oracle_combined"}
    by_id = {r.candidate_id: r for r in rows}
    head = {"entity-1": 1.0, "entity-2": 2.0}
    for uuid, row in by_id.items():
        if uuid in head:
            assert row.rerank_logit == head[uuid]
        else:
            assert row.rerank_logit is None
    assert any(r.rerank_logit is None for r in rows)  # the tail case is exercised
    for row in rows:
        assert row.ranking_score is not None
        assert sum(row.contributions.values()) == pytest.approx(row.ranking_score)
    # Head rows carry the rerank term, tail rows do not.
    assert all(("rerank" in r.contributions) == (r.rerank_logit is not None) for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["oracle_combined", "reranker_only"])
async def test_trace_strategy_is_the_effective_one_after_model_failure(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch, requested,
):
    _break_model(monkeypatch)
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        RetrievalTuningConfig(ranking_strategy=requested), trace=True)
    rows = res.trace.ranking
    assert [r.candidate_id for r in rows] == _ids(res) and rows
    assert {r.strategy for r in rows} == {"oracle_only"}
    assert all(r.rerank_logit is None for r in rows)


@pytest.mark.asyncio
async def test_trace_strategy_reranker_only_reports_logits(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    _install(monkeypatch, {"Gamma": 6.0, "Beta": 2.0, "Alpha": -3.0})
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        RetrievalTuningConfig(), trace=True)
    got = {r.candidate_id: (r.strategy, r.rerank_logit, r.rank) for r in res.trace.ranking}
    assert got == {"entity-3": ("reranker_only", 6.0, 1), "entity-2": ("reranker_only", 2.0, 2),
                   "entity-1": ("reranker_only", -3.0, 3)}


# --------------------------------------------------------------------------- 12


def test_tuning_validation():
    with pytest.raises(ValueError, match="ranking_strategy"):
        RetrievalTuningConfig(ranking_strategy="bogus")
    for bad in (-0.1, 10.1, float("nan")):
        with pytest.raises(ValueError, match="oracle_weight_current"):
            RetrievalTuningConfig(oracle_weight_current=bad)
    assert RetrievalTuningConfig(oracle_weight_current=0.0, oracle_weight_rerank=10.0)  # bounds inclusive


def test_settings_env_maps_strategy_into_tuning(monkeypatch):
    assert MemorySettings().retrieval_tuning().ranking_strategy == "reranker_only"
    monkeypatch.setenv("MENHIR_RECALL_RANKING_STRATEGY", " Oracle_Combined ")
    settings = MemorySettings.from_env()
    assert settings.retrieval_tuning().ranking_strategy == "oracle_combined"


def test_recall_lab_tuning_validation_and_round_trip():
    with pytest.raises(ValueError):
        RecallLabTuning(ranking_strategy="bogus")
    with pytest.raises(ValueError):
        RecallLabTuning(oracle_weight_current=10.5)
    domain = RecallLabTuning(ranking_strategy="oracle_combined", oracle_weight_current=0.5).to_domain()
    assert domain.ranking_strategy == "oracle_combined"
    assert domain.oracle_weight_current == 0.5
    assert domain.oracle_role_weights == {**dict.fromkeys(ALL_ROLES, 1.0), "current": 0.5}


# --------------------------------------------------------------------------- 13


def test_redaction_keeps_ranking_numbers_and_closed_vocab_but_masks_free_text():
    """Breaks if the allowlist drops ranking numbers (Lab unusable) or lets reason/rationale
    (which can quote memory text) through."""
    row = {
        "uuid": "u-1",
        "final_score": 0.7,
        "ranking": {
            "strategy": "oracle_combined",
            "rerank_logit": 4.25,
            "ranking_score": 5.5,
            "combiner_rank": 0,
            "decision": "refuse",
            "warden_label": None,
            "role_logits": {"relevant": 1.5, "current": 0.2, "blocked": 0.0, "rerank": 4.25},
            "weights": {"relevant": 1.0, "rerank": 0.5},
            "contributions": {"relevant": 1.5, "rerank": 2.125},
            "reason": "wrong scope: project (secret-project-name)",
            "rationale": ["+relevant semantic 0.64", "my private note"],
        },
    }
    out = _redact_allowlist(row, reveal=False)["ranking"]
    assert out["strategy"] == "oracle_combined" and out["decision"] == "refuse"
    assert out["rerank_logit"] == 4.25 and out["ranking_score"] == 5.5 and out["combiner_rank"] == 0
    assert out["role_logits"] == row["ranking"]["role_logits"]
    assert out["weights"] == row["ranking"]["weights"]
    assert out["contributions"] == row["ranking"]["contributions"]
    assert out["reason"] == "[hidden]"
    assert out["rationale"] == ["[hidden]", "[hidden]"]
    # A strategy/decision outside the closed vocabulary is masked (it could carry free text).
    leaky = _redact_allowlist({"ranking": {"strategy": "my secret", "decision": "free text"}}, reveal=False)
    assert leaky["ranking"] == {"strategy": "[hidden]", "decision": "[hidden]"}


def test_redaction_reveal_returns_ranking_untouched():
    row = {"ranking": {"reason": "wrong scope", "rationale": ["x"]}}
    assert _redact_allowlist(row, reveal=True) is row


# --------------------------------------------------------------------------- tie order


@pytest.mark.asyncio
async def test_combined_logit_tie_keeps_scored_order_when_ids_sort_against_it(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    """entity-9 (higher RRF) and entity-2 (lower RRF) tie on logit and their ids sort against the
    scored order. Combined with rerank-only weights must order them as reranker_only does.
    Breaks if ties fall back to candidate-id order instead of the scored order."""
    pool = [("entity-9", "Nine", 0.85, {}), ("entity-2", "Two", 0.65, {}), ("entity-5", "Five", 0.50, {})]
    _install(monkeypatch, {"Nine": 2.0, "Two": 2.0, "Five": -3.0})
    ro = await _recall(stub_graphiti_client, stub_memory_graph_adapter, RetrievalTuningConfig(), pool)
    oc = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter,
        RetrievalTuningConfig(ranking_strategy="oracle_combined", **_only_rerank_weights()), pool,
    )
    assert _ids(ro) == ["entity-9", "entity-2", "entity-5"]
    assert _ids(oc) == _ids(ro)
