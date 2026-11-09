"""#577: the CLI hook scrubs credentials from memory rows and context before rendering."""

from __future__ import annotations

import io
import sys
import types

import pytest

from menhir.cli import hook as hook_module

pytestmark = pytest.mark.unit

AWS = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
QUERY = "a sufficiently long prompt that triggers recall"


class _FakeStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


class _Builder:
    async def build_context(self, *args, **kwargs):
        return types.SimpleNamespace(context=f"- [0.9] ctx token {GITHUB}")


class _Adapter:
    def fetch_flagged_memories(self, limit=10, workspace=None):
        return [{"name": f"pin {AWS}", "content": f"key {AWS}", "summary": f"s {GITHUB}"}]

    def list_todos(self, status="open", limit=5, namespace=None):
        return [{"content": f"todo with {GITHUB}", "priority": "high"}]

    def list_temporal_in_window(self, window_days=30, namespace=None, limit=5):
        return [{"name": "n", "content": f"remind {AWS}", "target_date": "2030-01-01"}]


def _install(monkeypatch: pytest.MonkeyPatch, stdin: str = "{}") -> None:
    svc = types.SimpleNamespace(graph_adapter=_Adapter(), context_builder=_Builder())
    monkeypatch.setattr("menhir.cli.bootstrap.build_hook_services", lambda settings=None: svc)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(stdin))


def _run_prompt() -> None:
    hook_module._run_prompt_impl(query=QUERY, max_tokens=2000, frequency=0, workspace=None)


def _run_postcompact() -> None:
    hook_module._run_postcompact_impl(max_tokens=2000, workspace=None)


_RUNNERS = [(_run_prompt, "{}"), (_run_postcompact, '{"source": "compact"}')]


@pytest.mark.parametrize("runner,stdin", _RUNNERS)
def test_hook_output_is_scrubbed(monkeypatch, capsys, runner, stdin) -> None:
    monkeypatch.delenv("MENHIR_SECRET_POLICY", raising=False)
    _install(monkeypatch, stdin)
    runner()
    out = capsys.readouterr().out
    assert AWS not in out
    assert GITHUB not in out
    assert "[REDACTED:" in out


@pytest.mark.parametrize("runner,stdin", _RUNNERS)
def test_policy_off_returns_rows_as_given(monkeypatch, capsys, runner, stdin) -> None:
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    _install(monkeypatch, stdin)
    runner()
    out = capsys.readouterr().out
    assert AWS in out
    assert GITHUB in out
    assert "[REDACTED:" not in out


@pytest.mark.parametrize("runner,stdin", _RUNNERS)
def test_detector_failure_withholds_memory_text(monkeypatch, capsys, runner, stdin) -> None:
    monkeypatch.delenv("MENHIR_SECRET_POLICY", raising=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("detector down")

    monkeypatch.setattr("menhir.domain.payload_scrub.redact", _boom)
    _install(monkeypatch, stdin)
    runner()  # must not raise
    captured = capsys.readouterr()
    for text in (AWS, GITHUB, "pin ", "todo with", "remind", "ctx token", "Pinned"):
        assert text not in captured.out
    assert "credential scan failed" in captured.err
    assert AWS not in captured.err and GITHUB not in captured.err


@pytest.mark.asyncio
async def test_backend_context_builder_scrubs(monkeypatch) -> None:
    from menhir.cli._backend_context import BackendContextBuilder

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"context": f"hello {AWS}"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.delenv("MENHIR_SECRET_POLICY", raising=False)
    monkeypatch.setattr("menhir.cli._backend_context.httpx.AsyncClient", _Client)
    result = await BackendContextBuilder(backend_url="http://x").build_context("q")
    assert AWS not in result.context
    assert "[REDACTED:" in result.context
