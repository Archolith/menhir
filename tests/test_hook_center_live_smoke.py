"""Hook Center Live Smoke v1 — unit tests for the smoke harness.

No real server: mocks HTTP calls and subprocess invocations.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_smoke():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke" / "hook_center_live_smoke.py"
    spec = importlib.util.spec_from_file_location("hook_center_live_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status

    def read(self):
        return json.dumps(self.data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeUrlopen:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.last_url = None
        self.last_data = None

    def __call__(self, req, **kw):
        url = str(getattr(req, "full_url", req))
        data = getattr(req, "data", None)
        self.last_url = url
        self.last_data = data
        for pattern, resp in self.routes.items():
            if pattern in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return FakeResponse({})


REPORT_OK = "Dirty files count:      1\nStale anchors count:    0\n\nDirty files:\n  [menhir] src/t.py  (edit)\n"
POLICY_WARN = 'Menhir policy warning: .env.local matches frozen_paths pattern ".env*".'
POLICY_BLOCK = 'Menhir policy blocked edit: .env.local matches frozen_paths pattern ".env*".'


def _make_subprocess(block=False):
    """Build a subprocess.run mock that returns appropriate output per script."""
    call_count = [0]

    def _run(*a, **kw):
        args = " ".join(a[0]) if a else ""
        i = call_count[0]
        call_count[0] += 1
        if "report_dirty_files" in args:
            return type("P", (), {"returncode": 0, "stdout": REPORT_OK, "stderr": ""})()
        if i <= 1:
            return type("P", (), {"returncode": 0, "stdout": POLICY_WARN, "stderr": ""})()
        code = 2 if block else 0
        return type("P", (), {"returncode": code, "stdout": POLICY_BLOCK, "stderr": ""})()

    return _run


def _raise_subprocess(*a, **kw):
    raise RuntimeError("subprocess failed")


# --------------------------------------------------------------------------- tests


def test_url_resolution_default(monkeypatch):
    monkeypatch.delenv("MENHIR_SMOKE_URL", raising=False)
    smoke = _load_smoke()
    assert smoke._resolve_base_url() == "http://127.0.0.1:8099"


def test_url_resolution_env(monkeypatch):
    monkeypatch.setenv("MENHIR_SMOKE_URL", "http://other:9090")
    smoke = _load_smoke()
    assert smoke._resolve_base_url() == "http://other:9090"


def test_post_payload_contains_no_file_content(monkeypatch):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={"/tool-events": FakeResponse({"accepted": True, "marked_dirty": False})})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    smoke.step_send_event("http://x:8099", "project", "src/t.py")
    if fake.last_data:
        body = json.loads(fake.last_data.decode("utf-8"))
        assert "content" not in body
        assert "text" not in body
        assert "body" not in body


def test_accepted_false_causes_fail(monkeypatch):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": False, "marked_dirty": False}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py"])
    assert smoke.main() == 1


def test_accepted_true_marked_dirty_false_produces_pass_unscanned(monkeypatch, capsys):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": False}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py"])
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "PASS_WITH_UNSCANNED_FILE" in out


def test_report_script_failure_causes_fail(monkeypatch):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": True}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _raise_subprocess)
    monkeypatch.setattr("sys.argv", ["smoke.py"])
    assert smoke.main() == 1


def test_policy_guard_warn_expected_output(monkeypatch, capsys):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": True}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py"])
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "Policy guard warn: PASS" in out
    assert "Policy guard block: PASS" in out


def test_policy_guard_block_exit_2(monkeypatch, capsys):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": True}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py"])
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "Policy guard block: PASS" in out


def test_require_stale_fails_when_count_zero(monkeypatch):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": True}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py", "--require-stale"])
    assert smoke.main() == 1


def test_skip_policy_flag(monkeypatch, capsys):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": True}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py", "--skip-policy"])
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out


def test_json_output_is_parseable_json(monkeypatch, capsys):
    smoke = _load_smoke()
    fake = FakeUrlopen(routes={
        "/dirty": FakeResponse({"dirty_files": [], "stale_anchors": [], "counts": {}}),
        "/tool-events": FakeResponse({"accepted": True, "marked_dirty": True}),
        "/stale": FakeResponse({"stale_anchors": [], "count": 0}),
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("subprocess.run", _make_subprocess(block=True))
    monkeypatch.setattr("sys.argv", ["smoke.py", "--json"])
    assert smoke.main() == 0
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["result"] == "PASS"
    assert parsed["server"] == "http://127.0.0.1:8099"
    assert parsed["accepted"] is True
    assert parsed["report_ok"] is True
