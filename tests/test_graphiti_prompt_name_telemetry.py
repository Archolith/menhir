"""Regression tests for Graphiti prompt_name -> llm_usage_events.operation telemetry (#91)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from graphiti_core.prompts.models import Message

from menhir.infrastructure.graphiti_llm_adapter import MenhirOpenAIGenericClient
from menhir.infrastructure.observability import (
    LLMUsageEvent,
    _InstrumentedEndpoint,
    reset_llm_operation_label,
    reset_llm_usage_callback,
    set_llm_operation_label,
    set_llm_usage_callback,
)

pytestmark = [pytest.mark.unit]


def _fake_chat_response() -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1200,
            completion_tokens=40,
            total_tokens=1240,
            prompt_tokens_details=SimpleNamespace(cached_tokens=1024),
        ),
        service_tier="flex",
    )


@pytest.mark.asyncio
async def test_instrumented_endpoint_uses_active_operation_label_for_chat_only():
    events: list[LLMUsageEvent] = []
    cb_token = set_llm_usage_callback(events.append)
    try:
        chat_inner = SimpleNamespace(create=AsyncMock(return_value=_fake_chat_response()))
        embed_inner = SimpleNamespace(create=AsyncMock(return_value=_fake_chat_response()))
        chat_ep = _InstrumentedEndpoint(
            chat_inner, event_type="chat", endpoint="chat.completions.create"
        )
        embed_ep = _InstrumentedEndpoint(
            embed_inner, event_type="embedding", endpoint="embeddings.create"
        )

        # Unset label falls back to endpoint name.
        await chat_ep.create(model="gpt-6-luna")
        assert [e.operation for e in events if e.phase == "completed"] == [
            "chat.completions.create"
        ]

        # Active label applies to chat calls, never to embedding calls.
        op_token = set_llm_operation_label("dedupe_edges.resolve_edge")
        try:
            await chat_ep.create(model="gpt-6-luna")
            await embed_ep.create(model="text-embedding-3-small")
        finally:
            reset_llm_operation_label(op_token)

        completed_ops = [(e.kind, e.operation) for e in events if e.phase == "completed"]
        assert completed_ops == [
            ("chat", "chat.completions.create"),
            ("chat", "dedupe_edges.resolve_edge"),
            ("embedding", "embeddings.create"),
        ]

        # After reset, fallback resumes.
        await chat_ep.create(model="gpt-6-luna")
        assert events[-1].operation == "chat.completions.create"
    finally:
        reset_llm_usage_callback(cb_token)


@pytest.mark.asyncio
async def test_menhir_client_propagates_prompt_name_to_usage_operation_and_resets():
    events: list[LLMUsageEvent] = []
    cb_token = set_llm_usage_callback(events.append)
    try:
        chat_inner = SimpleNamespace(create=AsyncMock(return_value=_fake_chat_response()))
        chat_ep = _InstrumentedEndpoint(
            chat_inner, event_type="chat", endpoint="chat.completions.create"
        )

        client = MenhirOpenAIGenericClient.__new__(MenhirOpenAIGenericClient)
        client.structured_output_mode = "json_schema"
        client.max_tokens = 100

        async def fake_generate(*args, **kwargs):
            await chat_ep.create(model="gpt-6-luna")
            return {"duplicate_facts": [], "contradicted_facts": []}

        client._generate_response = fake_generate  # type: ignore[method-assign]

        result = await client.generate_response(
            [Message(role="system", content="s"), Message(role="user", content="u")],
            prompt_name="dedupe_edges.resolve_edge",
        )
        assert result == {"duplicate_facts": [], "contradicted_facts": []}
        assert [e.operation for e in events if e.phase == "completed"] == [
            "dedupe_edges.resolve_edge"
        ]

        # Calling chat endpoint afterwards must not see stale label.
        await chat_ep.create(model="gpt-6-luna")
        assert events[-1].operation == "chat.completions.create"
    finally:
        reset_llm_usage_callback(cb_token)


@pytest.mark.asyncio
async def test_menhir_client_resets_operation_label_on_error():
    events: list[LLMUsageEvent] = []
    cb_token = set_llm_usage_callback(events.append)
    try:
        chat_inner = SimpleNamespace(create=AsyncMock(return_value=_fake_chat_response()))
        chat_ep = _InstrumentedEndpoint(
            chat_inner, event_type="chat", endpoint="chat.completions.create"
        )

        client = MenhirOpenAIGenericClient.__new__(MenhirOpenAIGenericClient)
        client.structured_output_mode = "json_schema"
        client.max_tokens = 100
        client._generate_response = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="boom"):
            await client.generate_response(
                [Message(role="system", content="s"), Message(role="user", content="u")],
                prompt_name="dedupe_nodes.nodes",
            )

        # Label must be cleared even after failure.
        await chat_ep.create(model="gpt-6-luna")
        assert events[-1].operation == "chat.completions.create"
    finally:
        reset_llm_usage_callback(cb_token)
