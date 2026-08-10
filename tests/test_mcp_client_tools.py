"""Tests for MintClientTool and RevokeClientTool MCP tools."""

from __future__ import annotations

import asyncio
import json
import os

import pytest


@pytest.fixture(autouse=True)
def reset_store_singleton() -> None:
    """Reset the client token store singleton between tests."""
    import menhir.api.client_token_store  # noqa: E402

    menhir.api.client_token_store._store_singleton = None


@pytest.fixture
def enabled_env(tmp_path: pytest.TempPathFactory) -> None:
    """Set env vars so the client token store is enabled and backed by tmp_path."""
    os.environ["MENHIR_CLIENT_TOKENS_ENABLED"] = "1"
    os.environ["MENHIR_OAUTH_AS_DIR"] = str(tmp_path)
    yield
    os.environ.pop("MENHIR_CLIENT_TOKENS_ENABLED", None)
    os.environ.pop("MENHIR_OAUTH_AS_DIR", None)


@pytest.mark.unit
def test_mint_client_returns_token(enabled_env: None) -> None:
    """Minting returns a JSON payload with client_id, client_name, tier, and token."""
    from menhir.mcp.tools.ops.mint_client import MintClientTool

    tool = MintClientTool()
    result = asyncio.run(tool.endpoint(client_name="alpha", tier="agent"))
    payload = json.loads(result)

    assert "client_id" in payload
    assert payload["client_name"] == "alpha"
    assert payload["tier"] == "agent"
    assert payload["token"]  # non-empty


@pytest.mark.unit
def test_mint_then_token_resolves(enabled_env: None) -> None:
    """After minting, the ClientTokenStore can resolve the returned token."""
    from menhir.mcp.tools.ops.mint_client import MintClientTool
    from menhir.api.client_token_store import ClientTokenStore

    tool = MintClientTool()
    result = asyncio.run(tool.endpoint(client_name="beta", tier="readonly"))
    payload = json.loads(result)

    # Build a store pointing at the same db path
    from menhir.infrastructure.paths import oauth_as_db_path

    store = ClientTokenStore(oauth_as_db_path() / "client_tokens.db")
    record = store.resolve(payload["token"])
    assert record is not None
    assert record.client_id == payload["client_id"]
    assert record.client_name == "beta"
    assert record.tier == "readonly"


@pytest.mark.unit
def test_mint_invalid_tier(enabled_env: None) -> None:
    """An invalid tier returns a JSON error."""
    from menhir.mcp.tools.ops.mint_client import MintClientTool

    tool = MintClientTool()
    result = asyncio.run(tool.endpoint(client_name="gamma", tier="root"))
    payload = json.loads(result)
    assert "error" in payload


@pytest.mark.unit
def test_revoke_client(enabled_env: None) -> None:
    """Revoking a client returns {'revoked': true} and the token becomes unresolvable."""
    from menhir.mcp.tools.ops.mint_client import MintClientTool
    from menhir.mcp.tools.ops.revoke_client import RevokeClientTool
    from menhir.api.client_token_store import ClientTokenStore
    from menhir.infrastructure.paths import oauth_as_db_path

    # Mint first
    mint_result = asyncio.run(MintClientTool().endpoint(client_name="delta", tier="agent"))
    mint_payload = json.loads(mint_result)
    client_id = mint_payload["client_id"]
    raw_token = mint_payload["token"]

    # Revoke
    revoke_result = asyncio.run(RevokeClientTool().endpoint(client_id=client_id))
    revoke_payload = json.loads(revoke_result)
    assert revoke_payload["revoked"] is True

    # Token should no longer resolve
    store = ClientTokenStore(oauth_as_db_path() / "client_tokens.db")
    record = store.resolve(raw_token)
    assert record is None


@pytest.mark.unit
def test_disabled_returns_error() -> None:
    """Without the env var, the endpoint returns a JSON error."""
    # Ensure the store singleton is None and env var is not set
    import menhir.api.client_token_store  # noqa: E402

    menhir.api.client_token_store._store_singleton = None
    # Make sure the env var is not set for this test
    os.environ.pop("MENHIR_CLIENT_TOKENS_ENABLED", None)

    from menhir.mcp.tools.ops.mint_client import MintClientTool

    tool = MintClientTool()
    result = asyncio.run(tool.endpoint(client_name="epsilon", tier="readonly"))
    payload = json.loads(result)
    assert "error" in payload


@pytest.mark.unit
def test_list_clients_returns_minted(enabled_env: None) -> None:
    """Mint two clients, then list them — both appear and no token material leaks."""
    from menhir.mcp.tools.ops.mint_client import MintClientTool
    from menhir.mcp.tools.ops.list_clients import ListClientsTool

    asyncio.run(MintClientTool().endpoint(client_name="client-one", tier="readonly"))
    asyncio.run(MintClientTool().endpoint(client_name="client-two", tier="agent"))

    result = asyncio.run(ListClientsTool().endpoint())
    payload = json.loads(result)

    assert "clients" in payload
    names = [c["client_name"] for c in payload["clients"]]
    assert "client-one" in names
    assert "client-two" in names
    for c in payload["clients"]:
        assert "token" not in c


@pytest.mark.unit
def test_list_clients_disabled_returns_error() -> None:
    """Without the env var, the endpoint returns a JSON error."""
    import menhir.api.client_token_store  # noqa: E402

    menhir.api.client_token_store._store_singleton = None
    os.environ.pop("MENHIR_CLIENT_TOKENS_ENABLED", None)

    from menhir.mcp.tools.ops.list_clients import ListClientsTool

    result = asyncio.run(ListClientsTool().endpoint())
    payload = json.loads(result)
    assert "error" in payload
