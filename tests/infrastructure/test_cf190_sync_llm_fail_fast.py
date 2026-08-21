"""CF-190: the two synchronous LLM seams inherited the OpenAI SDK's defaults.

Neither `sync_llm.make_sync_chat` nor `view_embedder` passed `timeout=` or `max_retries=`, so both
got the SDK defaults. Confirmed on a live constructed client, 2026-08-21:

    Timeout(connect=5.0, read=600, write=600, pool=600)
    max_retries = 2

Worst case ~3 x 600 s ~= 30 minutes for a single call against a server that HANGS rather than
refuses. A refused connection fails fast; a hung one does not, and that is the case these bounds
exist for.

Two siblings in the same package already state the opposite policy: `providers.py` declares
`request_timeout_s = 30.0` and threads it into every client it builds, and `llm.py` sets
`max_retries=0` with the comment "fail fast -- don't block MCP on a down server". Two seams
followed the policy and two did not.

Why each unbounded seam costs a thread rather than just a slow call:

* `sync_llm._complete` runs under `asyncio.to_thread`, so a stall consumes a thread-pool slot.
* `view_embedder.embed` is installed as `MaintenanceScheduler(experience_embed=...)` with
  `experience_counter_enabled` defaulting True, so a stall holds a scheduler worker. Its
  `except Exception` reports only "View counter embed failed; writing BM25-only" -- the operator
  never sees the stall, which is why the bound has to be at construction.

These tests assert the CONSTRUCTED CLIENT's settings rather than the source text, so they cannot
pass against a client that is configured somewhere else and then overridden.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.providers import DEFAULT_REQUEST_TIMEOUT_S

pytestmark = pytest.mark.unit


class _CapturingOpenAI:
    """Stands in for `openai.OpenAI`, recording exactly what the seam passed."""

    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).instances.append(kwargs)
        self.kwargs = kwargs
        self.chat = self
        self.completions = self
        self.embeddings = self

    def create(self, **_: Any) -> Any:  # pragma: no cover - not exercised here
        raise AssertionError("no network call expected in this test")


@pytest.fixture(autouse=True)
def _reset_instances():
    _CapturingOpenAI.instances = []
    yield
    _CapturingOpenAI.instances = []


def test_the_sdk_defaults_really_are_the_opposite_policy() -> None:
    """The premise, asserted rather than assumed. If a future SDK ships fail-fast defaults, the
    bounds below stop being load-bearing and this test says so."""
    from openai import OpenAI

    client = OpenAI(api_key="unused", base_url="http://localhost:9/v1")

    assert client.max_retries == 2
    assert client.timeout.read == 600


@pytest.mark.parametrize("field", ["timeout", "max_retries"])
def test_sync_chat_client_is_bounded(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """The finding, half one. Both bounds must be passed explicitly at construction."""
    import openai

    from menhir.config import MemorySettings
    from menhir.infrastructure import sync_llm

    monkeypatch.setenv("MENHIR_CHAT_PROVIDER", "local")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_CHAT_MODEL", "test-model")
    monkeypatch.setattr(openai, "OpenAI", _CapturingOpenAI)

    chat = sync_llm.make_sync_chat(MemorySettings.from_env())
    assert chat is not None

    # The client is built lazily on first use; reach it without making a network call.
    try:
        chat("system", "user")
    except Exception:
        pass

    assert _CapturingOpenAI.instances, "the seam never constructed a client"
    kwargs = _CapturingOpenAI.instances[0]
    assert field in kwargs, f"{field} not passed; the seam inherits the SDK default"
    if field == "max_retries":
        assert kwargs[field] == 0
    else:
        assert kwargs[field] == DEFAULT_REQUEST_TIMEOUT_S


@pytest.mark.parametrize("field", ["timeout", "max_retries"])
def test_view_embedder_client_is_bounded(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """The finding, half two -- the seam whose failure the operator cannot see."""
    import openai

    from menhir.config import MemorySettings
    from menhir.infrastructure import view_embedder

    monkeypatch.setenv("MENHIR_LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_EMBED_MODEL", "test-embed")
    monkeypatch.setattr(openai, "OpenAI", _CapturingOpenAI)

    embed = view_embedder.make_view_embedder(MemorySettings.from_env())
    if embed is None:
        pytest.skip("view embedder unavailable in this configuration")

    try:
        embed("some text")
    except Exception:
        pass

    assert _CapturingOpenAI.instances, "the seam never constructed a client"
    kwargs = _CapturingOpenAI.instances[0]
    assert field in kwargs, f"{field} not passed; the seam inherits the SDK default"
    if field == "max_retries":
        assert kwargs[field] == 0
    else:
        assert kwargs[field] == DEFAULT_REQUEST_TIMEOUT_S


def test_the_bound_is_the_package_constant_not_a_local_literal() -> None:
    """The invariant, not the instance. Both seams and `ProviderRuntimeDependencies` must agree on
    one number, so changing the policy is one edit rather than four."""
    import ast
    import pathlib

    from menhir.infrastructure.providers import ProviderRuntimeDependencies

    assert ProviderRuntimeDependencies().request_timeout_s == DEFAULT_REQUEST_TIMEOUT_S

    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in ("src/menhir/infrastructure/sync_llm.py", "src/menhir/infrastructure/view_embedder.py"):
        source = (root / rel).read_text(encoding="utf-8")
        assert "DEFAULT_REQUEST_TIMEOUT_S" in source, f"{rel} does not use the shared constant"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "OpenAI"):
                continue
            passed = {kw.arg for kw in node.keywords}
            assert "timeout" in passed and "max_retries" in passed, (
                f"{rel} constructs OpenAI without both bounds: {sorted(passed)}"
            )
