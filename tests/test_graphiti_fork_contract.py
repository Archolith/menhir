"""Regressions at the fork dependency, request and task boundaries."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import os
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    assert "archolith-graphiti-core==0.30.2.post4" in project["project"]["dependencies"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == "archolith-graphiti-core")
    assert package["source"] == {"registry": "https://pypi.org/simple"}
    assert package["version"] == version("archolith-graphiti-core") == "0.30.2.post4"


def test_adapter_reports_missing_fork_symbol_as_mixed_install(monkeypatch) -> None:
    """A stale shared package should explain the repair before other imports run."""
    adapter_path = ROOT / "src" / "menhir" / "infrastructure" / "graphiti_llm_adapter.py"
    spec = importlib.util.spec_from_file_location("_adapter_missing_fork_symbol", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    original_import = builtins.__import__

    def import_without_fork_error(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "graphiti_core.errors" and "GraphitiRequestTooLargeError" in fromlist:
            return ModuleType("graphiti_core.errors")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_fork_error)
    with pytest.raises(ImportError, match="graphiti_core.errors is missing") as exc_info:
        spec.loader.exec_module(adapter)
    assert "archolith-graphiti-core==0.30.2.post4" in str(exc_info.value)
    assert "fresh virtual environment" in str(exc_info.value)


def test_cold_graphiti_client_import_reports_mixed_install() -> None:
    """The ordinary application import must diagnose overwritten fork files."""
    script = """
import builtins
import types

original = builtins.__import__
def mixed(name, globals=None, locals=None, fromlist=(), level=0):
    module = original(name, globals, locals, fromlist, level)
    if fromlist and 'GraphitiRequestTooLargeError' in fromlist:
        stale = types.ModuleType(module.__name__)
        stale.__dict__.update({key: value for key, value in module.__dict__.items()
                               if key != 'GraphitiRequestTooLargeError'})
        return stale
    return module
builtins.__import__ = mixed
try:
    import menhir.infrastructure.graphiti_client
except ImportError as exc:
    assert 'archolith-graphiti-core==0.30.2.post4' in str(exc), str(exc)
    assert 'fresh virtual environment' in str(exc), str(exc)
else:
    raise AssertionError('mixed installation was accepted')
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1", "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_retry_adapter_preserves_namespace_and_operation() -> None:
    client = MenhirOpenAIGenericClient.__new__(MenhirOpenAIGenericClient)
    client.structured_output_mode = "json_schema"
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
