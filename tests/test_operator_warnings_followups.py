"""PR #488 review follow-ups: shared FAILED cache, ack carry-over, auth per kind, exposure, never-raise."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.core import backend_runtime_admin_ops as admin_ops
from menhir.core.backend_runtime import RuntimeProvider
from menhir.core.request_context import (
    bind_request_session,
    bind_request_tier,
    reset_request_session,
    reset_request_tier,
)
from menhir.domain.session import new_session
from menhir.infrastructure import observability as obs
from menhir.infrastructure.telemetry.operator_warnings_store import OperatorWarningSink
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.mcp import formatters
from menhir.mcp.formatters import _queue_summary, session_start_warning_lines
from menhir.mcp.tools.ops.get_memory_stats import _warning_section

pytestmark = [pytest.mark.unit]

T1 = "2026-11-03T10:00:00.000000+00:00"


class _Status401(Exception):
    status_code = 401


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    def _clean() -> None:
        obs.clear_provider_auth_failure()
        obs.clear_provider_quota_exhaustion()
        obs.set_provider_warning_sink(None)
        formatters._stuck_count_cache = None
        admin_ops._failed_overview_cache = None

    monkeypatch.delenv("MENHIR_BENCHMARK_MODE", raising=False)
    monkeypatch.delenv("MENHIR_CLIENT_NAMESPACES", raising=False)
    _clean()
    yield
    _clean()


@pytest.fixture
def store(tmp_path) -> McpTelemetryStore:
    return McpTelemetryStore(db_path=tmp_path / "warnings.db")


class _Graph:
    """Counts full-graph overview aggregates; ``fail`` makes the next ones raise."""

    def __init__(self, failed: int = 0, latest: str | None = None) -> None:
        self.calls = 0
        self.fail = False
        self.overview = {"failed_count": failed, "latest_failed_at": latest}

    def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("neo4j slow")
        return dict(self.overview)


def _provider(store: McpTelemetryStore, graph: _Graph) -> RuntimeProvider:
    """A fresh RuntimeProvider, the way every MCP and REST request builds one."""
    return RuntimeProvider(
        SimpleNamespace(graph_adapter=graph),
        process_session=SimpleNamespace(session_id="s"),
        telemetry=store,
    )


def _handle(kind: str = "chat") -> obs.LLMCallHandle:
    return obs.start_llm_usage_call(kind=kind, model="m", endpoint="e", operation="x")


def _fail_auth(kind: str = "chat") -> None:
    obs.fail_llm_usage_call(_handle(kind), _Status401("bad key"))


def _succeed(kind: str = "chat") -> None:
    obs.complete_llm_usage_call(_handle(kind))


def _warnings(backend: RuntimeProvider, fresh: bool = True) -> list[dict[str, Any]]:
    return asyncio.run(backend.get_operator_warnings(_fresh=fresh))["warnings"]


def _ack(backend: RuntimeProvider, kind: str, subject: str = "") -> dict[str, Any]:
    return asyncio.run(backend.acknowledge_operator_warning(kind, subject, "alice"))


# --- F1 + F7: one FAILED aggregate per TTL, shared across instances --------------------------


def test_fresh_provider_instances_share_one_overview_call(store) -> None:
    graph = _Graph(failed=2, latest=T1)
    for _ in range(5):
        (w,) = _warnings(_provider(store, graph), fresh=False)
        assert w["kind"] == "failed_episodes" and w["count"] == 2
    assert graph.calls == 1


def test_ack_still_reads_the_overview_fresh(store) -> None:
    graph = _Graph(failed=2, latest=T1)
    _warnings(_provider(store, graph), fresh=False)
    _ack(_provider(store, graph), "failed_episodes")
    assert graph.calls == 2


def test_overview_failure_keeps_the_last_count_and_is_not_retried_within_the_ttl(store) -> None:
    graph = _Graph(failed=2, latest=T1)
    _warnings(_provider(store, graph), fresh=False)
    admin_ops._failed_overview_cache = (0.0, *admin_ops._failed_overview_cache[1:])  # expire it
    graph.fail = True
    (w,) = _warnings(_provider(store, graph), fresh=False)
    assert w["kind"] == "failed_episodes" and w["count"] == 2
    _warnings(_provider(store, graph), fresh=False)
    assert graph.calls == 2  # the failed read is held for the TTL, not re-run per call
    summary = asyncio.run(_queue_summary(_Writer(_provider(store, graph))))
    assert "2 earlier memories are FAILED" in summary


# --- F2: an ack never carries into the next incident -----------------------------------------


class _HookedStore:
    """Delegates to the real store; runs ``after_resolve`` once, or raises once, on the UPDATE."""

    def __init__(self, inner: McpTelemetryStore) -> None:
        self._inner = inner
        self.after_resolve = None
        self.raise_once = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def resolve_operator_warning(self, *args: Any) -> int:
        if self.raise_once:
            self.raise_once = False
            raise sqlite3.OperationalError("database is locked")
        changed = self._inner.resolve_operator_warning(*args)
        hook, self.after_resolve = self.after_resolve, None
        if hook is not None:
            hook()
        return changed


def test_failure_recorded_during_the_resolve_does_not_let_an_ack_carry_over(store) -> None:
    hooked = _HookedStore(store)
    sink = OperatorWarningSink(hooked)
    obs.set_provider_warning_sink(sink)
    backend = _provider(store, _Graph())
    _fail_auth()
    # The failure lands after the UPDATE and before the sink's post-step.
    hooked.after_resolve = lambda: sink.record("provider_auth", "chat")
    _succeed()
    assert [r["subject"] for r in store.list_operator_warnings()] == ["chat"]
    assert _ack(backend, "provider_auth")["acknowledged"] == ["chat"]
    _succeed()  # the outage clears: the live row must be resolved, not missed
    assert store.list_operator_warnings() == []
    _fail_auth()  # recurrence
    (w,) = _warnings(backend)
    assert (w["subject"], w["acked"], w["occurrences"]) == ("chat", False, 1)


def test_a_resolve_that_raises_once_is_retried_by_the_next_success(store) -> None:
    hooked = _HookedStore(store)
    obs.set_provider_warning_sink(OperatorWarningSink(hooked))
    backend = _provider(store, _Graph())
    _fail_auth()
    _ack(backend, "provider_auth")
    hooked.raise_once = True
    _succeed()  # swallowed: the row stays active
    assert len(store.list_operator_warnings()) == 1
    _succeed()
    assert store.list_operator_warnings() == []
    _fail_auth()
    (w,) = _warnings(backend)
    assert w["acked"] is False and w["occurrences"] == 1


# --- F3: provider_auth resolves per call kind ------------------------------------------------


def test_chat_success_does_not_resolve_an_embedding_auth_row(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    _fail_auth("embedding")
    _succeed("chat")
    assert [(r["kind"], r["subject"]) for r in store.list_operator_warnings()] == [
        ("provider_auth", "embedding")
    ]
    _succeed("embedding")
    assert store.list_operator_warnings() == []


def test_an_acked_embedding_outage_stays_acked_through_chat_successes(store) -> None:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _provider(store, _Graph())
    _fail_auth("embedding")
    _ack(backend, "provider_auth")
    for _ in range(3):
        _succeed("chat")
        _fail_auth("embedding")
    (w,) = [w for w in _warnings(backend) if w["kind"] == "provider_auth"]
    assert w["acked"] is True and w["occurrences"] == 4


# --- F5: pinned callers get no deployment-wide FAILED count; identity is operator-only --------


class _Caller:
    def __init__(self, monkeypatch, *, client_name: str, tier: str, pin: str = "") -> None:
        if pin:
            monkeypatch.setenv("MENHIR_CLIENT_NAMESPACES", f"{client_name}={pin}")
        self._session = new_session("u", client_name=client_name)
        self._tier = tier

    def __enter__(self) -> "_Caller":
        self._tokens = (bind_request_session(self._session), bind_request_tier(self._tier))
        return self

    def __exit__(self, *exc: Any) -> None:
        reset_request_tier(self._tokens[1])
        reset_request_session(self._tokens[0])


def _acked_auth_and_failed(store: McpTelemetryStore) -> RuntimeProvider:
    obs.set_provider_warning_sink(OperatorWarningSink(store))
    backend = _provider(store, _Graph(failed=3, latest=T1))
    _fail_auth()
    _ack(backend, "provider_auth")
    return backend


def test_pinned_readonly_caller_sees_no_failed_episodes_and_no_ack_identity(store, monkeypatch) -> None:
    backend = _acked_auth_and_failed(store)
    with _Caller(monkeypatch, client_name="bot", tier="readonly", pin="game"):
        (w,) = _warnings(backend)
        assert w["kind"] == "provider_auth" and w["acked"] is True
        assert w["acked_by"] is None and w["acked_at"] is None
        assert asyncio.run(session_start_warning_lines(backend)) == []  # auth acked, FAILED hidden
        section = asyncio.run(_warning_section(backend))
    assert section[0] == "### Warnings" and len(section) == 3
    assert "provider_auth/chat" in section[1] and section[1].endswith("[acknowledged]")
    assert "FAILED" not in "\n".join(section) and "alice" not in "\n".join(section)


def test_pinned_caller_on_the_legacy_path_also_gets_no_failed_episodes(monkeypatch) -> None:
    with _Caller(monkeypatch, client_name="bot", tier="readonly", pin="game"):
        lines = asyncio.run(session_start_warning_lines(_OldServe(failed=4)))
        section = asyncio.run(_warning_section(_OldServe(failed=4)))
    assert lines == [] and section == []


def test_unpinned_operator_sees_everything_with_identity(store, monkeypatch) -> None:
    backend = _acked_auth_and_failed(store)
    with _Caller(monkeypatch, client_name="ops", tier="operator"):
        kinds = {w["kind"]: w for w in _warnings(backend)}
        assert set(kinds) == {"provider_auth", "failed_episodes"}
        assert kinds["provider_auth"]["acked_by"] == "alice"
        lines = asyncio.run(session_start_warning_lines(backend))
        section = asyncio.run(_warning_section(backend))
    assert len(lines) == 1 and "3 earlier memories are FAILED" in lines[0]
    assert any("acknowledged by alice at " in line for line in section)
    assert any("failed_episodes/global" in line and "UNACKNOWLEDGED" in line for line in section)


def test_unpinned_readonly_caller_keeps_failed_episodes_but_not_identity(store, monkeypatch) -> None:
    backend = _acked_auth_and_failed(store)
    with _Caller(monkeypatch, client_name="viewer", tier="readonly"):
        kinds = {w["kind"]: w for w in _warnings(backend)}
    assert set(kinds) == {"provider_auth", "failed_episodes"}
    assert kinds["provider_auth"]["acked"] is True and kinds["provider_auth"]["acked_by"] is None


def test_ack_by_a_pinned_operator_still_reads_the_unredacted_set(store, monkeypatch) -> None:
    backend = _provider(store, _Graph(failed=1, latest=T1))
    with _Caller(monkeypatch, client_name="ops", tier="operator", pin="game"):
        assert _ack(backend, "failed_episodes")["acknowledged"] == ["global"]


# --- F6: the warnings block never raises -----------------------------------------------------


class _Writer:
    """Queue-status shape for ``_queue_summary`` over a real or fake warnings source."""

    def __init__(self, source: Any) -> None:
        self._source = source

    async def get_queue_depth(self) -> int:
        return 0

    async def list_episode_processing(self, **kwargs: Any) -> list:
        return []

    async def scheduler_status_snapshot(self) -> dict[str, Any]:
        return {"running": True}

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        return await self._source.fetch_memory_overview(namespace=namespace)

    async def get_operator_warnings(self) -> dict[str, Any]:
        return await self._source.get_operator_warnings()


class _Payload:
    def __init__(self, warnings: Any, failed: int = 0) -> None:
        self.warnings = warnings
        self.failed = failed
        self.warning_calls = 0

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        return {"failed_count": self.failed}

    async def get_operator_warnings(self) -> dict[str, Any]:
        self.warning_calls += 1
        return {"warnings": self.warnings}


class _OldServe(_Writer):
    """An older serve: no get_operator_warnings op, a standing FAILED count."""

    def __init__(self, failed: int) -> None:
        self._failed = failed

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        return {"failed_count": self._failed}

    async def get_operator_warnings(self) -> dict[str, Any]:
        raise RuntimeError("Unknown backend operation: get_operator_warnings")


BASE = "queue_depth=0, active_enriching=0, scheduler=running"
_R4_PAYLOADS = [
    [1],
    [{"kind": "provider_auth"}],
    [{"summary": "x"}],
    [{"kind": "provider_auth", "summary": "x", "acked": False}, "str"],
    [None, ["nested"], {"kind": "failed_episodes", "count": "many"}],
]


@pytest.mark.parametrize("payload", _R4_PAYLOADS)
def test_malformed_entries_never_raise_on_any_surface(payload) -> None:
    source = _Payload(payload)
    summary = asyncio.run(_queue_summary(_Writer(source)))
    assert summary.startswith(BASE)
    assert isinstance(asyncio.run(session_start_warning_lines(source)), list)
    assert isinstance(asyncio.run(_warning_section(source)), list)


def test_well_formed_entry_beside_garbage_still_renders() -> None:
    payload = [{"kind": "provider_auth", "summary": "x", "acked": False}, "str"]
    summary = asyncio.run(_queue_summary(_Writer(_Payload(payload))))
    assert summary.splitlines()[1].startswith("WARNING: x. New memories are stored")


def test_unknown_kind_renders_its_summary_without_an_ack_hint_or_is_skipped() -> None:
    payload = [{"kind": "future_kind", "subject": "x", "summary": "a new problem"}, {"kind": "other"}]
    summary = asyncio.run(_queue_summary(_Writer(_Payload(payload))))
    assert summary == BASE + "\nWARNING: a new problem."


def test_a_raising_renderer_is_contained(monkeypatch) -> None:
    def _boom(entry: Any) -> str:
        raise RuntimeError("render bug")

    monkeypatch.setattr(formatters, "_operator_warning_line", _boom)
    payload = [{"kind": "provider_auth", "summary": "x"}]
    assert asyncio.run(_queue_summary(_Writer(_Payload(payload)))) == BASE
    assert asyncio.run(session_start_warning_lines(_Payload(payload))) == []


# --- F13: benchmark mode keeps the legacy add_memory text byte for byte ----------------------

_LEGACY_AUTH = (
    "\nWARNING: {summary}. New memories are stored but cannot be "
    "enriched or recalled until the provider credential (e.g. OPENAI_API_KEY) is fixed; "
    "failed episodes are retried automatically once a call succeeds."
)
_LEGACY_FAILED = (
    "\nWARNING: 2 earlier memories are FAILED and NOT recallable "
    "-- the text is stored but has no entities, so recall cannot return it. "
    "This write may end the same way; check get_enrichment_status(episode_id) if it matters."
)


def test_benchmark_mode_queue_summary_is_the_legacy_text_exactly(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_BENCHMARK_MODE", "1")
    _fail_auth()
    source = _Payload([{"kind": "provider_auth", "summary": "acked elsewhere", "acked": True}], failed=2)
    summary = asyncio.run(_queue_summary(_Writer(source)))
    auth = obs.last_provider_auth_failure().summary()
    assert summary == BASE + _LEGACY_AUTH.format(summary=auth) + _LEGACY_FAILED
    assert source.warning_calls == 0
    assert "acknowledge_warning" not in summary
