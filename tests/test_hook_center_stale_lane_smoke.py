"""Unit tests for the Hook Center stale-anchor lane smoke harness.

These test the SMOKE SCRIPT's behavior (orchestration, output, exit codes, safety),
not backend correctness. No Neo4j and no real server: the HTTP client is mocked via
``urllib`` and the in-process backend is replaced with a scripted fake.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_smoke():
    path = (Path(__file__).resolve().parents[1]
            / "scripts" / "smoke" / "hook_center_stale_lane_smoke.py")
    spec = importlib.util.spec_from_file_location("hook_center_stale_lane_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- HTTP fake


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    def read(self):
        return json.dumps(self._data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeUrlopen:
    """Route by URL + method. Records POST bodies for inspection."""

    def __init__(self, *, stale_resp=None, malformed=False):
        self.posts: list[tuple[str, dict]] = []
        self.malformed = malformed
        self.stale_resp = stale_resp or {
            "stale_anchors": [{"memory_uuid": "smoke-memory-001", "path": "src/smoke_target.py",
                               "dirty_at": "2026-07-09T00:00:00Z", "anchored_at": "2026-07-01T00:00:00Z"}],
            "count": 1,
        }

    def __call__(self, req, **kw):
        import urllib.error
        url = str(getattr(req, "full_url", req))
        method = getattr(req, "method", "GET")
        data = getattr(req, "data", None)
        body = json.loads(data.decode("utf-8")) if data else None
        if body is not None:
            self.posts.append((url, body))

        if "/stale-verifications" in url:
            if method == "POST":
                if body and body.get("verified_at") == "not-a-timestamp":
                    raise urllib.error.HTTPError(url, 400, "bad verified_at", {}, None)
                return _FakeResp({"accepted": True, "verification": {"uuid": "v1", **(body or {})}})
            return _FakeResp({"verifications": [{"path": "src/smoke_target.py",
                                                 "outcome": "still_valid"}], "count": 1})
        if "/tool-events/stale" in url:
            if self.malformed:
                return _BadJSON()
            return _FakeResp(self.stale_resp)
        if "/tool-events/dirty" in url:
            return _FakeResp({"dirty_files": [{"path": "src/smoke_target.py",
                                               "dirty_at": "2026-07-09T00:00:00Z",
                                               "operation": "edit"}],
                              "stale_anchors": [], "counts": {}})
        if url.endswith("/tool-events"):
            return _FakeResp({"accepted": True, "marked_dirty": True})
        return _FakeResp({})


class _BadJSON:
    def read(self):
        return b"<html>not json</html>"

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --------------------------------------------------------------------------- backend fake


def _item(uuid, stale, *, verification=None, action=None):
    info = {"stale_anchor": stale}
    fmt = {"stale_anchor": stale}
    if stale:
        info.update(stale_reason="file_changed_after_anchor", path="src/smoke_target.py",
                    dirty_at="2026-07-09T00:00:00Z", anchored_at="2026-07-01T00:00:00Z")
        fmt.update(stale_action=action or "verify_current_file_before_relying",
                   stale_advisory="Inspect the current file before relying on it.")
        if verification is not None:
            info["stale_verification"] = verification
    return {"uuid": uuid, "stale_anchor_info": info, "formatted": fmt}


class FakeBackend:
    """Scripted in-process backend. Returns a canned recall progression that matches
    the smoke's call order for a full PASS."""

    # Set from the loaded module in _wire so fixtures match the script's constants.
    SENTINEL = "smoke-lane-memory-body-sentinel-7c1f"
    EVENT_HASH = "smoke-synthetic-hash"

    def __init__(self, recall_sequence=None):
        self._seq = list(recall_sequence) if recall_sequence is not None else self._default_seq()
        self._i = 0
        self.clean_calls = 0

    @staticmethod
    def _default_seq():
        ctrl = _item("smoke-memory-control-001", False)
        return [
            [_item("smoke-memory-001", True), ctrl],                       # #4/#5
            [_item("smoke-memory-001", True), ctrl],                       # #9/#10 (no enrich)
            [_item("smoke-memory-001", True,
                   verification={"outcome": "still_valid", "verified_by": "smoke-agent",
                                 "basis": "inspected_current_file"}), ctrl],  # #8
            [_item("smoke-memory-001", True,
                   verification={"outcome": "outdated"},
                   action="do_not_rely_update_or_supersede"), ctrl],      # #12
        ]

    def ping(self):
        return True

    def any_smoke_data(self, project, uuids):
        return False

    def clean(self, project, uuids):
        self.clean_calls += 1

    def create_fixture(self, *a, **k):
        pass

    def file_event_metadata(self, project, path):
        return {"operation": "edit", "after_hash": self.EVENT_HASH}

    async def recall_items(self, project, hits):
        out = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return out

    async def build_context(self, project, hits, max_tokens):
        if max_tokens <= 1:
            return ""  # atomic omission: neither memory body nor warning
        return (f"[Memory 1] {self.SENTINEL}: body. "
                "Stale file anchor: src/smoke_target.py changed after ... "
                "inspect the current file before relying.")

    def memory_exists(self, uuid):
        return True

    def dirty_flag_set(self, project, path):
        return True

    def stale_count(self, project):
        return 1

    def close(self):
        pass


