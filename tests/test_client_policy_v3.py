"""Version 3 client policy: profiles, tier ceiling, deployment-owned products (#435)."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path

import pytest

from menhir.api.client_policy import load_client_policy
from menhir.mcp import tool_profiles
from menhir.mcp.tool_profiles import ADMIN, PROFILES, production_tool_tiers

FIXTURES = Path(__file__).resolve().parent / "fixtures"
V3 = FIXTURES / "client-policy.v3.synthetic.json"
V2 = FIXTURES / "client-policy.synthetic.json"
TIERS = production_tool_tiers()
CATALOG = frozenset(TIERS)
CHATGPT = "https://chatgpt.com/oauth/client.json"
CODEX = "https://menhir.example.com/oauth/client-metadata/agent-smith.json?client=codex"
GEMINI = "https://menhir.example.com/oauth/client-metadata/agent-smith.json?client=gemini"
READONLY = "example-web-client-1"
OPERATOR_SCOPES = ["menhir:read", "menhir:write", "menhir:admin"]


def _payload(source: Path = V3) -> dict:
    return json.loads(source.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> str:
    canonical = dict(payload)
    canonical.pop("canonical_digest", None)
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "ascii"
        )
    ).hexdigest()
    payload["canonical_digest"] = digest
    path.write_text(json.dumps(payload), encoding="utf-8")
    return digest


def _load(tmp_path: Path, payload: dict):
    path = tmp_path / "policy.json"
    digest = _write(path, payload)
    return load_client_policy(str(path), digest, tool_catalog=CATALOG)


def test_committed_v3_fixture_loads_with_census_and_digest() -> None:
    payload = _payload()
    authority = load_client_policy(str(V3), payload["canonical_digest"], tool_catalog=CATALOG)

    assert authority.version == 3
    assert authority.access_contract is not None
    for policy in authority.clients.values():
        assert policy.allowed_tools | policy.denied_tools == CATALOG
        assert not policy.allowed_tools & policy.denied_tools
        assert all(
            tool_profiles.tier_allows(policy.maximum_tier, TIERS[tool])
            for tool in policy.allowed_tools
        )


def test_agent_chatgpt_with_core_and_curate_gets_no_operator_tools() -> None:
    authority = load_client_policy(str(V3), _payload()["canonical_digest"], tool_catalog=CATALOG)
    chatgpt = authority.clients[CHATGPT]

    assert chatgpt.maximum_tier == "agent"
    assert chatgpt.oauth_scopes == {"menhir:read", "menhir:write"}
    assert "flag_memory" in chatgpt.allowed_tools
    assert "review_inbox" in chatgpt.allowed_tools
    assert "ingest_project" in chatgpt.allowed_tools  # curate, agent tier
    for tool in ("delete_memory", "promote_memory", "resolve_conflict", "close_memory"):
        assert tool in chatgpt.denied_tools
    assert not chatgpt.allowed_tools & PROFILES[ADMIN]


def test_operator_product_gets_everything_but_admin() -> None:
    authority = load_client_policy(str(V3), _payload()["canonical_digest"], tool_catalog=CATALOG)

    assert authority.clients[CODEX].allowed_tools == CATALOG - PROFILES[ADMIN]
    assert authority.clients[CODEX].denied_tools == PROFILES[ADMIN]


def test_readonly_client_built_from_allow_only() -> None:
    authority = load_client_policy(str(V3), _payload()["canonical_digest"], tool_catalog=CATALOG)

    assert authority.clients[READONLY].allowed_tools == {
        "get_scalar_view_items",
        "recall_history",
        "recall_memories",
        "recall_timeline",
    }


def test_tier_ceiling_drop_is_logged(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="menhir.api.client_policy"):
        load_client_policy(str(V3), _payload()["canonical_digest"], tool_catalog=CATALOG)

    assert any(
        "chatgpt" in record.getMessage() and "delete_memory" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.__setitem__("profiles", ["core", "curate", "operate", "admin"]),
        lambda c: c.__setitem__("allow", ["list_clients"]),
    ],
    ids=["admin-profile", "admin-allow"],
)
def test_product_client_never_resolves_admin_tools(tmp_path: Path, mutate) -> None:
    payload = _payload()
    mutate(payload["clients"][CODEX])

    with pytest.raises(ValueError, match="admin tools"):
        _load(tmp_path, payload)


def test_non_product_operator_client_may_hold_admin(tmp_path: Path) -> None:
    payload = _payload()
    gemini = payload["clients"][GEMINI]
    gemini.update(
        maximum_tier="operator",
        scopes=OPERATOR_SCOPES,
        profiles=["core", "curate", "operate", "admin"],
    )

    authority = _load(tmp_path, payload)

    assert authority.clients[GEMINI].allowed_tools == CATALOG
    assert authority.clients[GEMINI].denied_tools == frozenset()


def test_product_role_tier_mismatch_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["clients"][CODEX].update(maximum_tier="agent", scopes=["menhir:read", "menhir:write"])

    with pytest.raises(ValueError, match="codex client tier does not match operator"):
        _load(tmp_path, payload)


def test_free_form_product_with_readonly_role_loads(tmp_path: Path) -> None:
    payload = _payload()
    payload["access_contract"]["products"]["dashboards"] = {
        "role": "readonly",
        "client_ids": [READONLY],
    }

    authority = _load(tmp_path, payload)

    assert authority.access_contract.products["dashboards"].role == "readonly"


@pytest.mark.parametrize("role", ["admin", "superuser", ""])
def test_unknown_product_role_is_refused(tmp_path: Path, role: str) -> None:
    payload = _payload()
    payload["access_contract"]["products"]["chatgpt"]["role"] = role

    with pytest.raises(ValueError, match="role must be one of"):
        _load(tmp_path, payload)


def test_product_list_may_drop_a_product(tmp_path: Path) -> None:
    payload = _payload()
    del payload["access_contract"]["products"]["opencode"]

    assert "opencode" not in _load(tmp_path, payload).access_contract.products


def test_empty_products_are_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["access_contract"]["products"] = {}

    with pytest.raises(ValueError, match="non-empty object"):
        _load(tmp_path, payload)


def test_client_in_two_products_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["access_contract"]["products"]["codex"]["client_ids"].append(CHATGPT)

    with pytest.raises(ValueError, match="multiple products"):
        _load(tmp_path, payload)


@pytest.mark.parametrize(
    "scopes",
    [["menhir:read"], OPERATOR_SCOPES, ["menhir:read", "menhir:write", "offline_access"]],
)
def test_v3_scopes_must_equal_the_tier_scopes(tmp_path: Path, scopes) -> None:
    payload = _payload()
    payload["clients"][GEMINI]["scopes"] = scopes

    with pytest.raises(ValueError, match="tier scopes"):
        _load(tmp_path, payload)


def test_explicit_allow_above_tier_is_a_load_error(tmp_path: Path) -> None:
    payload = _payload()
    payload["clients"][GEMINI]["allow"] = ["delete_memory"]

    with pytest.raises(ValueError, match="above maximum_tier"):
        _load(tmp_path, payload)


def test_missing_profiles_key_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    del payload["clients"][GEMINI]["profiles"]

    with pytest.raises(ValueError, match="missing profiles"):
        _load(tmp_path, payload)


def test_v3_rejects_explicit_tool_lists(tmp_path: Path) -> None:
    payload = _payload()
    payload["clients"][GEMINI]["allowed_tools"] = ["recall_memories"]

    with pytest.raises(ValueError, match="unknown client fields"):
        _load(tmp_path, payload)


def test_v2_rejects_profile_keys(tmp_path: Path) -> None:
    payload = _payload(V2)
    payload["clients"][GEMINI]["profiles"] = ["core"]

    with pytest.raises(ValueError, match="unknown client fields"):
        _load(tmp_path, payload)


def test_v3_requires_profile_catalog(tmp_path: Path) -> None:
    payload = _payload()
    del payload["profile_catalog"]

    with pytest.raises(ValueError, match="top-level"):
        _load(tmp_path, payload)


def test_v2_rejects_profile_catalog(tmp_path: Path) -> None:
    payload = _payload(V2)
    payload["profile_catalog"] = 1

    with pytest.raises(ValueError, match="top-level"):
        _load(tmp_path, payload)


@pytest.mark.parametrize("version", [True, 4, "3"])
def test_unsupported_versions_are_refused(tmp_path: Path, version) -> None:
    payload = _payload()
    payload["version"] = version

    with pytest.raises(ValueError, match="supported version"):
        _load(tmp_path, payload)


def test_policy_catalog_newer_than_build_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["profile_catalog"] = tool_profiles.CATALOG_VERSION + 1

    with pytest.raises(ValueError, match="newer than this build"):
        _load(tmp_path, payload)


def test_withheld_tools_warn_and_startup_continues(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(tool_profiles, "CATALOG_VERSION", tool_profiles.CATALOG_VERSION + 1)
    monkeypatch.setitem(tool_profiles.TOOL_PROFILE, "flag_memory", ("curate", tool_profiles.CATALOG_VERSION))
    payload = _payload()

    with caplog.at_level(logging.WARNING, logger="menhir.api.client_policy"):
        authority = _load(tmp_path, payload)

    assert "flag_memory" not in authority.clients[CHATGPT].allowed_tools
    assert "flag_memory" in authority.clients[CHATGPT].denied_tools
    assert any(
        record.levelno == logging.WARNING and "flag_memory" in record.getMessage()
        for record in caplog.records
    )


def test_tool_tiers_must_match_the_runtime_catalog(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    digest = _write(path, _payload())

    with pytest.raises(ValueError, match="tool tiers disagree"):
        load_client_policy(
            str(path),
            digest,
            tool_catalog=CATALOG,
            tool_tiers={name: tier for name, tier in TIERS.items() if name != "add_memory"},
        )


def test_v2_fixture_still_loads() -> None:
    payload = _payload(V2)
    authority = load_client_policy(str(V2), payload["canonical_digest"], tool_catalog=CATALOG)

    assert authority.version == 2
    assert authority.clients[CHATGPT].maximum_tier == "operator"


def test_v2_product_list_is_free_form(tmp_path: Path) -> None:
    payload = copy.deepcopy(_payload(V2))
    del payload["access_contract"]["products"]["opencode"]

    assert "opencode" not in _load(tmp_path, payload).access_contract.products


def test_v2_still_refuses_readonly_product_role(tmp_path: Path) -> None:
    payload = _payload(V2)
    payload["access_contract"]["products"]["web"] = {"role": "readonly", "client_ids": [READONLY]}

    with pytest.raises(ValueError, match="role must be one of"):
        _load(tmp_path, payload)


def test_agent_tier_token_for_agent_chatgpt_is_accepted_and_operator_refused() -> None:
    authority = load_client_policy(str(V3), _payload()["canonical_digest"], tool_catalog=CATALOG)
    scopes = authority.clients[CHATGPT].oauth_scopes

    assert authority.require_client(client_id=CHATGPT, scopes=scopes, tier="agent")
    with pytest.raises(PermissionError, match="tier exceeds"):
        authority.require_client(client_id=CHATGPT, scopes=scopes, tier="operator")


def test_production_startup_accepts_v3_policy(monkeypatch) -> None:
    from menhir.api import server_support
    from menhir.api.server_support import build_server_prereqs
    from menhir.config import MemorySettings

    monkeypatch.setattr(server_support, "configure_signing_key_readonly", lambda settings: object())
    settings = MemorySettings(
        startup_scope="production",
        runtime_mode="candidate-readonly",
        privacy_redact=True,
        oauth_enabled=True,
        oauth_as_enabled=True,
        oauth_as_refresh_tokens_enabled=True,
        oauth_public_base_url="https://menhir.example.com",
        oauth_resource="https://menhir.example.com/mcp-http",
        oauth_audiences=("https://menhir.example.com/mcp-http",),
        oauth_issuer="https://menhir.example.com",
        oauth_jwks_uri="https://menhir.example.com/.well-known/jwks.json",
        client_policy_path=str(V3),
        oauth_signing_key_path=str(Path(__file__).resolve().parent / "oauth-signing-key.test.json"),
        client_policy_digest=_payload()["canonical_digest"],
        api_key="test-api-key",
    )

    prereqs = build_server_prereqs(settings, tool_catalog=CATALOG)

    assert prereqs["client_policy"].version == 3


def _metadata_settings():
    from types import SimpleNamespace

    return SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://menhir.example.com",
        oauth_as_refresh_tokens_enabled=True,
        oauth_scopes_supported=("menhir:read", "menhir:write", "menhir:admin"),
        oauth_admin_scopes=("menhir:admin",),
    )


def _metadata_scope(authority, client_key: str) -> str:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from menhir.api.oauth_as_metadata import router

    app = FastAPI()
    app.state.settings = _metadata_settings()
    app.state.client_policy = authority
    app.include_router(router)
    response = TestClient(app).get(f"/oauth/client-metadata/agent-smith.json?client={client_key}")
    assert response.status_code == 200
    return response.json()["scope"]


def test_agent_smith_cimd_scope_follows_the_policy_tier(tmp_path: Path) -> None:
    from menhir.api.oauth_as_metadata import agent_smith_client_document_for_id

    payload = _payload()
    payload["access_contract"]["products"]["codex"]["role"] = "agent"
    payload["clients"][CODEX].update(
        maximum_tier="agent", scopes=["menhir:read", "menhir:write"], profiles=["core"]
    )
    authority = _load(tmp_path, payload)

    assert _metadata_scope(authority, "codex") == "menhir:read menhir:write offline_access"
    document = agent_smith_client_document_for_id(CODEX, _metadata_settings(), authority)
    assert document is not None
    assert document["scope"] == "menhir:read menhir:write offline_access"
    # Without the policy the built-in table still says operator.
    legacy = agent_smith_client_document_for_id(CODEX, _metadata_settings())
    assert legacy["scope"] == "menhir:read menhir:write menhir:admin offline_access"


def test_agent_smith_cimd_scope_falls_back_when_client_not_in_policy(tmp_path: Path) -> None:
    claude = "https://menhir.example.com/oauth/client-metadata/agent-smith.json?client=claude"
    payload = _payload()
    del payload["clients"][claude]
    products = payload["access_contract"]["products"]
    products["claude"]["client_ids"].remove(claude)
    authority = _load(tmp_path, payload)

    assert _metadata_scope(authority, "claude") == (
        "menhir:read menhir:write menhir:admin offline_access"
    )
