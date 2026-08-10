"""Server-side namespace pinning (MENHIR_CLIENT_NAMESPACES).

A pinned client's tool calls are FORCED into its namespace: config wins over the
caller's own argument. This exists because a caller cannot always be trusted to
scope its own writes -- e.g. a public game-chat bot driven by a small local model,
which will omit the namespace argument or pass the wrong one. Unpinned clients must
be completely unaffected.
"""

from __future__ import annotations

import pytest

from menhir.config.settings import parse_client_namespaces
from menhir.mcp import contracts
from menhir.mcp.contracts import BaseTextTool


class _NamespacedTool(BaseTextTool):
    name = "fake_add_memory"
    description = "test double"
    required_tier = "agent"

    async def endpoint(self, text: str = "", namespace: str = "") -> str:  # noqa: D102
        return namespace


class _NoNamespaceTool(BaseTextTool):
    name = "fake_no_namespace"
    description = "test double without a namespace param"
    required_tier = "agent"

    async def endpoint(self, text: str = "") -> str:  # noqa: D102
        return "ok"


@pytest.mark.unit
def test_parse_client_namespaces_pairs_and_junk() -> None:
    assert parse_client_namespaces("tiny-agent-rp=palworld-rp") == {
        "tiny-agent-rp": "palworld-rp"
    }
    # Multiple entries, whitespace, case-folded client names.
    assert parse_client_namespaces(" A=one , B=two ") == {"a": "one", "b": "two"}
    # Malformed entries are skipped, not fatal.
    assert parse_client_namespaces("noequals,=nokey,key=,ok=fine") == {"ok": "fine"}
    assert parse_client_namespaces("") == {}


@pytest.mark.unit
def test_unpinned_client_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "")
    tool = _NamespacedTool()
    assert tool._apply_pinned_namespace({"namespace": "whatever"}) == {
        "namespace": "whatever"
    }
    assert tool._apply_pinned_namespace({}) == {}


@pytest.mark.unit
def test_pinned_client_namespace_is_injected_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "palworld-rp")
    tool = _NamespacedTool()
    # The model omits `namespace` entirely -- the pin supplies it.
    assert tool._apply_pinned_namespace({"text": "hi"}) == {
        "text": "hi",
        "namespace": "palworld-rp",
    }


@pytest.mark.unit
def test_pinned_client_cannot_override_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "palworld-rp")
    tool = _NamespacedTool()
    # The model tries to write to the default graph. The pin wins.
    forced = tool._apply_pinned_namespace({"text": "hi", "namespace": "default"})
    assert forced["namespace"] == "palworld-rp"


@pytest.mark.unit
def test_tool_without_namespace_param_is_not_given_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "palworld-rp")
    tool = _NoNamespaceTool()
    # Injecting `namespace` here would be a TypeError at call time.
    assert tool._apply_pinned_namespace({"text": "hi"}) == {"text": "hi"}