def _wire(mod, monkeypatch, backend=None, urlopen=None):
    # Keep the fake fixtures aligned with the script's own constants.
    FakeBackend.SENTINEL = mod.MEMORY_SENTINEL
    FakeBackend.EVENT_HASH = mod.EVENT_HASH
    monkeypatch.setattr(mod, "SERVER_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_maybe_spawn_server", lambda config: None)
    monkeypatch.setattr(mod, "_make_backend", lambda config: backend or FakeBackend())
    monkeypatch.setattr("urllib.request.urlopen", urlopen or _FakeUrlopen())


_BASE_ARGV = ["--no-self-serve", "--url", "http://x:8099"]


# --------------------------------------------------------------------------- tests


def test_full_pass(monkeypatch):
    mod = _load_smoke()
    _wire(mod, monkeypatch)
    assert mod.main(_BASE_ARGV) == 0


def test_json_output_is_parseable_json_only(monkeypatch, capsys):
    mod = _load_smoke()
    _wire(mod, monkeypatch)
    rc = mod.main(_BASE_ARGV + ["--json"])
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)  # must be pure JSON on stdout
    assert rc == 0
    assert parsed["result"] == "PASS"
    assert set(parsed["checks"]) == set(mod.CHECK_KEYS)
    assert all(parsed["checks"].values())


def test_human_output_contains_result_summary(monkeypatch, capsys):
    mod = _load_smoke()
    _wire(mod, monkeypatch)
    mod.main(_BASE_ARGV)
    out = capsys.readouterr().out
    assert "Result: PASS" in out
    assert "tool_event_accepted" in out


def test_network_failure_exits_nonzero(monkeypatch, capsys):
    mod = _load_smoke()

    def _boom(req, **kw):
        raise OSError("connection refused")

    _wire(mod, monkeypatch, urlopen=_boom)
    rc = mod.main(_BASE_ARGV + ["--json"])
    assert rc == 1
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["result"] == "FAIL"


def test_malformed_server_response_exits_nonzero(monkeypatch, capsys):
    mod = _load_smoke()
    _wire(mod, monkeypatch, urlopen=_FakeUrlopen(malformed=True))
    rc = mod.main(_BASE_ARGV + ["--json"])
    assert rc == 1
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["result"] == "FAIL"


def test_skipped_checks_are_honest(monkeypatch, capsys):
    mod = _load_smoke()
    _wire(mod, monkeypatch)
    rc = mod.main(_BASE_ARGV + ["--json", "--skip-recall", "--skip-context-builder"])
    parsed = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert parsed["result"] == "PASS_WITH_SKIPS"
    skipped = parsed["skipped"]
    # recall-dependent checks are marked skipped and absent from the pass set
    assert "recall_stale_label" in skipped
    assert "context_warning_atomic" in skipped
    assert "recall_stale_label" not in parsed["checks"]


def test_fail_and_skip_are_distinct(monkeypatch, capsys):
    mod = _load_smoke()
    # Force recall to report the memory as NOT stale -> a real FAIL (not a skip).
    ctrl = _item("smoke-memory-control-001", False)
    seq = [[_item("smoke-memory-001", False), ctrl]] * 4
    _wire(mod, monkeypatch, backend=FakeBackend(recall_sequence=seq))
    rc = mod.main(_BASE_ARGV + ["--json"])
    parsed = json.loads(capsys.readouterr().out.strip())
    assert rc == 1
    assert parsed["result"] == "FAIL"
    assert "recall_stale_label" in parsed.get("failed", [])
    # A failed check is False in checks, never silently dropped into skipped.
    assert parsed["checks"]["recall_stale_label"] is False
    assert "recall_stale_label" not in parsed.get("skipped", {})


def test_tool_event_post_uploads_no_file_content(monkeypatch):
    mod = _load_smoke()
    fake = _FakeUrlopen()
    _wire(mod, monkeypatch, urlopen=fake)
    mod.main(_BASE_ARGV)
    tool_posts = [b for (u, b) in fake.posts if u.endswith("/tool-events")]
    assert tool_posts, "expected a tool-event POST"
    for body in tool_posts:
        for forbidden in ("content", "text", "body", "transcript", "file_content"):
            assert forbidden not in body


def test_cleanup_runs_by_default_and_keep_data_skips_post_clean(monkeypatch):
    # run_smoke always pre-cleans (idempotent re-run); the post-run clean is gated by
    # --keep-data. So default == 2 clean calls (pre + post), --keep-data == 1 (pre only).
    mod = _load_smoke()
    b1 = FakeBackend()
    _wire(mod, monkeypatch, backend=b1)
    mod.main(_BASE_ARGV)
    assert b1.clean_calls == 2

    b2 = FakeBackend()
    monkeypatch.setattr(mod, "_make_backend", lambda config: b2)
    mod.main(_BASE_ARGV + ["--keep-data"])
    assert b2.clean_calls == 1


def test_malformed_verification_check_reads_400(monkeypatch, capsys):
    """The malformed-timestamp receipt must be rejected (400) and reported PASS."""
    mod = _load_smoke()
    _wire(mod, monkeypatch)
    mod.main(_BASE_ARGV + ["--json"])
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["checks"]["malformed_timestamp_conservative"] is True
