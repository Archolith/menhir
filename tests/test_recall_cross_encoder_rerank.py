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

    def from_cache(repo_id, filename, **kw):  # the cache-only lookup: whatever a download wrote
        fake.cache_lookups.append((repo_id, filename, kw))
        path = fake.snapshot / filename
        return str(path) if path.is_file() else None

    fake.cache_lookups = []
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", from_cache)
    monkeypatch.setattr(cer, "PINNED_FILES", {
        "tokenizer.json": hashlib.sha256(fake.tokenizer).hexdigest(), "onnx/model.onnx": None})
    return fake


def _auto_settings(**kw):
    return MemorySettings(**{"recall_rerank_auto_download": True, **kw})


def test_fetch_returns_the_snapshot_dir_at_the_pinned_revision_without_a_token(hub):
    assert cer.fetch_pinned_model(local_files_only=False) == str(hub.snapshot)
    assert {f for _, f, _ in hub.calls} == {"tokenizer.json", "onnx/model.onnx"}
    for repo, _, kw in hub.calls:
        assert (repo, kw["revision"], kw["token"]) == (cer.PINNED_REPO, cer.PINNED_REVISION, False)


def test_fetch_refuses_a_tokenizer_that_does_not_match_its_pin(hub):
    hub.tokenizer = b"swapped"
    with pytest.raises(cer.RerankUnavailable, match="pinned sha256"):
        cer.fetch_pinned_model(local_files_only=False)


def test_fetch_failure_is_rerank_unavailable(hub):
    hub.fail = OSError("offline")
    with pytest.raises(cer.RerankUnavailable, match="could not download"):
        cer.fetch_pinned_model(local_files_only=False)


def test_hooks_only_use_a_cached_model_and_never_download(hub):
    assert cer.cached_model_dir_for_settings(_auto_settings()) == ""  # nothing cached yet
    cer.fetch_pinned_model(local_files_only=False)  # a server downloaded it
    hub.calls.clear()
    assert cer.cached_model_dir_for_settings(_auto_settings()) == str(hub.snapshot)
    assert hub.calls == []  # hooks never reach hf_hub_download
    assert {(r, kw["revision"]) for r, _, kw in hub.cache_lookups} == {(cer.PINNED_REPO, cer.PINNED_REVISION)}
    (hub.snapshot / "tokenizer.json").write_bytes(b"swapped")  # a cached file that fails its pin
    assert cer.cached_model_dir_for_settings(_auto_settings()) == ""
    assert cer.cached_model_dir_for_settings(_auto_settings(recall_rerank_model_dir="/m")) == "/m"
    assert cer.cached_model_dir_for_settings(_auto_settings(recall_rerank_auto_download=False)) == ""
    assert cer.cached_model_dir_for_settings(_auto_settings(recall_rerank_enabled=False)) == ""


def test_hook_lookup_opens_no_network_connection_even_online_inside_an_agent_harness(
    monkeypatch, tmp_path,
):
    """SEC-01: hf_hub_download(local_files_only=True) still fetched /api/agent-harnesses for its
    User-Agent. Real huggingface_hub, offline mode off, no cached harness registry, sockets
    recorded and refused: the cached-model lookup must not try to connect at all."""
    import hashlib
    import socket

    from huggingface_hub import constants

    tok = b'{"tok": 2}'
    snap = tmp_path / "hub" / "models--cross-encoder--ms-marco-MiniLM-L6-v2" / "snapshots" / cer.PINNED_REVISION
    (snap / "onnx").mkdir(parents=True)
    (snap / "tokenizer.json").write_bytes(tok)
    (snap / "onnx" / "model.onnx").write_bytes(b"onnx")
    monkeypatch.setattr(cer, "PINNED_FILES", {
        "tokenizer.json": hashlib.sha256(tok).hexdigest(), "onnx/model.onnx": None})
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(constants, "AGENT_HARNESSES_PATH", str(tmp_path / "no-registry.json"), raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test")
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network blocked in test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert cer.fetch_pinned_model(local_files_only=True) == str(snap)
    assert attempts == []


@pytest.mark.parametrize("kw", [
    {"recall_rerank_enabled": False},
    {"recall_rerank_model_dir": "/explicit"},
    {"recall_rerank_auto_download": False},
])
def test_no_background_fetch_when_off_explicit_or_opted_out(hub, kw):
    target = SimpleNamespace(rerank_model_dir="")
    assert cer.start_model_fetch(_auto_settings(**kw), target) is None
    assert hub.calls == []


def test_no_background_fetch_without_a_recall_service(hub):
    assert cer.start_model_fetch(_auto_settings(), None) is None
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


# --- security audit Low findings (.agent/reviews/menhir-552-rerank-security-audit-results.md) ---

class _FakeOrt:
    """Records what onnxruntime is given; enough for load_reranker."""

    class SessionOptions:
        pass

    def __init__(self):
        self.telemetry_off = 0
        self.models = []

    def disable_telemetry_events(self):
        self.telemetry_off += 1

    def get_available_providers(self):
        return ["CPUExecutionProvider"]

    def InferenceSession(self, model, options, providers):  # noqa: N802 - onnxruntime's name
        self.models.append(model)
        return SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name="input_ids")])


