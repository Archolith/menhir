"""CF-34 -- an unbound request tier is now a refusal, not a pass.

`if tier and not _tier_allows(...)` made an absent tier a PASS. The safe default for an
authorization check is to deny when it cannot determine the subject, and this one admitted. It was
never network-reachable -- HTTP binds a tier in the auth middleware -- but it made every gate in
`BaseTool.execute` conditional on the transport having done its job, which is the shape that turns
a future refactor moving a call off that path into a silent authorization hole rather than a crash.

OWNER RULING 2026-08-22: absent tier becomes denial, and every legitimate in-process path binds one
deliberately.

**The blocker the entry recorded had already been removed.** It stopped short because
`TierFilteredFastMCP.list_tools` documents the empty tier as a SUPPORTED state for local stdio. But
`bind_stdio_local_trust()` -- written for exactly this, docstring naming "the implicit empty-tier
bypass in BaseTool.execute (which was the undocumented status quo)" -- is already wired at
`mcp/server.py:63`. Stdio binds operator explicitly, so the empty tier stopped being that path's
contract before this change landed.

**Measured, not assumed:** nothing outside `menhir/mcp/` imports the tool modules except
`api/mcp_remote.py`, which is the HTTP path and binds via middleware. The CLI and the scheduler --
which the entry names -- reach services and adapters directly and never touch this gate. So the
production blast radius was the stdio path, already covered.

**The cost was in tests**, where 30 of the 32 files importing `menhir.mcp.tools` bound no tier. An
autouse fixture in `conftest` stands in for the transport, which is one change rather than thirty --
and the tests below exist so that fixture cannot hide a MISSING bind in production code.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from menhir.core.request_context import bind_request_tier, reset_request_tier

pytestmark = pytest.mark.unit


class _Tool:
    """A minimal BaseTool subclass, so this tests the contract rather than one real tool."""

    def __new__(cls):
        from menhir.mcp.contracts import BaseTool

        class _Concrete(BaseTool):
            name = "cf34_probe"
            description = "probe"
            required_tier = "readonly"

            async def endpoint(self, **kwargs):  # pragma: no cover - not reached when refused
                return "ok"

            def call_payload(self, *a, **kw):
                return {}

        return _Concrete()


def _run(tier: str | None):
    token = bind_request_tier("" if tier is None else tier)
    try:
        return asyncio.run(_Tool().execute())
    finally:
        reset_request_tier(token)


def test_an_unbound_tier_is_refused() -> None:
    """THE FINDING. Previously this returned the tool's result."""
    assert "No request tier is bound" in str(_run(None))


def test_a_bound_tier_still_works() -> None:
    """POSITIVE CONTROL. Asserts the tool's OUTPUT, not the absence of one error string -- a gate
    that refused every tier raises a DIFFERENT message and would have slipped past that. Caught by
    mutation."""
    result = str(_run("readonly"))
    assert "ok" in result, result
    assert "cannot invoke" not in result, result


def test_the_refusal_names_how_to_bind_one() -> None:
    """The message is the only thing a developer hitting this in a new call path will read, so it
    names both binding seams rather than only stating the rule."""
    message = str(_run(None))
    assert "auth middleware" in message
    assert "bind_stdio_local_trust" in message


def test_the_resource_path_is_gated_too() -> None:
    """TWO SITES, not one. `BaseJsonResource.execute` carried the identical predicate, and fixing
    only the tool half would leave resources reading at any tier.

    EXECUTED, not grepped. An earlier version searched the function's source for the refusal
    message -- which survives when the branch guarding it is disabled, so the mutation walked
    straight through."""
    from menhir.mcp.contracts import BaseJsonResource

    class _Resource(BaseJsonResource):
        uri = "menhir://cf34-probe"
        name = "cf34_probe"
        description = "probe"

        async def build_payload(self, **kwargs):  # pragma: no cover - not reached when refused
            return {"value": 42}

        async def endpoint(self, **kwargs):  # pragma: no cover - not reached when refused
            return {"value": 42}

    token = bind_request_tier("")
    try:
        result = str(asyncio.run(_Resource().execute()))
    finally:
        reset_request_tier(token)

    assert "No request tier is bound" in result


def test_neither_predicate_still_short_circuits_on_a_falsy_tier() -> None:
    """The exact defect shape, asserted at source so it cannot return in either place. `if tier
    and ...` reads as a guard and is a bypass."""
    from menhir.mcp import contracts

    for func in (contracts.BaseTool.execute, contracts.BaseJsonResource.execute):
        assert "if tier and not _tier_allows" not in inspect.getsource(func), (
            f"{func.__qualname__} still treats an absent tier as a pass"
        )


def test_the_stdio_entry_point_binds_a_tier() -> None:
    """WHY THE FLIP IS SAFE, and the assertion that keeps it safe. If stdio ever stops binding,
    every tool call over that transport starts refusing -- so this is the guard that turns that
    into a test failure instead of a support ticket."""
    import ast

    from menhir.mcp import server

    # PARSED, not grepped: commenting the call out leaves the text in the source, which is how the
    # mutation defeated the string version of this test.
    tree = ast.parse(inspect.getsource(server.main))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "bind_stdio_local_trust" in called, (
        "stdio no longer binds a tier; every tool call over that transport will now refuse"
    )


def test_no_production_module_outside_mcp_imports_the_tool_modules() -> None:
    """The reachability claim, held as an invariant rather than a one-off census. The entry named
    the CLI and the scheduler as unbound callers; neither imports these. If a new module starts
    importing tools directly, it has to bind a tier -- and this test is what says so."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "menhir"
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("mcp/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "from menhir.mcp.tools" in text or "menhir.mcp import tools" in text:
            offenders.append(rel)

    assert offenders == ["api/mcp_remote.py"], (
        "a module outside menhir/mcp now imports the tool modules directly; it must bind a "
        f"request tier or every call it makes will refuse. Found: {offenders}"
    )
