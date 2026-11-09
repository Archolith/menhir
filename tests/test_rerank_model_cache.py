"""The rerank model cache persists by default (follow-up to #567).

The image set HOME=/tmp, so huggingface_hub cached the model under /tmp, which a read-only
deployment mounts as tmpfs: every restart downloaded ~92 MB again. The image now points HF_HOME
at an app-owned VOLUME, MENHIR_RECALL_RERANK_CACHE_DIR overrides it, and the server warns when
the cache still lands on tmpfs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.services import cross_encoder_rerank as cer

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(cer, "_fetch_thread", None)
    monkeypatch.setattr(cer, "_fetch_targets", [])
    monkeypatch.setattr(cer, "load_reranker", lambda d, dev: None)


def _auto(**overrides) -> MemorySettings:
    return MemorySettings(recall_rerank_enabled=True, recall_rerank_model_dir="",
                          recall_rerank_auto_download=True, **overrides)


# --- setting -------------------------------------------------------------------------------

def test_cache_dir_setting_reads_env(monkeypatch):
    monkeypatch.delenv("MENHIR_RECALL_RERANK_CACHE_DIR", raising=False)
    assert MemorySettings.from_env().recall_rerank_cache_dir == ""
    monkeypatch.setenv("MENHIR_RECALL_RERANK_CACHE_DIR", "  /data/hf  ")
    assert MemorySettings.from_env().recall_rerank_cache_dir == "/data/hf"


@pytest.mark.parametrize(("value", "expected"), [("", None), ("   ", None), ("/data/hf", "/data/hf")])
def test_empty_cache_dir_means_the_huggingface_default(value, expected):
    assert cer.cache_dir_for_settings(_auto(recall_rerank_cache_dir=value)) == expected


# --- both fetch paths use it ---------------------------------------------------------------

def test_server_fetch_downloads_into_the_configured_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(cer, "fetch_pinned_model", lambda **kw: calls.append(kw) or "/data/hf/snap")
    target = SimpleNamespace(rerank_model_dir="")
    cer.start_model_fetch(_auto(recall_rerank_cache_dir="/data/hf"), target).join(10)
    assert target.rerank_model_dir == "/data/hf/snap"
    assert calls == [{"local_files_only": False, "force_download": False, "cache_dir": "/data/hf"}]


def test_digest_retry_downloads_into_the_same_cache(monkeypatch):
    calls = []

    def fetch(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise cer.RerankDigestMismatch("cached copy is not the pinned one")
        return "/data/hf/snap"

    monkeypatch.setattr(cer, "fetch_pinned_model", fetch)
    cer.start_model_fetch(_auto(recall_rerank_cache_dir="/data/hf"), SimpleNamespace(rerank_model_dir="")).join(10)
    assert [(c["force_download"], c["cache_dir"]) for c in calls] == [(False, "/data/hf"), (True, "/data/hf")]


def test_hook_lookup_reads_the_configured_cache_without_downloading(monkeypatch):
    calls = []
    monkeypatch.setattr(cer, "fetch_pinned_model", lambda **kw: calls.append(kw) or "/data/hf/snap")
    assert cer.cached_model_dir_for_settings(_auto(recall_rerank_cache_dir="/data/hf")) == "/data/hf/snap"
    assert calls == [{"local_files_only": True, "cache_dir": "/data/hf"}]


# --- tmpfs detection -----------------------------------------------------------------------

MOUNTS = "\n".join([
    "overlay / overlay rw,relatime 0 0",
    "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0",
    "/dev/sda1 /tmp/persist ext4 rw,relatime 0 0",
    "/dev/sda1 /srv/menhir/cache/huggingface ext4 rw,relatime 0 0",
    r"tmpfs /mnt/with\040space tmpfs rw 0 0",
]) + "\n"


@pytest.fixture
def mounts(tmp_path) -> str:
    path = tmp_path / "mounts"
    path.write_text(MOUNTS, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(("path", "expected"), [
    ("/tmp/.cache/huggingface/hub/snap", "/tmp"),
    ("/tmp", "/tmp"),
    ("/tmp/persist/hub/snap", None),          # a deeper non-tmpfs mount wins
    ("/tmpx/hub/snap", None),                 # a name prefix is not a path prefix
    ("/srv/menhir/cache/huggingface/hub/snap", None),
    ("/mnt/with space/hub", "/mnt/with space"),  # octal-escaped mount point
])
def test_tmpfs_mount_for(mounts, path, expected):
    # resolve=False: compare the paths as given, so the test holds on any CI host.
    assert cer.tmpfs_mount_for(path, mounts, resolve=False) == expected


def test_tmpfs_mount_for_without_a_mount_table(tmp_path):
    assert cer.tmpfs_mount_for("/tmp/x", str(tmp_path / "missing")) is None


@pytest.mark.parametrize(("mount", "warned"), [("/tmp", True), (None, False)])
def test_server_warns_once_ready_when_the_cache_is_on_tmpfs(monkeypatch, caplog, mount, warned):
    monkeypatch.setattr(cer, "fetch_pinned_model", lambda **kw: "/tmp/.cache/snap")
    monkeypatch.setattr(cer, "tmpfs_mount_for", lambda path: mount)
    with caplog.at_level(logging.INFO, logger=cer.__name__):
        cer.start_model_fetch(_auto(), SimpleNamespace(rerank_model_dir="")).join(10)
    assert "recall rerank ready" in caplog.text
    assert ("model cache is on tmpfs" in caplog.text) is warned


# --- image default -------------------------------------------------------------------------

def test_image_caches_the_model_in_an_app_owned_volume():
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile[dockerfile.index(" AS runtime"):]
    cache = "/srv/menhir/cache/huggingface"
    assert f"HF_HOME={cache}" in runtime
    assert "HF_HUB_DISABLE_TELEMETRY=1" in runtime
    created = runtime.index(f"        {cache} \\")
    owned = runtime.index("chown -R menhir:menhir", created)
    assert "/srv/menhir/cache" in runtime[owned:runtime.index("\n", owned)]
    # Content written after a VOLUME line is discarded, so the volume follows the chown.
    assert runtime.index(f'VOLUME ["{cache}"]') > owned
