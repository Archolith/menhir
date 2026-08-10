"""Hook Center Actionability Pack v1 — stale endpoint, dirty report script, policy guard.

No live Neo4j / server: uses FastAPI TestClient with mocked adapters and
monkeypatched urllib/policy-file reads.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest


# ======================================================================
# Part 1 — stale-anchor diagnostic endpoint
# ======================================================================


def _client(stale_anchors=None):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from menhir.api.routes import router

    adapter = SimpleNamespace(
        stale_anchored_memories=stale_anchors or (
            lambda **k: [{"memory_uuid": "m1", "name": "foo behavior",
                          "project": "menhir", "path": "src/foo.py",
                          "dirty_at": "2026-07-08T12:00:00Z",
                          "anchored_at": "2026-07-01T00:00:00Z",
                          "operation": "edit"}]
        ),
        list_dirty_files=lambda **k: [],
        record_file_event=lambda **k: {"accepted": True, "matched": 0,
                                       "marked_dirty": False, "paths": [k["path"]]},
    )
    built = SimpleNamespace(graph_adapter=adapter)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(built=built)
    return TestClient(app)


@pytest.mark.unit
def test_stale_endpoint_returns_stale_anchors():
    c = _client()
    r = c.get("/api/tool-events/stale")
    assert r.status_code == 200
    body = r.json()
    assert "stale_anchors" in body
    assert body["count"] >= 1
    assert body["stale_anchors"][0]["memory_uuid"] == "m1"


@pytest.mark.unit
def test_stale_endpoint_passes_project():
    seen = {}

    def _stale(**k):
        seen.update(k)
        return []

    c = _client(stale_anchors=_stale)
    c.get("/api/tool-events/stale?project=menhir")
    assert seen.get("project") == "menhir"


@pytest.mark.unit
def test_stale_endpoint_passes_limit():
    seen = {}

    def _stale(**k):
        seen.update(k)
        return []

    c = _client(stale_anchors=_stale)
    c.get("/api/tool-events/stale?limit=50")
    assert seen.get("limit") == 50


@pytest.mark.unit
def test_stale_endpoint_empty_returns_count_zero():
    c = _client(stale_anchors=lambda **k: [])
    r = c.get("/api/tool-events/stale")
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ======================================================================
# Part 2 — dirty-file report script
# ======================================================================


def _load_report_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "maintenance" / "report_dirty_files.py"
    spec = importlib.util.spec_from_file_location("report_dirty_files", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE_DIRTY_RESPONSE = {
    "dirty_files": [
        {"project": "menhir", "path": "src/foo.py",
         "dirty_at": "t", "operation": "edit", "source_client": "claude_code"}
    ],
    "stale_anchors": [
        {"memory_uuid": "m1", "name": "foo behavior", "project": "menhir",
         "path": "src/foo.py", "dirty_at": "t", "anchored_at": "t-7d", "operation": "edit"}
    ],
    "counts": {"dirty_files": 1, "stale_anchors": 1},
}


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


@pytest.mark.unit
def test_report_resolves_default_url(monkeypatch):
    monkeypatch.delenv("MENHIR_TOOL_EVENTS_URL", raising=False)
    monkeypatch.delenv("MENHIR_URL", raising=False)
    mod = _load_report_script()
    assert mod._resolve_url() == "http://127.0.0.1:8090/api/tool-events/dirty"


@pytest.mark.unit
def test_report_resolves_tool_events_url(monkeypatch):
    monkeypatch.setenv("MENHIR_TOOL_EVENTS_URL", "http://other:9000/api/tool-events")
    mod = _load_report_script()
    assert mod._resolve_url() == "http://other:9000/api/tool-events/dirty"


@pytest.mark.unit
def test_report_resolves_menhir_url(monkeypatch):
    monkeypatch.delenv("MENHIR_TOOL_EVENTS_URL", raising=False)
    monkeypatch.setenv("MENHIR_URL", "http://other:9000")
    mod = _load_report_script()
    assert mod._resolve_url() == "http://other:9000/api/tool-events/dirty"


@pytest.mark.unit
def test_report_auth_header_when_key_set(monkeypatch):
    monkeypatch.setenv("MENHIR_AGENT_KEY", "secret-token")
    mod = _load_report_script()
    req = mod._build_request("http://example.com/dirty")
    assert req.get_header("Authorization") == "Bearer secret-token"


@pytest.mark.unit
def test_report_no_auth_header_when_key_unset(monkeypatch):
    monkeypatch.delenv("MENHIR_AGENT_KEY", raising=False)
    mod = _load_report_script()
    req = mod._build_request("http://example.com/dirty")
    assert req.get_header("Authorization") is None


@pytest.mark.unit
def test_report_json_mode(monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", lambda r, **k: FakeResponse(SAMPLE_DIRTY_RESPONSE))
    mod = _load_report_script()
    monkeypatch.setattr("sys.argv", ["report_dirty_files.py", "--json"])
    assert mod.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["dirty_files"] == 1


@pytest.mark.unit
def test_report_summary_mode(monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", lambda r, **k: FakeResponse(SAMPLE_DIRTY_RESPONSE))
    mod = _load_report_script()
    monkeypatch.setattr("sys.argv", ["report_dirty_files.py"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "Dirty files count:" in out
    assert "Stale anchors count:" in out
    assert "src/foo.py" in out
    assert "foo behavior" in out


@pytest.mark.unit
def test_report_network_failure_exits_nonzero(monkeypatch):
    def _fail(*a, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr("urllib.request.urlopen", _fail)
    mod = _load_report_script()
    monkeypatch.setattr("sys.argv", ["report_dirty_files.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


# ======================================================================
# Part 3 — Policy Guard hook
# ======================================================================


def _load_policy_guard():
    path = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "menhir_policy_guard.py"
    spec = importlib.util.spec_from_file_location("menhir_policy_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WARN_POLICY = {"mode": "warn", "frozen_paths": [".env*", "*.key"],
               "branch_scope": ["src/**", "docs/**"]}
BLOCK_POLICY = {"mode": "block", "frozen_paths": [".env*", "*.key"],
                "branch_scope": ["src/**", "docs/**"]}


@pytest.mark.unit
def test_policy_guard_no_policy_file_exits_zero(monkeypatch, tmp_path):
    guard = _load_policy_guard()
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name":"Edit","tool_input":{"file_path":"x"}}'))
    # point to a non-existent policy
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(tmp_path / "nonexistent.json"))
    assert guard.main() == 0


@pytest.mark.unit
def test_policy_guard_malformed_input_exits_zero(monkeypatch, tmp_path):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(WARN_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard.main() == 0


@pytest.mark.unit
def test_policy_guard_non_file_tool_exits_zero(monkeypatch, tmp_path):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(BLOCK_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})))
    assert guard.main() == 0


@pytest.mark.unit
def test_policy_guard_frozen_warn_mode(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(WARN_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": ".env.local"}})))
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower()
    assert ".env" in out


@pytest.mark.unit
def test_policy_guard_frozen_block_mode(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(BLOCK_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "secrets.key"}})))
    assert guard.main() == 2
    out = capsys.readouterr().out
    assert "blocked" in out.lower()


@pytest.mark.unit
def test_policy_guard_branch_scope_warning(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(WARN_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "node_modules/pkg/index.js"}})))
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "outside this branch scope" in out


@pytest.mark.unit
def test_policy_guard_branch_scope_allowed(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(WARN_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/foo.py"}})))
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert out == ""


@pytest.mark.unit
def test_policy_guard_rename_checks_both_paths(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps({"mode": "warn", "frozen_paths": ["*.secret"]}))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "move", "tool_input": {"path": "src/new.py", "old_path": "src/old.secret"}})))
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "old.secret" in out
    assert "warning" in out.lower()


@pytest.mark.unit
def test_policy_guard_backslash_normalization(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps({"mode": "warn", "frozen_paths": ["src\\**"]}))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src\\foo.py"}})))
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower()


@pytest.mark.unit
def test_policy_guard_no_file_content_in_output(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(BLOCK_POLICY))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": ".env.prod"}})))
    guard.main()
    out = capsys.readouterr().out
    # should never print the file's contents or any secret-like values
    assert "DATABASE_URL" not in out
    assert "password" not in out


@pytest.mark.unit
def test_policy_guard_watch_mode_logs_but_exits_zero(monkeypatch, tmp_path, capsys):
    guard = _load_policy_guard()
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps({"mode": "watch", "frozen_paths": [".env*"]}))
    monkeypatch.setenv("MENHIR_POLICY_FILE", str(pfile))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": ".env"}})))
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert out == ""
