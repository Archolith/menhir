"""CF-32 / CF-33: caller identity is not self-certified, and tool tenancy is declared.

Owner decisions recorded 2026-08-19:
  CF-33 -- a PINNED client MAY invoke a GLOBAL tool. The pin is a DATA boundary; tier is the
           action boundary.
  CF-32 -- an unknown or absent client name is REFUSED, not treated as unrestricted.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.config import MemorySettings
from menhir.mcp.contracts import ToolScope, assert_tool_scopes_declared
from menhir.mcp.service_access import require_trusted_client_identity
from menhir.mcp.tools import ALL_TOOLS

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# CF-33 -- scope is declared, not inferred from a signature accident
# ---------------------------------------------------------------------------


def test_cf33_every_tool_declares_a_scope() -> None:
    """The pin reached a tool only when its endpoint happened to name a `namespace` parameter.
    That is a policy applied to signatures, not to callers: adding a tool silently removed it
    from the pin's reach with no error and no log line, which is how this cluster came to need
    four separate per-site patches."""
    assert_tool_scopes_declared(ALL_TOOLS)


def test_cf33_an_undeclared_tool_fails_startup() -> None:
    """The load-bearing half. The enum documents; this is what makes an omission unshippable."""

    class _Undeclared:
        name = "undeclared_tool"

        async def endpoint(self) -> str:
            return ""

    with pytest.raises(RuntimeError, match="no `scope` declared"):
        assert_tool_scopes_declared([_Undeclared])


def test_cf33_an_invalid_scope_fails_startup() -> None:
    class _Bogus:
        name = "bogus_tool"
        scope = "sort-of-global"

        async def endpoint(self) -> str:
            return ""

    with pytest.raises(RuntimeError, match="unrecognized `scope`"):
        assert_tool_scopes_declared([_Bogus])


def test_cf33_a_declaration_contradicting_the_signature_fails_startup() -> None:
    """A wrong declaration is worse than none: a tool marked NAMESPACED with no `namespace`
    parameter reads as pinned in the audit list while the pin cannot actually reach it."""

    class _LiesAboutNamespace:
        name = "lies_namespaced"
        scope = ToolScope.NAMESPACED

        async def endpoint(self) -> str:
            return ""

    class _LiesAboutGlobal:
        name = "lies_global"
        scope = ToolScope.GLOBAL

        async def endpoint(self, namespace: str = "") -> str:
            return ""

    with pytest.raises(RuntimeError, match="contradicts their signature"):
        assert_tool_scopes_declared([_LiesAboutNamespace])
    with pytest.raises(RuntimeError, match="contradicts their signature"):
        assert_tool_scopes_declared([_LiesAboutGlobal])


def test_cf33_the_global_list_is_small_and_reviewable() -> None:
    """The value of the declaration is turning "41 tools global by accident" into a short list a
    human can audit. If this count grows, someone declared GLOBAL to silence the startup check
    rather than to describe the tool -- which is the one way this mechanism fails."""
    global_tools = sorted(t.name for t in ALL_TOOLS if getattr(t, "scope", None) == ToolScope.GLOBAL)
    assert global_tools == [
        "force_scheduler_takeover",
        "get_client_context",
        "list_clients",
        "mint_client",
        "pause_scheduler",
        "recover_orphans",
        "repair_stale_enrichment",
        "resume_scheduler",
        "revoke_client",
    ]


def test_cf33_no_tool_is_global_merely_because_it_takes_no_arguments() -> None:
    """Every GLOBAL entry is operational state -- scheduler control or client administration.
    None of them reads or writes tenant memory, which is the property that makes the owner's
    'pin is a data boundary' decision safe."""
    global_names = {t.name for t in ALL_TOOLS if getattr(t, "scope", None) == ToolScope.GLOBAL}
    assert not (global_names & {"add_memory", "recall_memories", "delete_memory", "list_todos"})


def test_cf33_namespaced_tools_can_actually_receive_the_pin() -> None:
    for tool in ALL_TOOLS:
        if getattr(tool, "scope", None) == ToolScope.NAMESPACED:
            params = inspect.signature(tool.endpoint).parameters
            assert "namespace" in params, tool.name


# ---------------------------------------------------------------------------
# CF-220 -- add_memory_and_track escaped the pin its sibling honours
# ---------------------------------------------------------------------------


def test_cf220_the_tracking_write_path_is_pinnable_like_its_sibling() -> None:
    """`add_memory` declares `namespace` and is pinnable. `add_memory_and_track` performs the
    SAME write through the same `queue_episode` call and did not, so a pinned client escaped its
    pin simply by calling the sibling. `queue_episode` accepted `namespace` all along."""
    from menhir.mcp.tools.ingest.add_memory import AddMemoryTool
    from menhir.mcp.tools.ingest.add_memory_and_track import AddMemoryAndTrackTool

    for tool in (AddMemoryTool, AddMemoryAndTrackTool):
        assert "namespace" in inspect.signature(tool.endpoint).parameters, tool.name
        assert tool.scope == ToolScope.NAMESPACED, tool.name


# ---------------------------------------------------------------------------
# CF-32 -- a self-declared name is a claim, not an identity
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, client_name: str) -> None:
        self.client_name = client_name


def _settings(**kw) -> MemorySettings:
    base = dict(client_namespaces={}, client_tools={}, known_clients=frozenset())
    base.update(kw)
    return MemorySettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def bound(monkeypatch):
    """Bind a request session and auth mode without a live server."""

    def _bind(client_name: str, auth_mode: str = "header"):
        monkeypatch.setattr(
            "menhir.mcp.service_access.get_request_session",
            lambda: _Session(client_name),
        )
        monkeypatch.setattr(
            "menhir.mcp.service_access.get_request_auth_mode", lambda: auth_mode
        )

    return _bind


def test_cf32_an_unrestricted_deployment_refuses_nothing(bound) -> None:
    """No configured restriction means there is no policy to evade, so behaviour is unchanged."""
    bound("anything-at-all")
    require_trusted_client_identity(_settings())


def test_cf32_an_unknown_name_is_refused_when_restrictions_exist(bound) -> None:
    """The evasion is precisely to claim a name that is NOT restricted, so refusing only
    restricted names would leave it completely intact. The refusal has to be all-or-nothing on
    the deployment."""
    bound("some-other-client")
    with pytest.raises(PermissionError, match="Unknown client name"):
        require_trusted_client_identity(_settings(client_namespaces={"pinned": "ns"}))


def test_cf32_an_absent_name_is_refused_when_restrictions_exist(bound) -> None:
    bound("")
    with pytest.raises(PermissionError, match="must identify"):
        require_trusted_client_identity(_settings(client_namespaces={"pinned": "ns"}))


def test_cf32_a_restricted_client_is_accepted(bound) -> None:
    bound("pinned")
    require_trusted_client_identity(_settings(client_namespaces={"pinned": "ns"}))


def test_cf32_a_known_but_unrestricted_client_is_accepted(bound) -> None:
    """`known` and `restricted` are different facts. Without a third registry, recognizing an
    ordinary client like `claude-code` would have forced a namespace pin on it as a side effect
    of making it nameable -- and on this deployment that would have silently re-scoped the
    busiest client in the graph."""
    bound("claude-code")
    require_trusted_client_identity(
        _settings(client_namespaces={"pinned": "ns"}, known_clients=frozenset({"claude-code"}))
    )


def test_cf32_name_matching_is_case_and_whitespace_insensitive(bound) -> None:
    bound("  Claude-Code  ")
    require_trusted_client_identity(
        _settings(client_tools={"pinned": frozenset({"x"})}, known_clients=frozenset({"claude-code"}))
    )


@pytest.mark.parametrize("mode", ["oauth", "client_token"])
def test_cf32_credential_derived_identity_is_not_second_guessed(bound, mode: str) -> None:
    """OAuth and per-client-token modes derive the name from a credential the server itself
    issued, so a caller cannot rename itself into a different policy and there is nothing to
    refuse. Refusing here would break those deployments for no security gain."""
    bound("some-other-client", auth_mode=mode)
    require_trusted_client_identity(_settings(client_namespaces={"pinned": "ns"}))


@pytest.mark.parametrize("mode", ["header", "query", "admin"])
def test_cf32_every_self_declared_mode_is_checked(bound, mode: str) -> None:
    bound("some-other-client", auth_mode=mode)
    with pytest.raises(PermissionError):
        require_trusted_client_identity(_settings(client_namespaces={"pinned": "ns"}))


def test_cf32_identity_is_checked_before_the_policies_that_key_on_it() -> None:
    """Order matters: both the pin and the tool allowlist key on `client_name`, so an identity
    check that ran after them would be decorative."""
    import menhir.mcp.contracts as contracts

    source = inspect.getsource(contracts.BaseTool.execute)
    assert source.index("require_trusted_client_identity") < source.index("get_client_tool_allowlist")


class _FakeTokenStore:
    """Minimal stand-in for the per-client token store."""

    def mint(self, client_name: str, tier: str):
        record = type(
            "_Rec", (), {"client_id": "cid", "client_name": client_name, "tier": tier}
        )()
        return "raw-token", record


# ---------------------------------------------------------------------------
# CF-83 -- a boundary you can mint your way out of is not a boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cf83_minting_an_undeclared_client_is_refused(monkeypatch) -> None:
    """A namespace pin is server config keyed on client_name, so minting a name that is not
    configured yields a credential no pin covers -- and a pinned operator mints its way out of
    its own data boundary.

    Note this is NOT settled by the decision that a pinned client may invoke a GLOBAL tool. That
    decision says the pin bounds data and tier bounds actions; minting is a global action whose
    EFFECT is a new principal with a different data boundary.
    """
    import json as _json

    from menhir.mcp.tools.ops import mint_client as mod

    monkeypatch.setattr(
        "menhir.api.client_token_store.get_client_token_store", lambda: _FakeTokenStore()
    )
    monkeypatch.setattr(
        "menhir.mcp.service_access.client_restrictions_configured", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "menhir.mcp.service_access.declared_client_names",
        lambda *a, **k: frozenset({"tiny-agent"}),
    )

    result = _json.loads(await mod.MintClientTool().endpoint(client_name="helper"))
    assert "refusing to mint undeclared client" in result["error"]
    assert "token" not in result


@pytest.mark.asyncio
async def test_cf83_minting_a_declared_client_still_works(monkeypatch) -> None:
    import json as _json

    from menhir.mcp.tools.ops import mint_client as mod

    monkeypatch.setattr(
        "menhir.api.client_token_store.get_client_token_store", lambda: _FakeTokenStore()
    )
    monkeypatch.setattr(
        "menhir.mcp.service_access.client_restrictions_configured", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "menhir.mcp.service_access.declared_client_names",
        lambda *a, **k: frozenset({"tiny-agent"}),
    )

    result = _json.loads(await mod.MintClientTool().endpoint(client_name="tiny-agent"))
    assert result["token"] == "raw-token"


@pytest.mark.asyncio
async def test_cf83_an_unrestricted_deployment_mints_freely(monkeypatch) -> None:
    """No configured restriction means there is no pin to escape, so minting is unchanged."""
    import json as _json

    from menhir.mcp.tools.ops import mint_client as mod

    monkeypatch.setattr(
        "menhir.api.client_token_store.get_client_token_store", lambda: _FakeTokenStore()
    )
    monkeypatch.setattr(
        "menhir.mcp.service_access.client_restrictions_configured", lambda *a, **k: False
    )

    result = _json.loads(await mod.MintClientTool().endpoint(client_name="brand-new"))
    assert result["token"] == "raw-token"


def test_cf83_declared_names_have_one_authority() -> None:
    """The identity refusal and the mint refusal must consult the same set. Two copies of "what
    counts as a declared name" would agree until someone edited one of them -- the CF-47 shape."""
    import inspect

    from menhir.mcp import service_access
    from menhir.mcp.tools.ops import mint_client as mod

    assert "declared_client_names" in inspect.getsource(service_access.require_trusted_client_identity)
    assert "declared_client_names" in inspect.getsource(mod.MintClientTool.endpoint)
