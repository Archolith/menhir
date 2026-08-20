"""CF-41: the dashboard must send the LEAST-privileged key available, as its comment claims.

`console` polls read-only `/api/ready` and `/api/stats`. Its own comment says it sends "the
least-privileged key available", but the fallback tried `operator_key` before `agent_key` -- so a
host with both configured put the OPERATOR key on the wire for the most routine operation in the
CLI, and per CF-42 that wire is plain HTTP.

Driven through the real `console` command. `run_console` is imported inside the function body, so
patching `menhir.cli.console.run_console` intercepts the actual call rather than re-checking the
fallback expression in isolation -- a copy of the expression would pass even if the caller stopped
using it.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from menhir.cli import app


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def _fake_run_console(**kwargs: Any) -> None:
        seen.update(kwargs)
        seen["_called"] = True

    # The string form cannot be used here: `menhir.cli.console` as an attribute of the
    # `menhir.cli` package resolves to the `console` COMMAND FUNCTION, which shadows the
    # submodule of the same name. Patch the module object itself; the in-function
    # `from menhir.cli.console import run_console` resolves through sys.modules and so
    # picks this up.
    import menhir.cli.console as console_module

    monkeypatch.setattr(console_module, "run_console", _fake_run_console)
    return seen


def _settings(**keys: str):
    class _S:
        readonly_key = keys.get("readonly_key", "")
        agent_key = keys.get("agent_key", "")
        operator_key = keys.get("operator_key", "")
        api_key = keys.get("api_key", "")
        privacy_redact = False
        api_host = "127.0.0.1"
        api_port = 8090

    return _S()


def _run(monkeypatch: pytest.MonkeyPatch, **keys: str):
    monkeypatch.setattr(
        "menhir.config.MemorySettings.from_env", staticmethod(lambda: _settings(**keys))
    )
    return CliRunner().invoke(app, ["console"])


@pytest.mark.unit
def test_agent_key_is_preferred_over_operator_key(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    result = _run(monkeypatch, agent_key="AGENT-KEY", operator_key="OPERATOR-KEY")

    assert captured.get("_called"), f"run_console was never reached: {result.output}"
    assert captured["api_key"] == "AGENT-KEY"
    assert captured["api_key"] != "OPERATOR-KEY"


@pytest.mark.unit
def test_readonly_key_wins_when_present(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    """POSITIVE CONTROL: without this, the test above would pass if api_key were always None."""
    _run(
        monkeypatch,
        readonly_key="READONLY-KEY",
        agent_key="AGENT-KEY",
        operator_key="OPERATOR-KEY",
    )

    assert captured.get("_called")
    assert captured["api_key"] == "READONLY-KEY"


@pytest.mark.unit
def test_operator_key_is_still_used_when_it_is_the_only_one(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    """The reorder must not stop the dashboard working on an operator-only host."""
    _run(monkeypatch, operator_key="OPERATOR-KEY")

    assert captured.get("_called")
    assert captured["api_key"] == "OPERATOR-KEY"