class _FakeTokenizer:
    loaded: list[str] = []

    @classmethod
    def from_str(cls, text):
        cls.loaded.append(text)
        return cls()

    def enable_truncation(self, *args, **kwargs):
        pass

    def no_padding(self):
        pass


@pytest.fixture
def pinned_dir(monkeypatch, tmp_path):
    import hashlib
    import sys

    model, tok = b"pinned onnx", b'{"pinned": "tokenizer"}'
    (tmp_path / "model.onnx").write_bytes(model)
    (tmp_path / "tokenizer.json").write_bytes(tok)
    key = hashlib.sha256(model).hexdigest()
    monkeypatch.setattr(cer, "PINNED_MODELS", {key: "fake pinned"})
    monkeypatch.setattr(cer, "PINNED_TOKENIZERS", {key: hashlib.sha256(tok).hexdigest()})
    ort = _FakeOrt()
    _FakeTokenizer.loaded = []
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=_FakeTokenizer))
    return SimpleNamespace(path=tmp_path, model=model, tok=tok, ort=ort)


def test_explicit_dir_refuses_a_tokenizer_that_is_not_pinned_to_the_model(pinned_dir):
    """SEC-04: load_reranker used to hash only model.onnx."""
    (pinned_dir.path / "tokenizer.json").write_bytes(b'{"swapped": true}')
    with pytest.raises(cer.RerankDigestMismatch, match="not the tokenizer pinned"):
        cer.load_reranker(str(pinned_dir.path), "cpu")
    assert pinned_dir.ort.models == []


def test_load_builds_from_the_bytes_it_hashed_and_turns_ort_telemetry_off(pinned_dir):
    """SEC-03/09: the session and tokenizer get the checked bytes, never a path to reopen."""
    scorer = cer.load_reranker(str(pinned_dir.path), "cpu")
    assert scorer.label == "fake pinned"
    assert pinned_dir.ort.models == [pinned_dir.model]
    assert _FakeTokenizer.loaded == [pinned_dir.tok.decode()]
    assert pinned_dir.ort.telemetry_off == 1


def test_load_keeps_no_model_copy_and_no_cpu_fallback(pinned_dir, monkeypatch):
    """Memory: ORT's wrapper holds the model bytes for the session's life (~90 MB). Fallback off
    keeps cuda from silently moving to CPU, and is what makes dropping the bytes safe."""

    class Session:
        def __init__(self, model, options, providers):
            self._model_bytes = model
            self.fallback = True

        def disable_fallback(self):
            self.fallback = False

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids")]

    monkeypatch.setattr(pinned_dir.ort, "InferenceSession", Session, raising=False)
    session = cer.load_reranker(str(pinned_dir.path), "cpu").session
    assert session.fallback is False
    assert session._model_bytes is None


