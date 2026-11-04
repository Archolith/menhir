"""Acknowledgeable operator warnings: an ack belongs to one incident and never carries over."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.core.backend_client import BackendClient
from menhir.core.backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin
from menhir.infrastructure import observability as obs
from menhir.infrastructure.telemetry.operator_warnings_store import (
    OperatorWarningSink,
    build_operator_warnings,
)
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.mcp import formatters
from menhir.mcp.formatters import _queue_summary, session_start_warning_lines

pytestmark = [pytest.mark.unit]


class _Status401(Exception):
    status_code = 401


@pytest.fixture(autouse=True)
def _reset():
    def _clean() -> None:
        obs.clear_provider_auth_failure()
        obs.clear_provider_quota_exhaustion()
        obs.set_provider_warning_sink(None)
        formatters._stuck_count_cache = None

    _clean()
    yield
    _clean()


@pytest.fixture
def store(tmp_path) -> McpTelemetryStore:
    return McpTelemetryStore(db_path=tmp_path / "warnings.db")


class _Backend(RuntimeProviderAdminOpsMixin):
    """Runtime-backend shape: real ops mixin over a tmp telemetry store and a stub overview."""

    def __init__(self, store: McpTelemetryStore, *, failed: int = 0, latest: str | None = None) -> None:
        self.telemetry = store
        self.overview = {"failed_count": failed, "latest_failed_at": latest}

    async def _off_loop(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        return dict(self.overview)

    async def get_queue_depth(self) -> int:
        return 0

    async def list_episode_processing(self, **kwargs: Any) -> list:
        return []

    async def scheduler_status_snapshot(self) -> dict[str, Any]:
        return {"running": True}


def _handle(kind: str = "chat") -> obs.LLMCallHandle:
    return obs.start_llm_usage_call(kind=kind, model="m", endpoint="e", operation="x")


def _fail_auth(kind: str = "chat") -> None:
    obs.fail_llm_usage_call(_handle(kind), _Status401("bad key"))


def _succeed(kind: str = "chat") -> None:
    obs.complete_llm_usage_call(_handle(kind))


def _warnings(backend: _Backend) -> list[dict[str, Any]]:
    return asyncio.run(backend.get_operator_warnings(_fresh=True))["warnings"]


def _ack(backend: _Backend, kind: str, subject: str = "", actor: str = "alice") -> dict[str, Any]:
    return asyncio.run(backend.acknowledge_operator_warning(kind, subject, actor))


# --- provider incidents ---------------------------------------------------------------------


def test_ack_then_more_failures_in_same_incident_stays_acked(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _Backend(store)
    _fail_auth()
    assert _ack(backend, "provider_auth")["acknowledged"] == ["chat"]
    _fail_auth()
    _fail_auth()
    (w,) = _warnings(backend)
    assert w["acked"] is True and w["acked_by"] == "alice"
    assert w["occurrences"] == 3
    # An acked warning is not repeated in the add_memory summary.
    assert "WARNING" not in asyncio.run(_queue_summary(backend))


def test_ack_success_then_new_failure_is_unacked(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _Backend(store)
    _fail_auth()
    _ack(backend, "provider_auth")
    _succeed()
    assert _warnings(backend) == []
    _fail_auth()
    (w,) = _warnings(backend)
    assert w["acked"] is False and w["occurrences"] == 1
    summary = asyncio.run(_queue_summary(backend))
    assert "WARNING" in summary and 'acknowledge_warning(kind="provider_auth")' in summary


def test_ack_on_resolved_or_absent_row_is_noop(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _Backend(store)
    assert _ack(backend, "provider_auth")["active"] == 0
    _fail_auth()
    _succeed()
    result = _ack(backend, "provider_auth")
    assert result["active"] == 0 and result["acknowledged"] == []
    assert store.ack_operator_warning("provider_auth", "chat", "alice") is False
    assert store.list_operator_warnings(active_only=False)[0]["acked_at"] is None


def test_restart_returns_the_active_row_and_the_warning(store, tmp_path) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    _fail_auth()
    # Simulate a restart: singletons gone, a new store on the same file.
    obs.clear_provider_auth_failure()
    reopened = McpTelemetryStore(db_path=store.db_path)
    backend = _Backend(reopened)
    (w,) = _warnings(backend)
    assert (w["kind"], w["subject"], w["acked"]) == ("provider_auth", "chat", False)
    assert "WARNING" in asyncio.run(_queue_summary(backend))
    _ack(backend, "provider_auth")
    assert _warnings(_Backend(McpTelemetryStore(db_path=store.db_path)))[0]["acked"] is True


def test_success_resolves_a_row_another_process_recorded_after_the_sink_loaded(store) -> None:
    sink = OperatorWarningSink(store)
    obs.set_provider_warning_sink(sink)
    _succeed()  # loads the (empty) active set
    # Another process (same sidecar file) records a billing failure this sink never saw.
    McpTelemetryStore(db_path=store.db_path).record_operator_warning_occurrence("provider_billing", "chat")
    sink.RELOAD_S = 0.0
    _succeed()
    assert store.list_operator_warnings(active_only=True) == []


def test_billing_success_of_other_kind_does_not_resolve_but_any_success_resolves_auth(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _Backend(store)
    obs.record_provider_quota_exhaustion(_handle("chat"))
    _fail_auth("chat")
    _succeed("embedding")
    by_kind = {w["kind"]: w for w in _warnings(backend)}
    assert "provider_billing" in by_kind  # embedding success must not clear chat billing
    assert "provider_auth" in by_kind  # auth clears per call kind (#488 F3)
    _succeed("chat")
    assert _warnings(backend) == []


def test_sink_errors_never_reach_the_llm_call_path() -> None:
    class _Broken:
        def record(self, *a: Any) -> None:
            raise RuntimeError("boom")

        def resolve(self, *a: Any) -> None:
            raise RuntimeError("boom")

    obs.set_provider_warning_sink(_Broken())
    _fail_auth()
    assert obs.last_provider_auth_failure() is not None
    _succeed()
    assert obs.last_provider_auth_failure() is None


# --- failed_episodes watermark --------------------------------------------------------------


def test_failed_episodes_watermark(store) -> None:
    t1, t2 = "2026-11-03T10:00:00.000000+00:00", "2026-11-03T11:00:00.000000+00:00"
    backend = _Backend(store, failed=2, latest=t1)
    (w,) = _warnings(backend)
    assert w["kind"] == "failed_episodes" and w["acked"] is False
    assert _ack(backend, "failed_episodes")["acknowledged"] == ["global"]
    assert _warnings(backend)[0]["acked"] is True
    assert "WARNING" not in asyncio.run(_queue_summary(backend))
    # An episode fails later: the ack does not cover it.
    backend.overview = {"failed_count": 3, "latest_failed_at": t2}
    assert _warnings(backend)[0]["acked"] is False
    # Count reaches zero: no warning at all.
    backend.overview = {"failed_count": 0, "latest_failed_at": None}
    assert _warnings(backend) == []
    assert _ack(backend, "failed_episodes")["active"] == 0


def test_failed_episodes_same_newest_time_stays_acked_and_nanosecond_strings_compare() -> None:
    rows = [{
        "kind": "failed_episodes", "subject": "global", "state": "active",
        "acked_at": "x", "acked_by": "a", "ack_watermark": "2026-11-03T10:00:00.123456789Z",
    }]
    same = build_operator_warnings(rows, failed_count=1, latest_failed_at="2026-11-03T10:00:00.123456+00:00")
    newer = build_operator_warnings(rows, failed_count=1, latest_failed_at="2026-11-03T10:00:00.123457+00:00")
    assert same[0]["acked"] is True and newer[0]["acked"] is False


# --- no failures ----------------------------------------------------------------------------


def test_no_failures_no_warnings_and_no_extra_lines(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _Backend(store)
    assert _warnings(backend) == []
    summary = asyncio.run(_queue_summary(backend))
    assert summary == "queue_depth=0, active_enriching=0, scheduler=running"
    assert asyncio.run(session_start_warning_lines(backend)) == []
    _succeed()  # a success with nothing active touches nothing
    assert store.list_operator_warnings(active_only=False) == []


def test_session_start_surfaces_show_unacked_and_skip_benchmark_mode(store, monkeypatch) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _Backend(store)
    _fail_auth()
    assert len(asyncio.run(session_start_warning_lines(backend))) == 1
    monkeypatch.setenv("MENHIR_BENCHMARK_MODE", "1")
    assert asyncio.run(session_start_warning_lines(backend)) == []
    monkeypatch.delenv("MENHIR_BENCHMARK_MODE")
    _ack(backend, "provider_auth")
    assert asyncio.run(session_start_warning_lines(backend)) == []


# --- bridge and fallback --------------------------------------------------------------------


def test_backend_client_exposes_both_ops() -> None:
    client = BackendClient.__new__(BackendClient)
    calls: list[tuple[str, Any]] = []

    async def _request(operation: str, payload: Any = None) -> Any:
        calls.append((operation, payload))
        return {"warnings": []}

    client._request = _request  # type: ignore[method-assign]
    asyncio.run(client.get_operator_warnings())
    asyncio.run(client.acknowledge_operator_warning("provider_auth", "chat", "bob"))
    assert calls == [
        ("get_operator_warnings", None),
        ("acknowledge_operator_warning", {"kind": "provider_auth", "subject": "chat", "actor": "bob"}),
    ]


def test_backend_exception_falls_back_to_legacy_text() -> None:
    class _Failing:
        async def get_queue_depth(self) -> int:
            return 0

        async def list_episode_processing(self, **kwargs: Any) -> list:
            return []

        async def scheduler_status_snapshot(self) -> dict[str, Any]:
            return {"running": True}

        async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
            return {"failed_count": 2}

        async def get_operator_warnings(self) -> dict[str, Any]:
            raise ConnectionError("serve is older / unreachable")

    _fail_auth()
    summary = asyncio.run(_queue_summary(_Failing()))
    assert "rejected our credentials" in summary
    assert "2 earlier memories are FAILED and NOT recallable" in summary
    assert "acknowledge_warning" not in summary  # nothing to ack against


# --- tool and tiers -------------------------------------------------------------------------


def _tool(backend: Any):
    from menhir.mcp.tools.ops.acknowledge_warning import AcknowledgeWarningTool

    tool = AcknowledgeWarningTool()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


def test_tool_is_operator_tier_and_registered() -> None:
    from menhir.access_contract import OPERATOR_REQUIRED_TOOLS
    from menhir.api.routes_support import _BACKEND_METHODS, _required_tier_for_operation
    from menhir.mcp.tools.ops.acknowledge_warning import AcknowledgeWarningTool

    assert AcknowledgeWarningTool.required_tier == "operator"
    assert AcknowledgeWarningTool.read_only_hint is False
    assert "acknowledge_warning" in OPERATOR_REQUIRED_TOOLS
    assert {"get_operator_warnings", "acknowledge_operator_warning"} <= _BACKEND_METHODS
    assert _required_tier_for_operation("acknowledge_operator_warning") == "operator"
    assert _required_tier_for_operation("get_operator_warnings") == "readonly"


def test_tool_rejects_unknown_kind_and_reports_nothing_active(store) -> None:
    backend = _Backend(store)
    tool = _tool(backend)
    assert "unknown warning kind" in asyncio.run(tool.endpoint(kind="bogus"))
    assert asyncio.run(tool.endpoint(kind="provider_auth")).startswith("nothing to acknowledge")


def test_tool_acks_with_caller_identity_and_empty_subject_acks_all(store, monkeypatch) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    monkeypatch.setattr(
        "menhir.mcp.tools.ops.acknowledge_warning.get_request_session",
        lambda: SimpleNamespace(client_name="claude-ops", user_id="u"),
    )
    backend = _Backend(store)
    _fail_auth("chat")
    _fail_auth("embedding")
    out = asyncio.run(_tool(backend).endpoint(kind="provider_auth"))
    assert "chat" in out and "embedding" in out
    assert {w["acked_by"] for w in _warnings(backend)} == {"claude-ops"}
    again = asyncio.run(_tool(backend).endpoint(kind="provider_auth"))
    assert "already acknowledged" in again
