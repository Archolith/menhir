"""#577: ScrubbingBackend scrubs every awaited MemoryBackend result once."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from menhir.core.backend_scrub import ScrubbingBackend, wrap_backend
from menhir.domain import payload_scrub
from menhir.domain.payload_scrub import PayloadScrubFailed

pytestmark = pytest.mark.unit

AWS = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class FakeBackend:
    marker = "plain-attr"

    async def recall(self, query, **kwargs):
        return {
            "content": f"key {AWS}",
            "summary": f"tok {GITHUB}",
            "items": [{"facts": [f"nested {AWS}", f"also {GITHUB}"]}],
            "context": f"## Context\n- {AWS}\n- {GITHUB}",
            "count": 3,
            7: "int key",
        }

    async def stream(self):
        yield f"chunk {AWS}"

    def sync_op(self):
        return f"sync {AWS}"


async def test_results_scrubbed():
    backend = wrap_backend(FakeBackend())
    result = await backend.recall("q")
    flat = repr(result)
    assert AWS not in flat and GITHUB not in flat
    assert "[REDACTED:" in flat
    assert "[REDACTED:" in result["context"]


async def test_policy_off_unchanged(monkeypatch):
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    result = await wrap_backend(FakeBackend()).recall("q")
    assert AWS in result["content"] and GITHUB in result["summary"]


async def test_non_string_leaves_and_keys_unchanged():
    result = await wrap_backend(FakeBackend()).recall("q")
    assert result["count"] == 3
    assert result[7] == "int key"
    assert set(result) == {"content", "summary", "items", "context", "count", 7}


async def test_detector_failure_fails_closed(monkeypatch):
    def boom(_text):
        raise RuntimeError("down")

    monkeypatch.setattr(payload_scrub, "redact", boom)
    with pytest.raises(PayloadScrubFailed):
        await wrap_backend(FakeBackend()).recall("q")


async def test_passthrough_attrs_generators_and_sync():
    backend = wrap_backend(FakeBackend())
    assert backend.marker == "plain-attr"
    assert backend.sync_op() == f"sync {AWS}"
    chunks = [c async for c in backend.stream()]
    assert chunks == [f"chunk {AWS}"]


def test_wrap_is_idempotent():
    wrapped = wrap_backend(FakeBackend())
    assert wrap_backend(wrapped) is wrapped


def test_get_backend_returns_wrapped(monkeypatch):
    from menhir.api import routes_support

    monkeypatch.setattr(
        routes_support, "_require_runtime_context",
        lambda request: SimpleNamespace(built=MagicMock(), session=MagicMock()),
    )
    monkeypatch.setattr(routes_support, "_resolve_caller_session", lambda request: MagicMock())
    monkeypatch.setattr(routes_support, "RuntimeProvider", lambda *a, **k: FakeBackend())
    assert isinstance(routes_support._get_backend(MagicMock()), ScrubbingBackend)


def test_build_memory_backend_returns_wrapped(monkeypatch):
    from menhir.core.runtime import _state
    from menhir.mcp import service_access

    monkeypatch.setattr(_state, "built", MagicMock())
    monkeypatch.setattr(_state, "session", MagicMock())
    monkeypatch.setattr(service_access, "RuntimeProvider", lambda *a, **k: FakeBackend())
    assert isinstance(service_access.build_memory_backend(), ScrubbingBackend)


def test_non_async_decorated_method_is_still_scrubbed() -> None:
    import asyncio
    import functools

    from menhir.core.backend_scrub import ScrubbingBackend

    async def _read():
        return {"content": "key AKIAIOSFODNN7EXAMPLE"}

    def _deco(fn):
        @functools.wraps(fn)
        def inner(*a, **k):  # sync wrapper hides `async def`
            return fn(*a, **k)

        return inner

    class _Inner:
        read = staticmethod(_deco(_read))

    out = asyncio.run(ScrubbingBackend(_Inner()).read())
    assert "AKIAIOSFODNN7EXAMPLE" not in out["content"]
