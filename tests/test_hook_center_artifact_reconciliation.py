"""The artifact-reconciliation leg of a Hook Center file event.

The invariant under test throughout: this leg is additive. Structural dirty
marking already happened before it runs, so no outcome here -- refusal, error,
or a path that is not corpus material -- may change the structural half of the
response.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api.routes import router
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter


def _client(reconcile=None, record=None) -> TestClient:
    adapter = SimpleNamespace(
        record_file_event=record or (lambda **k: {
            "accepted": True, "matched": 1, "marked_dirty": True, "paths": [k["path"]]}),
    )
    if reconcile is not None:
        adapter.reconcile_file_event_source = reconcile
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(built=SimpleNamespace(graph_adapter=adapter))
    return TestClient(app)


@pytest.mark.unit
def test_a_rename_relocates_one_source_and_still_marks_both_paths_dirty() -> None:
    seen: dict = {}
    dirty: dict = {}

    def _reconcile(**kwargs):
        seen.update(kwargs)
        return {"attempted": True, "applied": True, "artifact_uuid": "a-1"}

    def _record(**kwargs):
        dirty.update(kwargs)
        return {"accepted": True, "matched": 2, "marked_dirty": True,
                "paths": [kwargs["path"], kwargs["old_path"]]}

    client = _client(reconcile=_reconcile, record=_record)
    response = client.post("/api/tool-events", json={
        "event_type": "file_changed",
        "operation": "rename",
        "path": ".agent/archive/plans/a.md",
        "old_path": ".agent/plans/a.md",
        "project": "menhir",
        "after_hash": "abc123",
        "git_commit": "c0ffee",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["marked_dirty"] is True and body["matched"] == 2
    assert body["artifact_reconciliation"]["applied"] is True
    assert dirty["old_path"] == ".agent/plans/a.md"
    assert seen["after_hash"] == "abc123" and seen["git_commit"] == "c0ffee"


@pytest.mark.unit
def test_a_reconciliation_error_does_not_fail_the_event() -> None:
    def _boom(**_kwargs):
        raise RuntimeError("neo4j is down")

    response = _client(reconcile=_boom).post("/api/tool-events", json={
        "event_type": "file_changed", "operation": "rename",
        "path": ".agent/archive/plans/a.md", "old_path": ".agent/plans/a.md",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["marked_dirty"] is True
    assert body["artifact_reconciliation"]["applied"] is False


@pytest.mark.unit
def test_a_path_outside_the_corpus_reports_nothing() -> None:
    response = _client(
        reconcile=lambda **_k: {"attempted": False, "reason": "path_is_not_corpus_material"}
    ).post("/api/tool-events", json={
        "event_type": "file_changed", "operation": "edit", "path": "src/menhir/main.py",
    })
    assert response.status_code == 200
    assert response.json()["artifact_reconciliation"] is None


@pytest.mark.unit
def test_an_adapter_without_the_method_is_unaffected() -> None:
    """Older adapters keep working; the leg is opt-in by capability."""
    response = _client().post("/api/tool-events", json={
        "event_type": "file_changed", "operation": "edit", "path": ".agent/plans/a.md",
    })
    assert response.status_code == 200
    assert response.json()["artifact_reconciliation"] is None


# ---------------------------------------------------------------------------
# Adapter-level routing
# ---------------------------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def relocate_artifact_source_by_locator(self, **kwargs):
        self.calls.append(("relocate", kwargs))
        return {"applied": True}

    def refresh_artifact_source_by_locator(self, **kwargs):
        self.calls.append(("refresh", kwargs))
        return {"applied": True}


def _adapter(repo: _Repo) -> MemoryGraphAdapter:
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._work_artifacts = repo
    return adapter


@pytest.mark.unit
def test_a_rename_routes_to_relocation_with_the_destination_lane() -> None:
    repo = _Repo()
    result = _adapter(repo).reconcile_file_event_source(
        path=".agent/archive/plans/a.md", operation="rename",
        old_path=".agent/plans/a.md", repository="menhir", after_hash="abc",
    )
    assert result["applied"] is True
    name, kwargs = repo.calls[0]
    assert name == "relocate"
    assert kwargs["old_path"] == ".agent/plans/a.md"
    assert kwargs["observation"].lane == "archive"
    assert kwargs["observation"].integrity == "abc"


@pytest.mark.unit
def test_an_edit_refreshes_the_hash_at_an_unchanged_path() -> None:
    repo = _Repo()
    _adapter(repo).reconcile_file_event_source(
        path=".agent/plans/a.md", operation="edit", repository="menhir", after_hash="def",
    )
    assert repo.calls[0][0] == "refresh"


@pytest.mark.unit
def test_create_never_registers_from_the_event_alone() -> None:
    """The event carries no document metadata; a filename is not an identity."""
    repo = _Repo()
    result = _adapter(repo).reconcile_file_event_source(
        path=".agent/plans/new.md", operation="create", repository="menhir",
    )
    assert result == {"attempted": False, "reason": "operation_not_reconciled:create"}
    assert not repo.calls


@pytest.mark.unit
def test_a_delete_does_not_mark_anything_unresolved_from_the_event() -> None:
    """Missing-source marking belongs to the audit, which can see the whole corpus."""
    repo = _Repo()
    result = _adapter(repo).reconcile_file_event_source(
        path=".agent/plans/a.md", operation="delete", repository="menhir",
    )
    assert result["attempted"] is False
    assert not repo.calls


@pytest.mark.unit
def test_a_non_corpus_path_is_not_attempted() -> None:
    repo = _Repo()
    result = _adapter(repo).reconcile_file_event_source(
        path="src/menhir/main.py", operation="edit", repository="menhir",
    )
    assert result == {"attempted": False, "reason": "path_is_not_corpus_material"}
    assert not repo.calls


@pytest.mark.unit
def test_a_repository_error_is_returned_not_raised() -> None:
    class _Broken(_Repo):
        def refresh_artifact_source_by_locator(self, **kwargs):
            raise RuntimeError("boom")

    result = _adapter(_Broken()).reconcile_file_event_source(
        path=".agent/plans/a.md", operation="edit", repository="menhir",
    )
    assert result == {"attempted": True, "applied": False, "reason": "error:RuntimeError"}
