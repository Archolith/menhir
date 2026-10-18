"""Chat calls record once; cache-write tokens and service tier reach the usage event and store."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from menhir.infrastructure import observability as obs
from menhir.infrastructure import providers as prov
from menhir.infrastructure.observability import _InstrumentedAsyncOpenAI
from menhir.infrastructure.telemetry.llm_usage_store import initialize_llm_usage_schema
from menhir.mcp.telemetry import McpTelemetryStore, record_llm_usage_event

pytestmark = [pytest.mark.unit]


class _Transport:
    def __init__(self) -> None:
        self.executed = 0

    async def create(self, **kwargs):
        self.executed += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


async def test_instrumented_chat_backend_call_records_one_event_pair() -> None:
    transport = _Transport()
    raw = SimpleNamespace(chat=SimpleNamespace(completions=transport))
    backend = prov.OpenAIStyleChatBackend(
        provider=prov.ProviderConfig(
            kind=prov.ProviderKind.OPENAI,
            base_url="http://fake.invalid/v1",
            api_key="k",
            chat_model="fake-model",
            embed_model="fake-embed",
        ),
        settings=None,  # type: ignore[arg-type]
        dependencies=prov.ProviderRuntimeDependencies(
            openai_client_factory=lambda **_: _InstrumentedAsyncOpenAI(raw)
        ),
    )
    events: list[obs.LLMUsageEvent] = []
    token = obs.set_llm_usage_callback(events.append)
    try:
        await backend.create_chat_completion(
            system_prompt="s", user_prompt="u", operation="adapter_op", max_tokens=8, temperature=0.0
        )
    finally:
        obs.reset_llm_usage_callback(token)

    assert transport.executed == 1
    assert [e.phase for e in events] == ["started", "completed"]
    assert {e.operation for e in events} == {"adapter_op"}
    assert all(e.report_only for e in events)
    assert events[1].total_tokens == 15


def test_normalized_usage_reads_cache_write_tokens() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "total_tokens": 105,
        "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 30},
    }
    normalized = obs._normalized_usage(usage)
    assert normalized["cache_write_input_tokens"] == 30
    assert normalized["cached_input_tokens"] == 40
    assert obs._normalized_usage({"prompt_tokens": 1})["cache_write_input_tokens"] is None


def _complete_with(result) -> obs.LLMUsageEvent:
    events: list[obs.LLMUsageEvent] = []
    token = obs.set_llm_usage_callback(events.append)
    try:
        handle = obs.start_llm_usage_call(kind="chat", model="m", endpoint="e")
        obs.complete_llm_usage_call(handle, result=result)
    finally:
        obs.reset_llm_usage_callback(token)
    return events[-1]


def test_complete_llm_usage_call_records_service_tier_and_drops_malformed() -> None:
    assert _complete_with(SimpleNamespace(usage=None, service_tier="flex")).service_tier == "flex"
    assert _complete_with(SimpleNamespace(usage=None, service_tier="a b; secret")).service_tier is None
    assert _complete_with(SimpleNamespace(usage=None, service_tier="x" * 33)).service_tier is None
    assert _complete_with(SimpleNamespace(usage=None, service_tier=3)).service_tier is None
    assert _complete_with(SimpleNamespace(usage=None)).service_tier is None


def _record(store, call_id: str, **kwargs) -> bool:
    event = obs.LLMUsageEvent(kind="chat", phase="completed", call_id=call_id, **kwargs)
    return record_llm_usage_event(event=event, episode_uuid="ep", store=store)


def test_store_round_trips_cache_write_tokens_and_service_tier(tmp_path) -> None:
    store = McpTelemetryStore(db_path=tmp_path / "t.db")
    assert _record(store, "c1", cache_write_input_tokens=30, service_tier="flex")
    assert _record(store, "c2", cache_write_input_tokens=5, service_tier="bad tier!")

    summary = store.fetch_llm_usage_summary()
    assert summary["cache_write_input_tokens"] == 35
    assert summary["by_model"][0]["cache_write_input_tokens"] == 35
    with closing(sqlite3.connect(tmp_path / "t.db")) as conn:
        tiers = dict(conn.execute("SELECT call_id, service_tier FROM llm_usage_events"))
    assert tiers == {"c1": "flex", "c2": None}


def test_existing_table_gains_the_new_columns() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(
            "CREATE TABLE llm_usage_events (call_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,"
            " run_id TEXT, episode_uuid TEXT, error_code TEXT)"
        )
        initialize_llm_usage_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage_events)")}
    assert {"cache_write_input_tokens", "service_tier"} <= columns
