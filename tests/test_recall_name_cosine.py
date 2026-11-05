"""The real name cosine and its default-off floor (plan menhir-recall-real-similarity-test)."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from menhir.config import MemorySettings
from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from menhir.domain.recall import CandidateData, QueryPreset, RecallResult
from menhir.domain.retrieval_trace_models import ScoringTrace
from menhir.domain.retrieval_tuning import CandidateSource, RetrievalTuningConfig
from menhir.explorer.recall_lab import RecallLabTuning
from menhir.services.recall_name_cosine import attach_name_cosines, name_cosine
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recall_lab_batch.py"
_spec = importlib.util.spec_from_file_location("recall_lab_batch", _SCRIPT)
batch = importlib.util.module_from_spec(_spec)
sys.modules["recall_lab_batch"] = batch
_spec.loader.exec_module(batch)


def _candidate(**overrides: object) -> CandidateData:
    values = dict(
        uuid="a", name="n", content="c", scope="PERSISTENT", memory_type="SEMANTIC",
        similarity=0.8, last_accessed_days_ago=1.0, edge_count=1, adjacency_score=0.0,
        freshness="ACTIVE",
    )
    values.update(overrides)
    return CandidateData(**values)


def _score(candidates, **kwargs):
    trace = ScoringTrace()
    results = ScoringService().score_candidates(
        candidates, QueryPreset.KNOWLEDGE, trace=trace, **kwargs
    )
    return [r.uuid for r in results], {c.uuid: c for c in trace.candidates}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 0.0], [-1.0, 0.0], -1.0),
        (None, [1.0], None),
        ([1.0], None, None),
        ([1.0, 0.0], [1.0], None),
        ([0.0, 0.0], [1.0, 0.0], None),
    ],
)
def test_name_cosine_is_raw_and_none_means_no_signal(a, b, expected) -> None:
    assert name_cosine(a, b) == expected


@pytest.mark.unit
def test_floor_off_ignores_a_low_cosine() -> None:
    uuids, _ = _score([_candidate(name_cosine=-0.9)])
    assert uuids == ["a"]


@pytest.mark.unit
def test_floor_drops_below_keeps_at_and_records_the_drop() -> None:
    uuids, traced = _score(
        [_candidate(uuid="low", name_cosine=0.29), _candidate(uuid="at", name_cosine=0.30)],
        name_cosine_floor=0.30,
    )
    assert uuids == ["at"]
    assert traced["low"].survived_floor is False
    assert traced["at"].survived_floor is True


@pytest.mark.unit
def test_floor_never_drops_a_candidate_with_no_vector() -> None:
    uuids, _ = _score([_candidate(name_cosine=None)], name_cosine_floor=0.9)
    assert uuids == ["a"]


@pytest.mark.unit
def test_exempt_sources_are_kept_unless_the_arm_says_all_sources() -> None:
    exempt = _candidate(source=CandidateSource.BM25, name_cosine=0.1)
    assert _score([exempt], name_cosine_floor=0.5)[0] == ["a"]
    assert _score([exempt], name_cosine_floor=0.5, name_cosine_floor_all_sources=True)[0] == []


@pytest.mark.unit
def test_floored_candidates_leave_prominence_normalization() -> None:
    """A dropped hub must not scale the survivors, or the arm differs from a real floor."""
    hub = _candidate(uuid="hub", edge_count=1000, name_cosine=0.1)
    kept = _candidate(uuid="kept", edge_count=3, name_cosine=0.9)
    _, traced = _score([hub, kept], name_cosine_floor=0.5)
    assert traced["kept"].score_parts.prominence_bonus == 1.0


@pytest.mark.unit
@pytest.mark.parametrize("value", [1.5, -1.5, float("nan")])
def test_tuning_rejects_a_floor_off_the_cosine_scale(value) -> None:
    with pytest.raises(ValueError):
        RetrievalTuningConfig(name_cosine_floor=value)


@pytest.mark.unit
def test_floor_is_off_by_default_everywhere() -> None:
    for tuning in (RetrievalTuningConfig(), RecallLabTuning().to_domain()):
        assert tuning.name_cosine_floor is None
        assert tuning.record_name_cosine is False


@pytest.mark.unit
def test_no_setting_can_turn_the_arm_on(monkeypatch) -> None:
    """frontier_shadow traces production recalls, so production must not reach these knobs."""
    monkeypatch.setenv("MENHIR_NAME_COSINE_FLOOR", "0.9")
    monkeypatch.setenv("MENHIR_RECORD_NAME_COSINE", "1")
    tuning = MemorySettings.from_env().retrieval_tuning()
    assert tuning.name_cosine_floor is None
    assert tuning.record_name_cosine is False


@pytest.mark.unit
def test_lab_arm_reaches_the_domain_tuning() -> None:
    tuning = RecallLabTuning(name_cosine_floor=0.3, name_cosine_floor_all_sources=True).to_domain()
    assert (tuning.name_cosine_floor, tuning.name_cosine_floor_all_sources) == (0.3, True)


class _Embedder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def embed_query(self, _query: str) -> list[float]:
        self.calls += 1
        if self.error:
            raise self.error
        return [1.0, 0.0]


class _Adapter:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.namespaces: list[str | None] = []

    def fetch_name_embeddings(self, uuids, namespace=None):
        self.namespaces.append(namespace)
        return {u: self.vectors[u] for u in uuids if u in self.vectors}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attach_sets_cosines_scopes_the_read_and_leaves_views_empty() -> None:
    adapter = _Adapter({"a": [1.0, 0.0]})
    service = SimpleNamespace(graphiti_client=_Embedder(), graph_adapter=adapter)
    out, record = await attach_name_cosines(
        service, "q", [_candidate(uuid="a"), _candidate(uuid="view")], "ns1"
    )
    assert record.warning is None
    assert record.cosines == {"a": 1.0, "view": None}
    assert [c.name_cosine for c in out] == [1.0, None]
    assert adapter.namespaces == ["ns1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attach_failure_warns_and_keeps_every_candidate() -> None:
    service = SimpleNamespace(
        graphiti_client=_Embedder(error=TimeoutError("x")), graph_adapter=_Adapter({})
    )
    candidates = [_candidate()]
    out, record = await attach_name_cosines(service, "q", candidates, None)
    assert out == candidates
    assert record.warning == "name_cosine_unavailable:TimeoutError"


def _recall_service(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    seen = datetime.now(timezone.utc) - timedelta(hours=1)
    stub_graphiti_client.search_scored_results = [("near", "Near", 0.9), ("far", "Far", 0.8)]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": uuid, "name": uuid, "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "text", "summary": None, "last_accessed": seen, "edge_count": 1,
            "sharpness": 0.5, "freshness": "ACTIVE", "user_flagged": False,
        }
        for uuid in ("near", "far")
    ]
    stub_memory_graph_adapter.fetch_name_embeddings = lambda uuids, namespace=None: {
        "near": [1.0, 0.0], "far": [0.0, 1.0],
    }
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


async def _vector(_query: str) -> list[float]:
    return [1.0, 0.0]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("trace", [False, True])
async def test_recall_reads_no_cosine_without_the_tuning(
    stub_graphiti_client, stub_memory_graph_adapter, trace
) -> None:
    """Trace alone must not trigger it: frontier_shadow traces production recalls."""
    service = _recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    calls: list[object] = []
    stub_memory_graph_adapter.fetch_name_embeddings = lambda *a, **k: calls.append(a) or {}

    result = await service.recall("q", trace=trace)

    assert {r.uuid for r in result.results} == {"near", "far"}
    assert calls == []
    assert result.name_cosine is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recording_cosines_does_not_change_results(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    service = _recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.embed_query = _vector

    plain = await service.recall("q")
    recorded = await service.recall("q", tuning=RetrievalTuningConfig(record_name_cosine=True))

    assert [r.uuid for r in recorded.results] == [r.uuid for r in plain.results]
    # approx: recency moves with the clock between the two calls.
    assert [r.final_score for r in recorded.results] == pytest.approx(
        [r.final_score for r in plain.results]
    )
    assert recorded.name_cosine.cosines == {"near": 1.0, "far": 0.0}
    assert recorded.name_cosine.warning is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_floor_arm_drops_through_the_pipeline(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    service = _recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.embed_query = _vector
    tuning = replace(RetrievalTuningConfig(), name_cosine_floor=0.5)

    result = await service.recall("q", tuning=tuning, trace=True)

    assert [r.uuid for r in result.results] == ["near"]
    assert {c.uuid: c.survived_floor for c in result.trace.candidates} == {
        "near": True, "far": False,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backend_wire_shape_has_no_name_cosine_key() -> None:
    ops = object.__new__(RuntimeProviderDataOpsMixin)
    result = RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
    )
    ops.built = SimpleNamespace(
        settings=SimpleNamespace(),
        recall_service=SimpleNamespace(recall=AsyncMock(return_value=result)),
    )
    ops._effective_session_id = lambda: None

    assert "name_cosine" not in await ops.recall("q")


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "bolt://127.0.0.1:7687",
        "bolt://localhost:7688",
        "bolt://127.0.0.1:7689",
        "bolt://127.0.0.1",
        "bolt://192.168.86.29:18341",
        "neo4j+s://example.com:18341",
    ],
)
def test_batch_refuses_anything_but_a_throwaway_graph(uri) -> None:
    with pytest.raises(SystemExit):
        batch.check_target(uri)


@pytest.mark.unit
def test_batch_accepts_a_loopback_throwaway_port() -> None:
    batch.check_target("bolt://127.0.0.1:18341")


@pytest.mark.unit
def test_batch_arms_layer_on_the_base_tuning_and_reject_unknown_keys(tmp_path) -> None:
    arms = tmp_path / "arms.json"
    arms.write_text(json.dumps([{"id": "base"}, {"id": "f", "tuning": {"name_cosine_floor": 0.3}}]))
    base = RetrievalTuningConfig(enable_warden_gate=False)

    loaded = batch.load_arms(arms, base)

    assert loaded["base"] == replace(base, record_name_cosine=True)
    assert loaded["f"].name_cosine_floor == 0.3
    assert loaded["f"].enable_warden_gate is False

    arms.write_text(json.dumps([{"id": "bad", "tuning": {"no_such_knob": 1}}]))
    with pytest.raises(TypeError):
        batch.load_arms(arms, base)


@pytest.mark.unit
def test_batch_blocks_llm_and_reranker() -> None:
    client = SimpleNamespace(llm_client_ref=SimpleNamespace(), reranker_ref=SimpleNamespace())
    calls = batch.block_llm(client)
    with pytest.raises(RuntimeError):
        client.llm_client_ref.generate_response("x")
    with pytest.raises(RuntimeError):
        client.reranker_ref.rank("x")
    assert len(calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_pins_one_vector_per_input_across_arms_and_reruns(tmp_path) -> None:
    """Without this, embedding jitter between arms reads as a floor effect."""
    made: list[str] = []

    async def create(text: str) -> list[float]:
        made.append(text)
        return [float(len(made))]

    client = SimpleNamespace(embedder_ref=SimpleNamespace(create=create))
    cache = tmp_path / "v.json"

    vectors = batch.pin_query_vectors(client, cache)
    first = await client.embedder_ref.create("q")
    assert await client.embedder_ref.create("q") == first
    batch.save_vectors(vectors, cache)

    rerun = SimpleNamespace(embedder_ref=SimpleNamespace(create=create))
    batch.pin_query_vectors(rerun, cache)
    assert await rerun.embedder_ref.create("q") == first
    assert made == ["q"]


_RECALL_ARGS = dict(
    limit=10, candidate_k=50, preset=QueryPreset.KNOWLEDGE, source_memory_limit=None,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_writes_all_candidates_and_never_touches_access(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path
) -> None:
    service = _recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.embed_query = _vector
    seen: list[dict[str, object]] = []
    real_recall = service.recall

    async def spy(query, **kwargs):
        seen.append(kwargs)
        return await real_recall(query, **kwargs)

    out = tmp_path / "out.jsonl"
    failed = await batch.run_batch(
        SimpleNamespace(recall=spy),
        [{"id": "q1", "namespace": None, "query": "q"}],
        {"f": RetrievalTuningConfig(name_cosine_floor=0.5)},
        out,
        concurrency=2,
        **_RECALL_ARGS,
    )

    row = json.loads(out.read_text(encoding="utf-8"))
    assert row.get("error") is None
    assert failed == 0
    assert seen[0]["update_access"] is False
    assert seen[0]["trace"] is True
    assert [r["uuid"] for r in row["results"]] == ["near"]
    assert row["results"][0]["relevance"] == "high"
    assert {c["uuid"]: (c["survived_floor"], c["name_cosine"]) for c in row["candidates"]} == {
        "near": (True, 1.0), "far": (False, 0.0),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_writes_a_clean_row_when_recall_finds_nothing(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path
) -> None:
    """Recall returns early with no trace on an empty namespace; that is a result, not a failure."""
    service = _recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.search_scored_results = []
    stub_memory_graph_adapter.candidate_metadata = []
    out = tmp_path / "out.jsonl"

    failed = await batch.run_batch(
        service,
        [{"id": "q1", "namespace": None, "query": "q"}],
        {"base": RetrievalTuningConfig()},
        out,
        concurrency=1,
        **_RECALL_ARGS,
    )

    row = json.loads(out.read_text(encoding="utf-8"))
    assert failed == 0
    assert (row["results"], row["candidates"]) == ([], [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_counts_a_cosine_outage_as_failed_and_retries_it(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path
) -> None:
    """A floor arm with no cosines ran as its baseline, so it must not pass as a result."""
    service = _recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.embed_query = _Embedder(error=TimeoutError("x")).embed_query
    out = tmp_path / "out.jsonl"
    rows = [{"id": "q1", "namespace": None, "query": "q"}]
    arms = {"f": RetrievalTuningConfig(name_cosine_floor=0.5)}

    assert await batch.run_batch(service, rows, arms, out, concurrency=1, **_RECALL_ARGS) == 1
    assert batch.finished_pairs(out) == set()

    stub_graphiti_client.embed_query = _vector
    assert await batch.run_batch(service, rows, arms, out, concurrency=1, **_RECALL_ARGS) == 0
    assert batch.finished_pairs(out) == {("q1", "f")}
