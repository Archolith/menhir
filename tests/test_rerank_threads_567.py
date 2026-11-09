"""#567: the recall reranker's ONNX thread pool follows MENHIR_RECALL_RERANK_THREADS.

Production ran a 1-CPU cgroup quota with 6 visible cores; ONNX Runtime sized its pool to 6, the
quota throttled it, and 50 pairs took ~15 s instead of ~6 s with one thread.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from types import SimpleNamespace

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.services import cross_encoder_rerank as cer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(cer, "_thread_setting", 0)
    monkeypatch.setattr(cer, "_auto_threads", [])
    monkeypatch.setattr(cer, "_CACHE", {})


# --- choosing the count ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("setting", "limit", "cpus", "expected"),
    [
        (3, 1.0, 6, 3),        # explicit wins, even above the quota
        (0, 1.0, 6, 1),        # the production shape
        (0, 1.5, 6, 1),        # floor: a second thread would be throttled
        (0, 0.5, 6, 1),        # never zero
        (0, 2.0, 6, 2),
        (0, 6.0, 6, None),     # quota not below the cores: keep onnxruntime's default
        (0, 8.0, 6, None),
        (0, None, 6, None),    # no quota: unchanged behaviour
    ],
)
def test_resolve_threads(setting, limit, cpus, expected):
    assert cer.resolve_threads(setting, limit=limit, cpus=cpus) == expected


def test_cgroup_v2_quota(tmp_path):
    (tmp_path / "cpu.max").write_text("100000 100000\n")
    assert cer.cgroup_cpu_limit(tmp_path) == 1.0
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert cer.cgroup_cpu_limit(tmp_path) == 1.5


def test_cgroup_v2_unlimited_is_none(tmp_path):
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert cer.cgroup_cpu_limit(tmp_path) is None


def test_cgroup_v1_quota(tmp_path):
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("200000\n")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert cer.cgroup_cpu_limit(tmp_path) == 2.0
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")
    assert cer.cgroup_cpu_limit(tmp_path) is None


@pytest.mark.parametrize("text", ["", "garbage", "100000 0", "0 100000"])
def test_cgroup_unreadable_or_odd_is_none(tmp_path, text):
    (tmp_path / "cpu.max").write_text(text)
    assert cer.cgroup_cpu_limit(tmp_path) is None


def test_no_cgroup_is_none(tmp_path):
    assert cer.cgroup_cpu_limit(tmp_path / "absent") is None


def test_auto_reads_the_cgroup_once(monkeypatch):
    reads = []
    monkeypatch.setattr(cer, "cgroup_cpu_limit", lambda: reads.append(1) or 1.0)
    monkeypatch.setattr(cer, "visible_cpus", lambda: 6)
    assert cer.current_threads() == 1
    assert cer.current_threads() == 1
    assert reads == [1]


def test_configured_setting_overrides_auto(monkeypatch):
    monkeypatch.setattr(cer, "cgroup_cpu_limit", lambda: 1.0)
    monkeypatch.setattr(cer, "visible_cpus", lambda: 6)
    cer.configure_threads(4)
    assert cer.current_threads() == 4
    cer.configure_threads(-2)  # clamped: negative means auto
    assert cer.current_threads() == 1


# --- the session gets it --------------------------------------------------------------------

class _Ort:
    class SessionOptions:
        pass

    def __init__(self):
        self.options = []

    def get_available_providers(self):
        return ["CPUExecutionProvider"]

    def InferenceSession(self, model, options, providers):  # noqa: N802 - onnxruntime's name
        self.options.append(options)
        return SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name="input_ids")])


class _Tok:
    @classmethod
    def from_str(cls, text):
        return cls()

    def enable_truncation(self, *args, **kwargs):
        pass

    def no_padding(self):
        pass


@pytest.fixture
def model_dir(monkeypatch, tmp_path):
    model, tok = b"pinned onnx", b'{"pinned": "tokenizer"}'
    (tmp_path / "model.onnx").write_bytes(model)
    (tmp_path / "tokenizer.json").write_bytes(tok)
    key = hashlib.sha256(model).hexdigest()
    monkeypatch.setattr(cer, "PINNED_MODELS", {key: "fake pinned"})
    monkeypatch.setattr(cer, "PINNED_TOKENIZERS", {key: hashlib.sha256(tok).hexdigest()})
    ort = _Ort()
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=_Tok))
    return SimpleNamespace(path=str(tmp_path), ort=ort)


def test_session_gets_the_capped_pool(model_dir, monkeypatch, caplog):
    monkeypatch.setattr(cer, "cgroup_cpu_limit", lambda: 1.0)
    monkeypatch.setattr(cer, "visible_cpus", lambda: 6)
    with caplog.at_level(logging.INFO, logger=cer.__name__):
        cer.load_reranker(model_dir.path, "cpu")
    (options,) = model_dir.ort.options
    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert "1 intra-op thread(s)" in caplog.text


def test_no_quota_leaves_onnxruntime_defaults(model_dir, monkeypatch):
    monkeypatch.setattr(cer, "cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(cer, "visible_cpus", lambda: 6)
    cer.load_reranker(model_dir.path, "cpu")
    (options,) = model_dir.ort.options
    assert not hasattr(options, "intra_op_num_threads")
    assert not hasattr(options, "inter_op_num_threads")


def test_thread_count_is_part_of_the_cache_key(model_dir, monkeypatch):
    monkeypatch.setattr(cer, "cgroup_cpu_limit", lambda: None)
    cer.configure_threads(2)
    first = cer.load_reranker(model_dir.path, "cpu")
    assert cer.load_reranker(model_dir.path, "cpu") is first
    cer.configure_threads(3)
    second = cer.load_reranker(model_dir.path, "cpu")
    assert second is not first
    assert [o.intra_op_num_threads for o in model_dir.ort.options] == [2, 3]


# --- settings and startup -------------------------------------------------------------------

def test_setting_defaults_to_auto_and_parses(monkeypatch):
    monkeypatch.delenv("MENHIR_RECALL_RERANK_THREADS", raising=False)
    assert MemorySettings.from_env().recall_rerank_threads == 0
    monkeypatch.setenv("MENHIR_RECALL_RERANK_THREADS", "2")
    assert MemorySettings.from_env().recall_rerank_threads == 2


def test_preload_configures_threads_before_loading(monkeypatch):
    seen = []
    monkeypatch.setattr(cer, "load_reranker", lambda d, dev: seen.append(cer.current_threads()))
    cer.preload_for_settings(MemorySettings(recall_rerank_model_dir="/models/x", recall_rerank_threads=3))
    assert seen == [3]


def test_ready_line_names_the_thread_count(monkeypatch, caplog):
    cer.configure_threads(2)
    monkeypatch.setattr(cer, "fetch_pinned_model", lambda **kw: "/cache/snap")
    monkeypatch.setattr(cer, "load_reranker", lambda d, dev: None)
    monkeypatch.setattr(cer, "_fetch_thread", None)
    monkeypatch.setattr(cer, "_fetch_targets", [])
    target = SimpleNamespace(rerank_model_dir="")
    settings = MemorySettings(recall_rerank_enabled=True, recall_rerank_model_dir="", recall_rerank_auto_download=True)
    with caplog.at_level(logging.INFO, logger=cer.__name__):
        cer.start_model_fetch(settings, target).join(10)
    assert target.rerank_model_dir == "/cache/snap"
    assert "recall rerank ready" in caplog.text and "2 intra-op thread(s)" in caplog.text
