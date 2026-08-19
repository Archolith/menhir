"""Focused regressions for the CF-165 end-to-end closure pass.

All persistence uses tmp_path or tiny fakes. No production graph, telemetry DB, or LLM is touched.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.erasure_purge import (
    ErasureSubjects,
    count_unaddressable_content,
    purge_content,
)
from menhir.infrastructure.telemetry.helpers import _safe_preview_of
from menhir.infrastructure.telemetry.recorders import record_llm_usage_event, record_merge
from menhir.infrastructure.telemetry.schema_migrations import ensure_lineage_columns
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import (
    DELETION_SUCCEEDED_REASONS,
    ERASED,
    ERASED_INCOMPLETE,
    GRAPH_ALREADY_ABSENT,
    ErasureCoordinator,
)

pytestmark = [pytest.mark.unit]


def test_safe_preview_removes_prose_and_rejects_fake_structural_prose() -> None:
    rendered = json.loads(
        _safe_preview_of(
            {
                "text": "my private memory is here",
                "diff": "password=hunter2",
                "query": "tell me about the private memory",
                "namespace": "tenant-a",
                "source": "claude-code",
                "node_uuid": "node-1",
                "nested": {"reason": "a long private explanation"},
            }
        )
    )
    assert rendered["text"] == "[redacted]"
    assert rendered["diff"] == "[redacted]"
    assert rendered["query"] == "[redacted]"
    assert rendered["nested"]["reason"] == "[redacted]"
    assert rendered["namespace"] == "tenant-a"
    assert rendered["source"] == "claude-code"
    assert rendered["node_uuid"] == "node-1"

    # Telemetry is captured before endpoint validation, so a structural key is not sufficient
    # reason to trust an arbitrary string value.
    forged = json.loads(_safe_preview_of({"namespace": "tenant-a secret sentence"}))
    assert forged["namespace"] == "[redacted]"


@pytest.mark.asyncio
async def test_denied_mcp_call_persists_only_sanitized_lineage(monkeypatch) -> None:
    from menhir.mcp.telemetry.tracker import track_mcp_call
    import menhir.mcp.service_access as service_access

    monkeypatch.setattr(service_access, "get_pinned_namespace", lambda: "tenant-pinned")

    class FakeStore:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def record(self, **kwargs) -> None:
            self.rows.append(dict(kwargs))

    async def denied():
        raise PermissionError("request included my private memory")

    store = FakeStore()
    result = await track_mcp_call(
        kind="tool",
        operation="add_memory",
        payload={"text": "my private memory", "namespace": "tenant-other"},
        runner=denied,
        store=store,
        error_mapper=lambda error: f"mapped:{error}",
    )

    assert result.startswith("mapped:")
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row["namespace"] == "tenant-pinned"
    assert row["node_uuid"] is None
    assert "my private memory" not in (row["payload_preview"] or "")
    assert "[redacted]" in (row["payload_preview"] or "")
    assert row["error"] == "PermissionError"


def test_mcp_event_is_namespace_erasable_without_node_uuid(tmp_path) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store.record(
        kind="tool",
        operation="recall_memories",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_ms=1,
        success=True,
        error=None,
        input_size=1,
        result_size=1,
        payload_preview='{"query":"legacy private text"}',
        namespace="tenant-a",
        node_uuid=None,
    )

    with sqlite3.connect(db) as conn:
        assert "mcp_events.payload_preview" not in count_unaddressable_content(conn)
        result = purge_content(
            conn,
            ErasureSubjects(namespaces=frozenset({"tenant-a"})),
            dry_run=False,
        )
        conn.commit()
        assert result.rows_affected["mcp_events.payload_preview"] == 1
        value = conn.execute("SELECT payload_preview FROM mcp_events").fetchone()[0]
    assert value is None


def test_extraction_lab_store_never_creates_new_null_lineage(tmp_path) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)

    store.record_extraction_lab_run(
        request_payload={"current_message": "private A", "source_namespace": "tenant-a"},
        result_payload={"current_message": "private A", "arms": []},
    )
    store.record_extraction_lab_run(
        request_payload={"current_message": "synthetic fixture"},
        result_payload={"current_message": "synthetic fixture", "arms": []},
    )

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT namespace FROM extraction_lab_runs ORDER BY id"
        ).fetchall()
        stranded = count_unaddressable_content(conn)
    assert rows == [("tenant-a",), ("__extraction_lab_unscoped__",)]
    assert not any(key.startswith("extraction_lab_runs.") for key in stranded)


def test_recall_feedback_prose_is_scrubbed_by_migration(tmp_path) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store._ensure_ready()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO recall_receipts "
            "(token, operation, client_id, session_id, created_at, reason) "
            "VALUES (?,?,?,?,?,?)",
            ("r-1", "recall_memories", "c", "s", "t", "private feedback prose"),
        )
        ensure_lineage_columns(conn)
        conn.commit()
        reason = conn.execute(
            "SELECT reason FROM recall_receipts WHERE token='r-1'"
        ).fetchone()[0]
    assert reason is None


def test_incomplete_erasure_is_not_boolean_success() -> None:
    assert ERASED in DELETION_SUCCEEDED_REASONS
    assert GRAPH_ALREADY_ABSENT in DELETION_SUCCEEDED_REASONS
    assert ERASED_INCOMPLETE not in DELETION_SUCCEEDED_REASONS


def test_non_episode_llm_usage_has_non_null_key_and_sanitized_payload() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.row = None

        def record_llm_usage_event(self, **kwargs):
            self.row = dict(kwargs)
            return True

    store = FakeStore()
    event = SimpleNamespace(
        phase="completed",
        call_id="call-1",
        provider_usage={"prompt_tokens": 12, "raw_prompt": "private prompt"},
        operation="judge",
        kind="chat",
        model="model-x",
        endpoint="chat",
        duration_ms=5,
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
        cached_input_tokens=0,
        reasoning_output_tokens=0,
        error=None,
    )

    assert record_llm_usage_event(event=event, store=store) is True
    assert store.row is not None
    assert store.row["episode_uuid"] == "__non_episode__"
    provider = json.loads(store.row["provider_usage_json"])
    assert provider["prompt_tokens"] == 12
    assert provider["raw_prompt"] == "[redacted]"


def test_merge_audit_derives_both_namespace_keys_from_snapshot() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.row = None

        def record_merge(self, **kwargs) -> None:
            self.row = dict(kwargs)

    store = FakeStore()
    record_merge(
        survivor_uuid="s",
        absorbed_uuid="a",
        similarity=0.99,
        snapshot_json=json.dumps({"properties": {"namespace": "tenant-a"}}),
        store=store,
    )
    assert store.row is not None
    assert store.row["survivor_namespace"] == "tenant-a"
    assert store.row["absorbed_namespace"] == "tenant-a"


class _ErasureAdapter:
    def __init__(self, *, fail_replay: bool = False) -> None:
        self.fail_replay = fail_replay
        self.deleted_namespaces: list[str] = []
        self.purged_turn_evidence: list[str] = []

    def capture_namespace_uuids(self, group_id: str, *, namespace: str | None = None):
        return []

    def delete_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        if self.fail_replay:
            raise RuntimeError("graph unavailable")
        self.deleted_namespaces.append(group_id)
        return 1

    def purge_turn_evidence(self, namespace: str) -> int:
        self.purged_turn_evidence.append(namespace)
        return 1

    def delete_memory(self, node_uuid: str) -> bool:
        return False


def _erasure(tmp_path, adapter: _ErasureAdapter) -> ErasureCoordinator:
    db = tmp_path / "telemetry.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    return ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )


def test_namespace_erasure_purges_turn_evidence_inside_saga(tmp_path) -> None:
    adapter = _ErasureAdapter()
    coord = _erasure(tmp_path, adapter)
    out = coord.erase_namespace("graph-group-a", namespace="tenant-a")
    assert out["reason"] == ERASED
    assert adapter.deleted_namespaces == ["graph-group-a"]
    assert adapter.purged_turn_evidence == ["tenant-a"]


def test_replay_graph_failure_does_not_commit_erasure(tmp_path) -> None:
    adapter = _ErasureAdapter(fail_replay=True)
    coord = _erasure(tmp_path, adapter)
    op_id = "prepared-op"
    with sqlite3.connect(tmp_path / "telemetry.db") as conn:
        coord.journal.prepare(
            operation_kind="EXPLICIT_ERASURE",
            request_json='{"namespace":"tenant-a"}',
            target_key="erasure:namespace:tenant-a",
            op_id=op_id,
            conn=conn,
        )
        coord.subjects.record_subjects(
            op_id,
            [("NAMESPACE", "tenant-a")],
            conn=conn,
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="graph unavailable"):
        coord.replay_prepared_row({"op_id": op_id})

    with sqlite3.connect(tmp_path / "telemetry.db") as conn:
        state = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0]
    assert state == "PREPARED"
    assert coord.subjects.count_unpurged(op_id) == 1
