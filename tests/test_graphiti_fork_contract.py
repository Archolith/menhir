"""Regressions at the fork dependency, request and task boundaries."""

from __future__ import annotations

import asyncio
import re
import tomllib
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from graphiti_core.prompts.models import Message

from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_llm_adapter import MenhirOpenAIGenericClient
from menhir.infrastructure.graphiti_resolution_policy import (
    MenhirNodePreResolutionHook,
    _resolution_telemetry,
    start_resolution_telemetry,
)

ROOT = Path(__file__).resolve().parents[1]


def test_installed_fork_and_lock_match_the_immutable_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [dep for dep in project["project"]["dependencies"] if dep.startswith("graphiti-core @ ")]
    assert len(pins) == 1
    match = re.fullmatch(r"graphiti-core @ git\+https://github\.com/Archolith/graphiti\.git@([0-9a-f]{40})", pins[0])
    assert match is not None
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == "graphiti-core")
    assert package["source"]["git"].endswith("#" + match[1])
    assert package["version"] == version("graphiti-core") == "0.30.2"


@pytest.mark.asyncio
async def test_retry_adapter_preserves_namespace_and_operation() -> None:
    client = MenhirOpenAIGenericClient.__new__(MenhirOpenAIGenericClient)
    client.max_tokens = 100
    client._generate_response = AsyncMock(side_effect=[ValueError("bad JSON"), {"ok": True}])
    result = await client.generate_response(
        [Message(role="system", content="Return JSON")],
        group_id="tenant_a", prompt_name="dedupe_nodes.resolve",
    )
    assert result == {"ok": True}
    assert client._generate_response.await_count == 2
    for call in client._generate_response.call_args_list:
        assert call.kwargs["group_id"] == "tenant_a"
        assert call.kwargs["prompt_name"] == "dedupe_nodes.resolve"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, RuntimeError, asyncio.CancelledError])
async def test_resolution_telemetry_flushes_inside_request_task(monkeypatch, error) -> None:
    import menhir.infrastructure.telemetry.recorders as recorders

    events = []
    monkeypatch.setattr(recorders, "record_lifecycle_event", lambda **event: events.append(event))
    token = _resolution_telemetry.set(None)
    try:
        client = GraphitiClient(client=SimpleNamespace())

        async def request():
            start_resolution_telemetry()
            await MenhirNodePreResolutionHook().pre_resolve_node(SimpleNamespace(
                extracted_node=SimpleNamespace(uuid="no-candidates", attributes={}),
            ))
            await asyncio.sleep(0)
            if error is not None:
                raise error()
            return "done"

        if error is None:
            assert await client._await_add_episode_request(awaitable=request()) == "done"
        else:
            with pytest.raises(error):
                await client._await_add_episode_request(awaitable=request())
        assert len(events) == 1
        assert events[0]["details"]["extracted_node_count"] == 1
        assert events[0]["details"]["no_candidates_new"] == 1
        assert _resolution_telemetry.get() is None
    finally:
        _resolution_telemetry.reset(token)
