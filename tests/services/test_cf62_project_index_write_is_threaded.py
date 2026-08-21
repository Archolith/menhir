"""CF-62: the one unthreaded write in ``refresh_structure_graphs`` runs off the event loop.

Every piece of I/O in this job is threaded except the final ``_write_project_index`` JSON write.
This pins that the write now runs on a worker thread, still writes the expected payload, and that
the test can actually tell threaded from unthreaded (positive control via the sibling calls).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import menhir.infrastructure.project_scanner as _project_scanner
import menhir.services.scheduler_tasks as st

pytestmark = pytest.mark.unit

_PROJECTS = [{"name": "proj-a", "root_path": "C:/proj/a"}]


class _Recorder:
    """Records the thread id each I/O callback runs on and the projects payload."""

    def __init__(self) -> None:
        self.thread_ids: dict[str, int] = {}
        self.projects: object = None

    def index(self, projects, *, index_path=None) -> None:
        self.thread_ids["index"] = threading.get_ident()
        self.projects = projects

    def list_projects(self) -> list[dict[str, str]]:
        self.thread_ids["list"] = threading.get_ident()
        return list(_PROJECTS)

    def scan(self, root_path: str, name: str) -> SimpleNamespace:
        self.thread_ids["scan"] = threading.get_ident()
        return SimpleNamespace(scan_fingerprint="fp-a")

    def fingerprint(self, project_name: str) -> None:
        self.thread_ids["fingerprint"] = threading.get_ident()
        return None

    def write_structure(self, scan, session_id: str, user_id: str) -> dict[str, int]:
        self.thread_ids["write"] = threading.get_ident()
        return {"entities": 1, "edges": 0}


class _Adapter:
    def __init__(self, rec: _Recorder) -> None:
        self.rec = rec

    def list_structure_projects(self):
        return self.rec.list_projects()

    def get_scan_fingerprint(self, project_name: str):
        return self.rec.fingerprint(project_name)

    def write_project_structure(self, scan, session_id: str, user_id: str):
        return self.rec.write_structure(scan, session_id, user_id)


class _Scanner:
    def __init__(self, rec: _Recorder) -> None:
        self.rec = rec

    def scan(self, root_path: str, name: str):
        return self.rec.scan(root_path, name)


@pytest.mark.asyncio
async def test_project_index_write_runs_off_the_event_loop(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(st, "_write_project_index", rec.index)
    monkeypatch.setattr(_project_scanner, "ProjectScanner", lambda: _Scanner(rec))

    loop_thread = threading.get_ident()
    await st.refresh_structure_graphs(_Adapter(rec))

    # The finding: the index write does not run on the event loop thread.
    assert rec.thread_ids["index"] != loop_thread

    # POSITIVE CONTROL: the index is still written -- the payload the recorder captured is the
    # expected `projects` value. A fix that dropped the call would pass the first assertion.
    assert rec.projects == _PROJECTS

    # POSITIVE CONTROL: at least one of the already-threaded sibling calls also runs off-loop, so
    # this test proves it can tell threaded from unthreaded rather than always passing.
    off_loop_siblings = [
        rec.thread_ids.get("list"),
        rec.thread_ids.get("scan"),
        rec.thread_ids.get("fingerprint"),
        rec.thread_ids.get("write"),
    ]
    assert any(tid is not None and tid != loop_thread for tid in off_loop_siblings)
