"""Cross-encoder recall rerank (MENHIR_RECALL_RERANK_ENABLED), offline with a fake scorer."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.explorer.recall_lab import RecallLabTuning
from menhir.services import cross_encoder_rerank as cer
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.unit
QUERY = "what colour is the sky"


class FakeScorer:
    label = "fake"
    device = "cpu"

    def __init__(self, by_name: dict[str, float]):
        self.by_name = by_name
        self.calls: list[list[str]] = []

    def score(self, query, passages):
        self.calls.append(list(passages))
        return [next(v for k, v in self.by_name.items() if p.startswith(k)) for p in passages]


def _meta(uuid, name, edges=3):
    return {
        "uuid": uuid, "name": name, "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": f"{name} content", "summary": f"{name} summary",
        "last_accessed": datetime.now(UTC), "edge_count": edges,
        "freshness": "ACTIVE", "user_flagged": False,
    }


def _pool(client, adapter):
    # RRF-style scores: entity-3 sits under the 0.15 rank floor.
    client.search_scored_results = [
        ("entity-1", "Alpha", 0.85),
        ("entity-2", "Beta", 0.65),
        ("entity-3", "Gamma", 0.05),
    ]
    adapter.candidate_metadata = [_meta("entity-1", "Alpha"), _meta("entity-2", "Beta"),
                                  _meta("entity-3", "Gamma")]


async def _recall(client, adapter, *, enabled=True, top_n=50, **svc):
    _pool(client, adapter)
    service = RecallService(graphiti_client=client, graph_adapter=adapter,
                            scoring_service=ScoringService(), rerank_model_dir="m", **svc)
    return await service.recall(QUERY, tuning=RetrievalTuningConfig(
        enable_cross_encoder_rerank=enabled, rerank_top_n=top_n))


@pytest.fixture
def fake(monkeypatch):
    scorer = FakeScorer({"Gamma": 6.0, "Beta": 2.0, "Alpha": -3.0})
    seen = {}

    def loader(model_dir, device):
        seen["args"] = (model_dir, device)
        return scorer

    monkeypatch.setattr(cer, "load_reranker", loader)
    scorer.seen = seen
    return scorer


@pytest.mark.asyncio
async def test_off_keeps_the_floor_and_never_loads(stub_graphiti_client, stub_memory_graph_adapter, fake):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, enabled=False)
    assert [r.uuid for r in res.results] == ["entity-1", "entity-2"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_on_orders_by_cross_encoder_and_replaces_the_floor(
    stub_graphiti_client, stub_memory_graph_adapter, fake,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, rerank_device="cpu")
    ids = [r.uuid for r in res.results]
    assert ids == ["entity-3", "entity-2", "entity-1"]
    scores = [r.final_score for r in res.results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(cer.sigmoid(6.0))
    assert fake.seen["args"] == ("m", "cpu")
    assert fake.calls[0][0] == "Alpha: Alpha summary"
    assert "rerank" not in (res.note or "")


@pytest.mark.asyncio
async def test_candidates_past_top_n_keep_the_floor(
    stub_graphiti_client, stub_memory_graph_adapter, fake,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, top_n=1)
    ids = [r.uuid for r in res.results]
    assert len(fake.calls[0]) == 1
    # entity-1 is the only reranked one; entity-2 passes the floor and follows; entity-3 is cut.
    assert ids == ["entity-1", "entity-2"]
    assert res.results[1].final_score <= res.results[0].final_score


@pytest.mark.asyncio
async def test_load_failure_falls_back_to_the_floor(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    def broken(model_dir, device):
        raise cer.RerankUnavailable("no model")

    monkeypatch.setattr(cer, "load_reranker", broken)
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter)
    assert "entity-3" not in [r.uuid for r in res.results]
    assert [r.uuid for r in res.results][:2] == ["entity-1", "entity-2"]
    assert "rerank unavailable (RerankUnavailable)" in (res.note or "")


def test_order_by_rerank_keeps_unscored_after_with_capped_score():
    items = [SimpleNamespace(uuid=u, final_score=s) for u, s in (("a", 0.9), ("b", 0.8), ("c", 0.99))]
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Item:
        uuid: str
        final_score: float

    out = cer.order_by_rerank([Item(i.uuid, i.final_score) for i in items], {"a": -1.0, "b": 1.0})
    assert [i.uuid for i in out] == ["b", "a", "c"]
    assert out[2].final_score == pytest.approx(cer.sigmoid(-1.0))


def test_candidate_text():
    assert cer.candidate_text("Sky", "blue", "raw") == "Sky: blue"
    assert cer.candidate_text("Sky", None, "raw") == "Sky: raw"
    assert cer.candidate_text(None, None, "raw") == "raw"
    assert cer.candidate_text("Sky", "", None) == "Sky"


@pytest.mark.parametrize("device,available,expected", [
    ("cpu", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]),
    ("auto", ["CUDAExecutionProvider", "CPUExecutionProvider"],
     ["CUDAExecutionProvider", "CPUExecutionProvider"]),
    ("auto", ["CPUExecutionProvider"], ["CPUExecutionProvider"]),
    ("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"],
     ["CUDAExecutionProvider", "CPUExecutionProvider"]),
])
def test_select_providers(device, available, expected):
    assert cer.select_providers(device, available) == expected


def test_cuda_never_falls_back_and_bad_device_is_refused():
    with pytest.raises(cer.RerankUnavailable, match="CUDAExecutionProvider"):
        cer.select_providers("cuda", ["CPUExecutionProvider"])
    with pytest.raises(cer.RerankUnavailable, match="must be one of"):
        cer.select_providers("gpu", ["CPUExecutionProvider"])


def test_load_refuses_missing_dir_files_and_unpinned_model(tmp_path):
    with pytest.raises(cer.RerankUnavailable, match="MODEL_DIR is not set"):
        cer.load_reranker("", "cpu")
    with pytest.raises(cer.RerankUnavailable, match="must contain"):
        cer.load_reranker(str(tmp_path), "cpu")
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"not the pinned model")
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(cer.RerankUnavailable, match="not a pinned rerank model"):
        cer.load_reranker(str(tmp_path), "cpu")


def test_settings_map_to_tuning_and_service(monkeypatch):
    monkeypatch.setenv("MENHIR_RECALL_RERANK_ENABLED", "true")
    monkeypatch.setenv("MENHIR_RECALL_RERANK_MODEL_DIR", " /models/minilm ")
    monkeypatch.setenv("MENHIR_RECALL_RERANK_DEVICE", "CUDA")
    monkeypatch.setenv("MENHIR_RECALL_RERANK_TOP_N", "30")
    settings = MemorySettings.from_env()
    tuning = settings.retrieval_tuning()
    assert (tuning.enable_cross_encoder_rerank, tuning.rerank_top_n) == (True, 30)
    assert (settings.recall_rerank_model_dir, settings.recall_rerank_device) == ("/models/minilm", "cuda")


def test_default_on_but_inert_without_a_model_dir(monkeypatch):
    assert FEATURES["recall_rerank_enabled"].default is True
    assert MemorySettings().retrieval_tuning().enable_cross_encoder_rerank is True
    assert RetrievalTuningConfig().enable_cross_encoder_rerank is True
    assert RecallLabTuning().enable_cross_encoder_rerank is True

    def must_not_load(model_dir, device):
        raise AssertionError("no model dir: the reranker must not load")

    monkeypatch.setattr(cer, "load_reranker", must_not_load)
    # Server startup with the defaults (no model dir, maybe no extra) still boots.
    assert cer.preload_for_settings(MemorySettings()) is None
    assert cer.preload_for_settings(MemorySettings(recall_rerank_model_dir="  ")) is None


@pytest.mark.asyncio
async def test_default_recall_without_a_model_dir_keeps_the_floor_and_no_note(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    def must_not_load(model_dir, device):
        raise AssertionError("no model dir: the reranker must not load")

    monkeypatch.setattr(cer, "load_reranker", must_not_load)
    _pool(stub_graphiti_client, stub_memory_graph_adapter)
    svc = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
                        scoring_service=ScoringService())
    res = await svc.recall(QUERY, tuning=MemorySettings().retrieval_tuning())
    assert [r.uuid for r in res.results] == ["entity-1", "entity-2"]
    assert "rerank" not in (res.note or "")


def test_preload_fails_fast_when_a_model_dir_is_set_and_unloadable(tmp_path):
    with pytest.raises(cer.RerankUnavailable):
        cer.preload_for_settings(MemorySettings(recall_rerank_model_dir=str(tmp_path / "missing")))


def test_top_n_is_validated():
    with pytest.raises(ValueError, match="rerank_top_n"):
        RetrievalTuningConfig(rerank_top_n=0)
    with pytest.raises(ValueError):
        RecallLabTuning(rerank_top_n=201)
    assert RecallLabTuning(enable_cross_encoder_rerank=True).to_domain().enable_cross_encoder_rerank


async def _recall_capped(client, adapter, *, enabled, cap):
    _pool(client, adapter)
    svc = RecallService(graphiti_client=client, graph_adapter=adapter,
                        scoring_service=ScoringService(), rerank_model_dir="m")
    return await svc.recall(QUERY, tuning=RetrievalTuningConfig(
        enable_cross_encoder_rerank=enabled, rerank_max_results=cap))


@pytest.mark.asyncio
async def test_max_results_caps_a_reranked_list(stub_graphiti_client, stub_memory_graph_adapter, fake):
    res = await _recall_capped(stub_graphiti_client, stub_memory_graph_adapter, enabled=True, cap=2)
    assert [r.uuid for r in res.results] == ["entity-3", "entity-2"]


@pytest.mark.asyncio
async def test_max_results_leaves_the_floor_path_alone(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch,
):
    def broken(model_dir, device):
        raise cer.RerankUnavailable("no model")

    monkeypatch.setattr(cer, "load_reranker", broken)
    # Off, and on but unloadable: no rerank ran, so the request limit stands.
    for enabled in (False, True):
        res = await _recall_capped(stub_graphiti_client, stub_memory_graph_adapter, enabled=enabled, cap=1)
        assert [r.uuid for r in res.results] == ["entity-1", "entity-2"]


def test_max_results_default_env_and_validation(monkeypatch):
    assert MemorySettings().retrieval_tuning().rerank_max_results == 7
    assert RecallLabTuning().to_domain().rerank_max_results == 7
    monkeypatch.setenv("MENHIR_RECALL_RERANK_MAX_RESULTS", "4")
    assert MemorySettings.from_env().retrieval_tuning().rerank_max_results == 4
    with pytest.raises(ValueError, match="rerank_max_results"):
        RetrievalTuningConfig(rerank_max_results=0)
    with pytest.raises(ValueError):
        RecallLabTuning(rerank_max_results=201)



@pytest.mark.skipif(not os.environ.get("MENHIR_TEST_RERANK_MODEL_DIR"),
                    reason="set MENHIR_TEST_RERANK_MODEL_DIR to the pinned MiniLM model dir")
def test_real_model_ranks_the_relevant_passage_first():
    pytest.importorskip("onnxruntime")
    scorer = cer.load_reranker(os.environ["MENHIR_TEST_RERANK_MODEL_DIR"], "cpu")
    good, bad = scorer.score(QUERY, ["Sky: the sky is blue on a clear day", "Pasta: boil salted water"])
    assert good > 0 > bad


# --- pinned auto download (no MENHIR_RECALL_RERANK_MODEL_DIR) ---

class FakeHub:
    """Stands in for huggingface_hub.hf_hub_download: writes files into one snapshot dir."""

    def __init__(self, root, *, tokenizer=b'{"tok": 1}', fail=None):
        self.snapshot = root / "snapshots" / cer.PINNED_REVISION
        self.tokenizer = tokenizer
        self.fail = fail
        self.calls = []

    def __call__(self, repo_id, filename, **kw):
        self.calls.append((repo_id, filename, kw))
        if self.fail is not None:
            raise self.fail
        path = self.snapshot / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.tokenizer if filename == "tokenizer.json" else b"onnx")
        return str(path)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    import hashlib

    import huggingface_hub

    fake = FakeHub(tmp_path)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake)
    monkeypatch.setattr(cer, "PINNED_FILES", {
        "tokenizer.json": hashlib.sha256(fake.tokenizer).hexdigest(), "onnx/model.onnx": None})
    return fake


def _auto_settings(**kw):
    return MemorySettings(**{"recall_rerank_auto_download": True, **kw})


def test_fetch_returns_the_snapshot_dir_at_the_pinned_revision_without_a_token(hub):
    assert cer.fetch_pinned_model(local_files_only=False) == str(hub.snapshot)
    assert {f for _, f, _ in hub.calls} == {"tokenizer.json", "onnx/model.onnx"}
    for repo, _, kw in hub.calls:
        assert (repo, kw["revision"], kw["token"], kw["local_files_only"]) == (
            cer.PINNED_REPO, cer.PINNED_REVISION, False, False)


def test_fetch_refuses_a_tokenizer_that_does_not_match_its_pin(hub):
    hub.tokenizer = b"swapped"
    with pytest.raises(cer.RerankUnavailable, match="pinned sha256"):
        cer.fetch_pinned_model(local_files_only=False)


def test_fetch_failure_is_rerank_unavailable(hub):
    hub.fail = OSError("offline")
    with pytest.raises(cer.RerankUnavailable, match="could not download"):
        cer.fetch_pinned_model(local_files_only=False)


def test_hooks_only_use_a_cached_model_and_never_download(hub):
    assert cer.cached_model_dir_for_settings(_auto_settings()) == str(hub.snapshot)
    assert all(kw["local_files_only"] is True for _, _, kw in hub.calls)
    hub.fail = OSError("not cached")
    assert cer.cached_model_dir_for_settings(_auto_settings()) == ""
    assert cer.cached_model_dir_for_settings(_auto_settings(recall_rerank_model_dir="/m")) == "/m"
    assert cer.cached_model_dir_for_settings(_auto_settings(recall_rerank_auto_download=False)) == ""
    assert cer.cached_model_dir_for_settings(_auto_settings(recall_rerank_enabled=False)) == ""


@pytest.mark.parametrize("kw", [
    {"recall_rerank_enabled": False},
    {"recall_rerank_model_dir": "/explicit"},
    {"recall_rerank_auto_download": False},
])
def test_no_background_fetch_when_off_explicit_or_opted_out(hub, kw):
    target = SimpleNamespace(rerank_model_dir="")
    assert cer.start_model_fetch(_auto_settings(**kw), target) is None
    assert hub.calls == []


def test_background_fetch_sets_the_dir_only_after_a_verified_load(hub, monkeypatch):
    loaded = []
    monkeypatch.setattr(cer, "load_reranker", lambda d, dev: loaded.append((d, dev)))
    target = SimpleNamespace(rerank_model_dir="")
    cer.start_model_fetch(_auto_settings(recall_rerank_device="cpu"), target).join(10)
    assert loaded == [(str(hub.snapshot), "cpu")]
    assert target.rerank_model_dir == str(hub.snapshot)


@pytest.mark.parametrize("failure", ["download", "load", "crash"])
def test_background_fetch_failure_keeps_the_floor_and_never_raises(hub, monkeypatch, failure):
    if failure == "download":
        hub.fail = OSError("offline")

    def load(d, dev):
        if failure == "load":
            raise cer.RerankUnavailable("unpinned")
        if failure == "crash":
            raise RuntimeError("boom")

    monkeypatch.setattr(cer, "load_reranker", load)
    target = SimpleNamespace(rerank_model_dir="")
    cer.start_model_fetch(_auto_settings(), target).join(10)
    assert target.rerank_model_dir == ""


def test_auto_download_setting_defaults_on_and_reads_env(monkeypatch):
    assert MemorySettings().recall_rerank_auto_download is True
    monkeypatch.setenv("MENHIR_RECALL_RERANK_AUTO_DOWNLOAD", "false")
    assert MemorySettings.from_env().recall_rerank_auto_download is False


def test_pinned_tokenizer_digest_matches_the_pinned_snapshot():
    model_dir = os.environ.get("MENHIR_TEST_RERANK_MODEL_DIR")
    if not model_dir:
        pytest.skip("set MENHIR_TEST_RERANK_MODEL_DIR to the pinned MiniLM model dir")
    from pathlib import Path

    assert cer._sha256(Path(model_dir) / "tokenizer.json") == cer.PINNED_FILES["tokenizer.json"]