def test_a_read_error_while_checking_a_file_is_rerank_unavailable(hub, monkeypatch):
    """SEC-12: an OSError from hashing escaped fetch_pinned_model and broke hook recall."""
    from pathlib import Path

    cer.fetch_pinned_model(local_files_only=False)
    real = Path.read_bytes

    def failing(self):
        if self.name == "tokenizer.json":
            raise OSError("I/O error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    with pytest.raises(cer.RerankUnavailable, match="could not read"):
        cer.fetch_pinned_model(local_files_only=True)
    assert cer.cached_model_dir_for_settings(_auto_settings()) == ""


def test_download_sends_its_own_user_agent(hub):
    """SEC-02: the default hub User-Agent names the agent harness and library versions."""
    cer.fetch_pinned_model(local_files_only=False)
    assert {kw["headers"]["user-agent"] for _, _, kw in hub.calls} == {cer.DOWNLOAD_USER_AGENT}


def _redownload_hub(hub, monkeypatch, *, repaired):
    import huggingface_hub

    good, hub.tokenizer = hub.tokenizer, b"corrupt"

    def download(repo_id, filename, **kw):
        if kw.get("force_download") and repaired:
            hub.tokenizer = good
        existing = hub.snapshot / filename
        if kw.get("force_download") and existing.is_file():
            # Like huggingface_hub: a forced download refreshes the blob but keeps an existing
            # snapshot entry, a plain copy where symlinks are unavailable.
            hub.calls.append((repo_id, filename, kw))
            return str(existing)
        return hub(repo_id, filename, **kw)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    monkeypatch.setattr(cer, "load_reranker", lambda d, dev: None)


def test_a_cached_copy_failing_its_pin_is_downloaded_again_once(hub, monkeypatch):
    """SEC-11: a bad cached file used to leave the server on the floor until someone cleared it."""
    _redownload_hub(hub, monkeypatch, repaired=True)
    target = SimpleNamespace(rerank_model_dir="")
    cer.start_model_fetch(_auto_settings(), target).join(10)
    assert target.rerank_model_dir == str(hub.snapshot)
    assert [kw.get("force_download") for _, _, kw in hub.calls].count(True) == 2  # both files, once


def test_a_redownload_that_still_fails_its_pin_gives_up(hub, monkeypatch):
    _redownload_hub(hub, monkeypatch, repaired=False)
    target = SimpleNamespace(rerank_model_dir="")
    cer.start_model_fetch(_auto_settings(), target).join(10)
    assert target.rerank_model_dir == ""
    assert [kw.get("force_download") for _, _, kw in hub.calls].count(True) == 1  # tokenizer, then stop


def test_a_second_start_joins_the_running_fetch_and_both_services_get_the_model(hub, monkeypatch):
    """SEC-15: each runtime-init retry used to start another fetch thread."""
    import threading

    gate, loads = threading.Event(), []

    def load(model_dir, device):
        loads.append(model_dir)
        assert gate.wait(10)

    monkeypatch.setattr(cer, "load_reranker", load)
    first, second = SimpleNamespace(rerank_model_dir=""), SimpleNamespace(rerank_model_dir="")
    thread = cer.start_model_fetch(_auto_settings(), first)
    assert cer.start_model_fetch(_auto_settings(), second) is thread
    gate.set()
    thread.join(10)
    assert loads == [str(hub.snapshot)]
    assert first.rerank_model_dir == second.rerank_model_dir == str(hub.snapshot)
    later = cer.start_model_fetch(_auto_settings(), SimpleNamespace(rerank_model_dir=""))
    assert later is not thread  # a finished fetch does not block a new one
    later.join(10)


def test_a_fetch_thread_that_fails_to_start_does_not_block_later_starts(hub, monkeypatch):
    import threading

    monkeypatch.setattr(cer, "load_reranker", lambda d, dev: None)
    real_start = threading.Thread.start

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError):
        cer.start_model_fetch(_auto_settings(), SimpleNamespace(rerank_model_dir=""))
    monkeypatch.setattr(threading.Thread, "start", real_start)
    target = SimpleNamespace(rerank_model_dir="")
    cer.start_model_fetch(_auto_settings(), target).join(10)
    assert target.rerank_model_dir == str(hub.snapshot)


def test_scoring_runs_one_at_a_time():
    """SEC-06: concurrent recalls queue for the scorer instead of oversubscribing the CPU."""
    import threading
    import time

    import numpy as np

    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class Session:
        def run(self, _outputs, feed):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return [np.zeros((len(feed["input_ids"]), 1))]

    class Tok:
        def encode(self, query, passage):
            return SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 1])

    scorer = cer.OnnxCrossEncoder(session=Session(), tokenizer=Tok(),
                                  input_names=frozenset({"input_ids"}), label="t", device="cpu")
    threads = [threading.Thread(target=scorer.score, args=(QUERY, ["a", "b"])) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert state["peak"] == 1


def test_recall_lab_caps_rerank_top_n_at_100():
    """SEC-05: Lab runs many arms per request, so it gets a lower cap than the setting."""
    from pydantic import ValidationError

    assert RecallLabTuning(rerank_top_n=100).rerank_top_n == 100
    with pytest.raises(ValidationError):
        RecallLabTuning(rerank_top_n=101)


def test_disabled_rerank_gives_no_model_dir_even_when_one_is_set(monkeypatch):
    """SEC-07: tunings that default the rerank on (Lab, view-entropy) must stay off when it is off."""
    from menhir.core.bootstrap import build_memory_services

    assert cer.cached_model_dir_for_settings(
        _auto_settings(recall_rerank_enabled=False, recall_rerank_model_dir="/m")) == ""

    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    class FakeGraphiti:
        @classmethod
        def from_settings_with_capabilities(cls, settings, **kwargs):
            return SimpleNamespace()

    class FakeLLM:
        @classmethod
        def from_settings(cls, settings):
            return SimpleNamespace()

    captured = {}

    class FakeRecall:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", Inert)
    monkeypatch.setattr("menhir.core.bootstrap.MemoryGraphAdapter", Inert)
    monkeypatch.setattr("menhir.core.bootstrap.GraphitiClient", FakeGraphiti)
    monkeypatch.setattr("menhir.core.bootstrap.LLMAdapter", FakeLLM)
    monkeypatch.setattr("menhir.core.bootstrap.RecallService", FakeRecall)
    monkeypatch.setattr(cer, "preload_for_settings", lambda settings: None)
    build_memory_services(settings=MemorySettings(recall_rerank_enabled=False, recall_rerank_model_dir="/m"))
    assert captured["rerank_model_dir"] == ""
    build_memory_services(settings=MemorySettings(recall_rerank_model_dir="/m"))
    assert captured["rerank_model_dir"] == "/m"
