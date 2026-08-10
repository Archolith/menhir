"""Hook Center / tool-event capture v0 — deterministic dirty/stale marking + the file-event hook.

No live Neo4j / server: a FakeNeo4j records routed queries, and the hook is exercised as pure
functions. Proves: normalized events mark the affected structure-file node dirty; a memory anchored
BEFORE the change is detectable as stale; delete/rename don't crash; the hook fails open and uploads
NO file content.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

from menhir.infrastructure.tool_event_repository import ToolEventRepository, affected_paths


class FakeNeo4j:
    """Routes an executed cypher string to canned rows by substring; records every call + params."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or []
        self.default = [] if default is None else default
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        for sub, rows in self.routes:
            if sub in query:
                return rows
        return self.default


def _load_hook():
    path = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "menhir_file_event.py"
    spec = importlib.util.spec_from_file_location("menhir_file_event", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- affected_paths


@pytest.mark.unit
def test_affected_paths_rename_marks_both():
    assert affected_paths("new.py", "old.py", "rename") == ["new.py", "old.py"]
    assert affected_paths("foo.py", "old.py", "edit") == ["foo.py"]      # non-rename ignores old_path
    assert affected_paths("foo.py", None, "delete") == ["foo.py"]
    assert affected_paths("", None, "edit") == []


# ----------------------------------------------------------------------------- repo: dirty marking


@pytest.mark.unit
def test_record_file_event_marks_matching_file_dirty():
    fake = FakeNeo4j(routes=[("SET f.structure_dirty = true", [{"matched": 1}])])
    out = ToolEventRepository(fake).record_file_event(
        path="src/foo.py", operation="edit", project="menhir", after_hash="abc", mtime="2026-07-08")
    assert out == {"accepted": True, "matched": 1, "marked_dirty": True, "paths": ["src/foo.py"],
                   "project_fallback_used": False}
    q, params = fake.executed[-1]
    assert params["paths"] == ["src/foo.py"] and params["project"] == "menhir"
    assert params["after_hash"] == "abc" and params["op"] == "edit"


@pytest.mark.unit
def test_record_file_event_no_matching_file_is_accepted_not_dirty():
    # a file not (yet) in the structure graph -> accepted, matched 0, no error (v0 fail-safe).
    fake = FakeNeo4j(default=[{"matched": 0}])
    out = ToolEventRepository(fake).record_file_event(path="src/never_scanned.py")
    assert out["accepted"] is True and out["matched"] == 0 and out["marked_dirty"] is False


@pytest.mark.unit
def test_record_file_event_rename_marks_both_paths():
    fake = FakeNeo4j(routes=[("SET f.structure_dirty = true", [{"matched": 2}])])
    out = ToolEventRepository(fake).record_file_event(
        path="src/new.py", old_path="src/old.py", operation="rename")
    assert out["paths"] == ["src/new.py", "src/old.py"] and out["marked_dirty"] is True


@pytest.mark.unit
def test_record_file_event_delete_does_not_crash():
    fake = FakeNeo4j(routes=[("SET f.structure_dirty = true", [{"matched": 1}])])
    out = ToolEventRepository(fake).record_file_event(path="src/gone.py", operation="delete")
    assert out["accepted"] is True and out["paths"] == ["src/gone.py"]


@pytest.mark.unit
def test_record_file_event_requires_path_and_valid_operation():
    repo = ToolEventRepository(FakeNeo4j())
    with pytest.raises(ValueError):
        repo.record_file_event(path="   ")
    with pytest.raises(ValueError):
        repo.record_file_event(path="x.py", operation="frobnicate")


@pytest.mark.unit
def test_record_file_event_stores_no_content():
    # the params sent to neo4j must never carry file content — only path/hash/mtime/op provenance.
    fake = FakeNeo4j(routes=[("SET f.structure_dirty = true", [{"matched": 1}])])
    ToolEventRepository(fake).record_file_event(path="src/foo.py", after_hash="deadbeef")
    _q, params = fake.executed[-1]
    assert set(params) == {"paths", "project", "op", "after_hash", "mtime", "source_client"}
    assert "content" not in params and "text" not in params and "body" not in params


# ----------------------------------------------------------------------------- repo: stale detection


# ----------------------------------------------------------------------------- repo: project-scope fallback (Issue 2)


@pytest.mark.unit
def test_record_file_event_project_match_no_fallback():
    fake = FakeNeo4j(routes=[("SET f.structure_dirty = true", [{"matched": 1}])])
    out = ToolEventRepository(fake).record_file_event(
        path="src/foo.py", operation="edit", project="menhir")
    assert out["matched"] == 1 and out["marked_dirty"] is True
    assert out["project_fallback_used"] is False
    _q, params = fake.executed[-1]
    assert params["project"] == "menhir"  # only one call, scoped


@pytest.mark.unit
def test_record_file_event_project_mismatch_fallback():
    # first call (project-scoped) -> 0 matched; fallback (path-only) -> 1 matched
    calls = iter([
        [{"matched": 0}],   # project-scoped
        [{"matched": 1}],   # path-only fallback
    ])
    def _side_effect(*a, **kw):
        return next(calls)
    fake = FakeNeo4j()
    fake.execute = _side_effect
    out = ToolEventRepository(fake).record_file_event(
        path="src/foo.py", operation="edit", project="wrong-project")
    assert out["matched"] == 1 and out["marked_dirty"] is True
    assert out["project_fallback_used"] is True


@pytest.mark.unit
def test_record_file_event_project_mismatch_no_path_match():
    calls = iter([
        [{"matched": 0}],   # project-scoped
        [{"matched": 0}],   # path-only fallback also misses
    ])
    def _side_effect(*a, **kw):
        return next(calls)
    fake = FakeNeo4j()
    fake.execute = _side_effect
    out = ToolEventRepository(fake).record_file_event(
        path="src/unknown.py", operation="write", project="wrong-project")
    assert out["accepted"] is True and out["matched"] == 0
    assert out["marked_dirty"] is False
    assert out["project_fallback_used"] is True


@pytest.mark.unit
def test_record_file_event_rename_project_mismatch_fallback():
    """rename: both affected paths should fallback correctly."""
    calls = iter([
        [{"matched": 0}],   # project-scoped (both paths)
        [{"matched": 2}],   # path-only fallback matches both
    ])
    def _side_effect(*a, **kw):
        return next(calls)
    fake = FakeNeo4j()
    fake.execute = _side_effect
    out = ToolEventRepository(fake).record_file_event(
        path="src/new.py", old_path="src/old.py", operation="rename",
        project="wrong-project")
    assert out["matched"] == 2 and out["marked_dirty"] is True
    assert out["project_fallback_used"] is True


# ----------------------------------------------------------------------------- repo: stale detection


@pytest.mark.unit
def test_stale_anchored_memories_detects_anchor_older_than_change():
    fake = FakeNeo4j(routes=[("f.dirty_at > a.created_at",
                              [{"memory_uuid": "m1", "name": "foo behavior", "project": "menhir",
                                "path": "src/foo.py", "dirty_at": "2026-07-08T10:00:00Z",
                                "anchored_at": "2026-07-01T00:00:00Z", "operation": "edit"}])])
    stale = ToolEventRepository(fake).stale_anchored_memories(project="menhir")
    assert len(stale) == 1 and stale[0]["memory_uuid"] == "m1"
    q, _ = fake.executed[-1]
    # the staleness predicate is the change-after-anchor comparison.
    assert "f.dirty_at > a.created_at" in q and "ANCHORED_TO" in q


@pytest.mark.unit
def test_list_dirty_files_and_clear():
    fake = FakeNeo4j(routes=[("RETURN f.structure_project AS project, f.structure_path AS path",
                              [{"project": "menhir", "path": "src/foo.py",
                                "dirty_at": "t", "operation": "edit", "source_client": "claude_code"}]),
                             ("REMOVE f.structure_dirty", [{"cleared": 1}])])
    repo = ToolEventRepository(fake)
    assert repo.list_dirty_files(project="menhir")[0]["path"] == "src/foo.py"
    assert repo.clear_file_dirty(path="src/foo.py", project="menhir") == 1


# ----------------------------------------------------------------------------- hook normalization


@pytest.mark.unit
def test_hook_normalizes_claude_edit_event():
    hook = _load_hook()
    ev = hook.normalize_event({"tool_name": "Edit", "tool_input": {"file_path": "src/foo.py"},
                               "session_id": "s1", "transcript_path": "/t.jsonl"})
    assert ev is not None
    assert ev["event_type"] == "file_changed" and ev["operation"] == "edit"
    assert ev["path"] == "src/foo.py" and ev["source_client"] == "claude_code"
    assert "content" not in ev and "text" not in ev  # never a content field


@pytest.mark.unit
def test_hook_normalizes_codex_write_and_rename():
    hook = _load_hook()
    w = hook.normalize_event({"tool": "Write", "input": {"path": "src/bar.py"},
                              "workspace_root": "/repo"})
    assert w["operation"] == "write" and w["source_client"] == "codex"
    mv = hook.normalize_event({"tool_name": "move", "tool_input": {"path": "src/new.py",
                                                                   "old_path": "src/old.py"}})
    assert mv["operation"] == "rename" and mv["old_path"] == "src/old.py"


@pytest.mark.unit
def test_hook_ignores_non_file_tools_and_missing_path():
    hook = _load_hook()
    assert hook.normalize_event({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert hook.normalize_event({"tool_name": "Edit", "tool_input": {}}) is None
    assert hook.normalize_event({}) is None
    assert hook.normalize_event("not a dict") is None


@pytest.mark.unit
def test_hook_delete_event_sends_no_hash_and_does_not_read_file():
    hook = _load_hook()
    ev = hook.normalize_event({"tool_name": "delete", "tool_input": {"file_path": "src/gone.py"}})
    assert ev["operation"] == "delete" and ev["after_hash"] is None and ev["mtime"] is None


@pytest.mark.unit
def test_hook_normalizes_absolute_path_under_project_root(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "git_probe", lambda *a, **kw: "/repo")
    ev = hook.normalize_event({"tool_name": "Edit",
                               "tool_input": {"file_path": "/repo/src/foo.py"},
                               "cwd": "/repo"})
    assert ev["path"] == "src/foo.py"
    assert "content" not in ev


@pytest.mark.unit
def test_hook_keeps_relative_path_as_is(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "git_probe", lambda *a, **kw: "/repo")
    ev = hook.normalize_event({"tool_name": "Write",
                               "tool_input": {"path": "src/bar.py"},
                               "cwd": "/repo"})
    assert ev["path"] == "src/bar.py"


@pytest.mark.unit
def test_hook_normalizes_rename_both_paths(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "git_probe", lambda *a, **kw: "/repo")
    ev = hook.normalize_event({"tool_name": "move",
                               "tool_input": {"path": "/repo/src/new.py",
                                              "old_path": "/repo/src/old.py"},
                               "cwd": "/repo"})
    assert ev["path"] == "src/new.py"
    assert ev["old_path"] == "src/old.py"


@pytest.mark.unit
def test_hook_outside_project_absolute_path_does_not_crash(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "git_probe", lambda *a, **kw: "/repo")
    ev = hook.normalize_event({"tool_name": "Edit",
                               "tool_input": {"file_path": "/other/lib.py"},
                               "cwd": "/other"})
    assert ev["path"] == "/other/lib.py"  # unchanged, outside repo


@pytest.mark.unit
def test_hook_stores_original_path_in_metadata_when_normalized(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "git_probe", lambda *a, **kw: "/repo")
    ev = hook.normalize_event({"tool_name": "Edit",
                               "tool_input": {"file_path": "/repo/src/data.py"},
                               "cwd": "/repo"})
    assert ev["path"] == "src/data.py"
    assert ev["metadata"]["original_path"] == "/repo/src/data.py"


@pytest.mark.unit
def test_hook_no_original_path_metadata_when_already_relative(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "git_probe", lambda *a, **kw: "/repo")
    ev = hook.normalize_event({"tool_name": "Edit",
                               "tool_input": {"file_path": "src/data.py"},
                               "cwd": "/repo"})
    assert ev["path"] == "src/data.py"
    assert "original_path" not in ev["metadata"]


@pytest.mark.unit
def test_hook_fails_open_when_menhir_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_TURN_HOOK_LOG", str(tmp_path / "hook.log"))
    hook = _load_hook()
    ok = hook.post_event({"path": "src/foo.py", "operation": "edit", "source_client": "claude_code"},
                         url="http://127.0.0.1:9/tool-events", timeout=1.0)
    assert ok is False
    entry = json.loads((tmp_path / "hook.log").read_text().strip())
    # the failure log records the path length, never file content.
    assert "content" not in entry and entry["source_client"] == "claude_code"


@pytest.mark.unit
def test_hook_main_fails_open_on_malformed_stdin(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    monkeypatch.setattr("sys.argv", ["menhir_file_event.py"])
    assert hook.main() == 0


@pytest.mark.unit
def test_hook_dry_run_reports_no_content_upload(monkeypatch, capsys):
    hook = _load_hook()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/foo.py"}})))
    monkeypatch.setattr("sys.argv", ["menhir_file_event.py", "--dry-run"])
    assert hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["would_send"] is True and out["content_uploaded"] is False


# ----------------------------------------------------------------------------- endpoint (POST /api/tool-events)


def _client(record_file_event=None, list_dirty=None, stale=None):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from menhir.api.routes import router

    adapter = SimpleNamespace(
        record_file_event=record_file_event or (lambda **k: {
            "accepted": True, "matched": 1, "marked_dirty": True, "paths": [k["path"]]}),
        list_dirty_files=list_dirty or (lambda **k: [{"project": "menhir", "path": "src/foo.py",
                                                      "dirty_at": "t", "operation": "edit"}]),
        stale_anchored_memories=stale or (lambda **k: [{"memory_uuid": "m1", "path": "src/foo.py"}]),
    )
    built = SimpleNamespace(graph_adapter=adapter)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(built=built)
    return TestClient(app)


@pytest.mark.unit
def test_endpoint_accepts_minimal_file_changed_event():
    c = _client()
    r = c.post("/api/tool-events", json={"event_type": "file_changed", "path": "src/foo.py"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True and body["marked_dirty"] is True and body["matched"] == 1


@pytest.mark.unit
def test_endpoint_optional_metadata_is_non_fatal():
    seen = {}

    def _rec(**k):
        seen.update(k)
        return {"accepted": True, "matched": 0, "marked_dirty": False, "paths": [k["path"]]}

    c = _client(record_file_event=_rec)
    # only path + event_type; every other field omitted -> still 200.
    r = c.post("/api/tool-events", json={"event_type": "file_changed", "path": "src/new.py",
                                         "project_root": "/home/u/menhir"})
    assert r.status_code == 200
    assert seen["project"] == "menhir"  # derived from project_root basename
    assert seen["after_hash"] is None and seen["mtime"] is None


@pytest.mark.unit
def test_endpoint_unknown_event_type_is_accepted_and_ignored():
    c = _client()
    r = c.post("/api/tool-events", json={"event_type": "tool_ran", "path": "x"})
    assert r.status_code == 200 and r.json()["marked_dirty"] is False
    assert r.json()["ignored_reason"] == "unsupported event_type in v0"


@pytest.mark.unit
def test_endpoint_unknown_event_type_no_path_accepted():
    c = _client()
    r = c.post("/api/tool-events", json={"event_type": "tool_ran"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True and body["marked_dirty"] is False
    assert body["ignored_reason"] == "unsupported event_type in v0"


@pytest.mark.unit
def test_endpoint_file_changed_no_path_400():
    c = _client()
    r = c.post("/api/tool-events", json={"event_type": "file_changed"})
    assert r.status_code == 400


@pytest.mark.unit
def test_endpoint_file_changed_blank_path_400():
    c = _client()
    r = c.post("/api/tool-events", json={"event_type": "file_changed", "path": "  "})
    assert r.status_code == 400


@pytest.mark.unit
def test_endpoint_sends_no_content_field():
    # a client that accidentally includes content: the server model drops it (not in ToolEventRequest).
    seen = {}

    def _rec(**k):
        seen.update(k)
        return {"accepted": True, "matched": 1, "marked_dirty": True, "paths": [k["path"]]}

    c = _client(record_file_event=_rec)
    c.post("/api/tool-events", json={"event_type": "file_changed", "path": "src/foo.py",
                                     "content": "SECRET FILE BODY"})
    assert "content" not in seen and "SECRET FILE BODY" not in json.dumps(seen)


@pytest.mark.unit
def test_dirty_diagnostic_endpoint():
    c = _client()
    r = c.get("/api/tool-events/dirty")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["dirty_files"] == 1 and body["counts"]["stale_anchors"] == 1


# ----------------------------------------------------------------------------- end-to-end smoke


class StatefulFakeGraph:
    """A tiny stateful graph that models the dirty/stale SEMANTICS (not raw Cypher) so the real
    ToolEventRepository can be exercised end to end: a file node + a memory anchored to it at a fixed
    time. record_file_event sets the file dirty (with a later dirty_at); stale detection then returns
    the pre-existing anchor because its file changed after it was anchored."""

    def __init__(self):
        # one memory anchored to src/foo.py on the 1st; the file has no dirty marker yet.
        self.anchors = [{"memory_uuid": "m1", "name": "foo behavior", "project": "menhir",
                         "path": "src/foo.py", "anchored_at": "2026-07-01T00:00:00Z"}]
        self.dirty: dict[str, dict] = {}  # path -> {dirty_at, op}

    def execute(self, query, params=None):
        params = params or {}
        if "SET f.structure_dirty = true" in query:
            matched = 0
            for p in params.get("paths", []):
                if any(a["path"] == p for a in self.anchors):  # file exists in the graph
                    self.dirty[p] = {"dirty_at": "2026-07-08T12:00:00Z", "op": params.get("op")}
                    matched += 1
            return [{"matched": matched}]
        if "f.dirty_at > a.created_at" in query:  # stale detection
            out = []
            for a in self.anchors:
                d = self.dirty.get(a["path"])
                if d and d["dirty_at"] > a["anchored_at"]:
                    out.append({"memory_uuid": a["memory_uuid"], "name": a["name"],
                                "project": a["project"], "path": a["path"],
                                "dirty_at": d["dirty_at"], "anchored_at": a["anchored_at"],
                                "operation": d["op"]})
            return out
        return []


@pytest.mark.unit
def test_smoke_file_change_makes_prior_anchor_stale():
    """SMOKE: an anchored memory + a later file_changed event -> the anchor is detectable as stale.
    Uses a stateful mock adapter, no live :8090 / Neo4j."""
    repo = ToolEventRepository(StatefulFakeGraph())
    # before the change: nothing is stale.
    assert repo.stale_anchored_memories(project="menhir") == []
    # a file edit event arrives for the anchored file.
    out = repo.record_file_event(path="src/foo.py", operation="edit", project="menhir",
                                 after_hash="newhash", source_client="claude_code")
    assert out["marked_dirty"] is True
    # now the pre-existing anchor (anchored 2026-07-01) is stale (file changed 2026-07-08).
    stale = repo.stale_anchored_memories(project="menhir")
    assert len(stale) == 1 and stale[0]["memory_uuid"] == "m1" and stale[0]["operation"] == "edit"
