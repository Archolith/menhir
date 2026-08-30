from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.api.client_policy import load_client_policy
from menhir.api.production_routes import readyz, source_fence_probe
from menhir.api.server import create_app
from menhir.api.server_support import build_server_prereqs
from menhir.config import MemorySettings
from menhir.services.recall_service import RecallService


def _inner_app(settings: MemorySettings):
    wrapped = create_app(settings=settings)
    return wrapped.app.app


def _all_routes(app):
    pending = list(app.routes)
    result = []
    while pending:
        route = pending.pop()
        result.append(route)
        pending.extend(getattr(route, "routes", ()))
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(getattr(original_router, "routes", ()))
    return result


def _route_paths(app) -> set[str]:
    return {
        str(getattr(route, "path", ""))
        for route in _all_routes(app)
        if getattr(route, "path", None) is not None
    }


def test_production_surface_omits_general_api_explorer_and_sse(tmp_path: Path) -> None:
    app = _inner_app(
        _production_settings(
            explorer_enabled=True,
            oauth_as_dir=str(tmp_path / "oauth"),
            oauth_signing_key_path=str(tmp_path / "oauth-signing-key.test.json"),
        )
    )

    paths = _route_paths(app)
    assert "/livez" in paths
    assert "/readyz" in paths
    assert "/mcp-http" in paths
    assert "/mcp" not in paths
    assert "/api/health" not in paths
    assert "/api/internal/backend/{operation}" not in paths
    assert "/explorer" not in paths
    assert app.docs_url is None
    assert app.openapi_url is None


@pytest.mark.asyncio
async def test_readyz_does_not_expose_source_identity_publicly() -> None:
    state = SimpleNamespace(
        settings=SimpleNamespace(
            runtime_mode="production", instance_id="source-prod-a"
        ),
        runtime_ctx=SimpleNamespace(
            capabilities=SimpleNamespace(neo4j_ready=True, enrichment_ready=True),
            scheduler=object(),
        ),
        oauth_signing_key=object(),
    )
    response = await readyz(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert response.status_code == 200
    assert b"instance_id" not in response.body


@pytest.mark.asyncio
async def test_source_fence_probe_requires_token_and_returns_source_signature(
    tmp_path,
) -> None:
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    token = "t" * 48
    challenge = "c" * 32
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "source-fence.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    settings = SimpleNamespace(
        runtime_mode="candidate-readonly",
        instance_id="source-prod-a",
        release_id="menhir-prod-0.2.0-1",
        source_fence_key_id="source-fence-v1",
        source_fence_token=token,
        source_fence_private_key_path=str(key_path),
    )
    state = SimpleNamespace(settings=settings, mutation_fence_active=True)
    request = SimpleNamespace(
        app=SimpleNamespace(state=state),
        headers={
            "authorization": "Bearer " + token,
            "x-menhir-fence-challenge": challenge,
        },
    )
    response = await source_fence_probe(request)
    assert response.status_code == 200
    body = json.loads(response.body)
    signature = base64.urlsafe_b64decode(body.pop("signature") + "==")
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    key.public_key().verify(signature, payload.encode())

    request.headers["authorization"] = "Bearer wrong"
    denied = await source_fence_probe(request)
    assert denied.status_code == 401


def test_candidate_surface_replaces_oauth_mutations_with_maintenance_routes(
    monkeypatch,
) -> None:
    from menhir.api import server_support

    monkeypatch.setattr(
        server_support,
        "configure_signing_key_readonly",
        lambda settings: object(),
    )
    app = _inner_app(_production_settings(runtime_mode="candidate-readonly"))

    candidate_routes = {
        (str(getattr(route, "path", "")), frozenset(getattr(route, "methods", set())))
        for route in _all_routes(app)
        if getattr(route, "name", "") == "candidate_mutation_unavailable"
    }
    assert ("/oauth/authorize", frozenset({"GET", "POST"})) in candidate_routes
    assert ("/oauth/register", frozenset({"POST"})) in candidate_routes
    assert ("/oauth/token", frozenset({"POST"})) in candidate_routes


def test_candidate_prereqs_do_not_open_mutating_oauth_stores(monkeypatch) -> None:
    from menhir.api import server_support

    marker = object()
    monkeypatch.setattr(
        server_support, "configure_signing_key_readonly", lambda settings: marker
    )
    monkeypatch.setattr(
        server_support,
        "configure_signing_key",
        lambda settings: (_ for _ in ()).throw(AssertionError("signing key mutated")),
    )
    monkeypatch.setattr(
        server_support,
        "configure_client_token_store",
        lambda settings: (_ for _ in ()).throw(AssertionError("token store opened")),
    )
    monkeypatch.setattr(
        server_support, "load_client_policy", lambda path, digest, **kwargs: marker
    )
    monkeypatch.setattr(
        server_support,
        "configure_client_store",
        lambda settings: (_ for _ in ()).throw(AssertionError("client store opened")),
    )
    monkeypatch.setattr(
        server_support,
        "configure_auth_code_store",
        lambda settings: (_ for _ in ()).throw(AssertionError("code store opened")),
    )
    monkeypatch.setattr(
        server_support,
        "configure_refresh_store",
        lambda settings: (_ for _ in ()).throw(AssertionError("refresh store opened")),
    )

    prereqs = build_server_prereqs(
        MemorySettings(
            startup_scope="production",
            runtime_mode="candidate-readonly",
            oauth_as_enabled=True,
            oauth_enabled=True,
            oauth_public_base_url="https://memory.example.test",
            oauth_resource="https://memory.example.test/mcp-http",
            oauth_issuer="https://memory.example.test",
            oauth_jwks_uri="https://memory.example.test/.well-known/jwks.json",
            oauth_as_refresh_tokens_enabled=True,
            privacy_redact=True,
            client_policy_path=str(
                Path(__file__).resolve().parent / "client-policy.test.json"
            ),
            oauth_signing_key_path=str(
                Path(__file__).resolve().parent / "oauth-signing-key.test.json"
            ),
            client_policy_digest="a" * 64,
        ),
        tool_catalog=frozenset({"recall_memories"}),
    )

    assert prereqs["signing_key"] is marker
    assert prereqs["oauth_client_store"] is None
    assert prereqs["auth_code_store"] is None
    assert prereqs["oauth_refresh_store"] is None
    assert prereqs["client_policy"] is marker


def test_candidate_mode_requires_production_surface() -> None:
    try:
        MemorySettings(startup_scope="full", runtime_mode="candidate-readonly")
    except ValueError as exc:
        assert "requires startup_scope='production'" in str(exc)
    else:
        raise AssertionError(
            "candidate-readonly accepted outside the production surface"
        )


def _production_settings(**overrides: object) -> MemorySettings:
    policy_path = (
        Path(__file__).resolve().parents[1] / "deploy" / "client-policy.production.json"
    )
    values: dict[str, object] = {
        "startup_scope": "production",
        "runtime_mode": "production",
        "privacy_redact": True,
        "oauth_enabled": True,
        "oauth_as_enabled": True,
        "oauth_as_refresh_tokens_enabled": True,
        "oauth_public_base_url": "https://memory.example.test",
        "oauth_resource": "https://memory.example.test/mcp-http",
        "oauth_audiences": ("https://memory.example.test/mcp-http",),
        "oauth_issuer": "https://memory.example.test",
        "oauth_jwks_uri": "https://memory.example.test/.well-known/jwks.json",
        "client_policy_path": str(policy_path),
        "oauth_signing_key_path": str(
            Path(__file__).resolve().parent / "oauth-signing-key.test.json"
        ),
        "client_policy_digest": "e62a46ec2582208eb6f80116c86cb473371b039fa9dc1892048b0c319e851571",
        "api_key": "test-api-key",
    }
    values.update(overrides)
    return MemorySettings(**values)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"privacy_redact": False}, "privacy redaction"),
        ({"oauth_enabled": False}, "OAuth resource-server"),
        ({"oauth_as_enabled": False}, "authorization-server"),
        ({"oauth_resource": "https://memory.example.test/wrong"}, "resource"),
        ({"oauth_issuer": "https://issuer.example.test"}, "issuer"),
        ({"oauth_jwks_uri": "https://memory.example.test/wrong"}, "JWKS"),
        ({"client_policy_path": ""}, "client policy"),
        ({"client_policy_digest": "not-a-digest"}, "SHA-256"),
        ({"oauth_signing_key_path": ""}, "signing key"),
        ({"oauth_signing_key_path": "relative/key.json"}, "must be absolute"),
        (
            {
                "oauth_as_refresh_retry_grace_s": 30,
                "oauth_refresh_retry_keyring_path": "",
            },
            "keyring",
        ),
    ],
)
def test_production_startup_fails_closed_on_security_drift(
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _production_settings(**override)


@pytest.mark.asyncio
async def test_candidate_recall_forces_access_updates_off(monkeypatch) -> None:
    from menhir.services import recall_service as recall_module

    observed: dict[str, object] = {}

    async def fake_recall(service, query, **kwargs):
        observed.update(kwargs)
        return object()

    async def identity_layer(service, result, query, namespace):
        return result

    monkeypatch.setattr(recall_module, "run_recall", fake_recall)
    monkeypatch.setattr(
        recall_module,
        "apply_event_history_authority_layer",
        identity_layer,
    )
    service = RecallService(
        graphiti_client=object(),
        graph_adapter=object(),
        scoring_service=object(),
        read_only=True,
    )

    await service.recall("candidate probe", update_access=True)

    assert observed["update_access"] is False


def test_production_client_policy_is_digest_bound_and_tracks_clients() -> None:
    path = (
        Path(__file__).resolve().parents[1] / "deploy" / "client-policy.production.json"
    )
    digest = "e62a46ec2582208eb6f80116c86cb473371b039fa9dc1892048b0c319e851571"

    from menhir.mcp.tools import ALL_TOOLS

    authority = load_client_policy(
        str(path),
        digest,
        tool_catalog=frozenset(tool.name for tool in ALL_TOOLS),
    )
    policy = authority.require_client(
        client_id="69c2cd871b488ff4",
        scopes=frozenset({"menhir:read", "menhir:write"}),
        tier="agent",
    )

    assert policy.label == "chatgpt-chat"
    assert policy.maximum_tier == "agent"
    web_allowed_tools = frozenset(
        {
            "add_memory",
            "add_memory_and_track",
            "build_context",
            "get_client_context",
            "get_enrichment_status",
            "get_memory_stats",
            "query_structure",
            "read_flagged_memories",
            "recall_context_memories",
            "recall_memories",
            "watch_enrichment",
        }
    )
    assert policy.allowed_tools == web_allowed_tools
    assert "list_todos" in policy.denied_tools
    assert "ingest_project" not in policy.allowed_tools
    assert "ingest_project" in policy.denied_tools
    assert policy.registration is not None
    assert policy.registration.redirect_uris == (
        "https://chatgpt.com/connector_platform_oauth_redirect",
    )

    claude_web = authority.require_client(
        client_id="6cf6322fa828bb72",
        scopes=frozenset({"menhir:read", "menhir:write", "offline_access"}),
        tier="agent",
    )
    assert claude_web.label == "claude-web"
    assert claude_web.registration is not None
    assert claude_web.registration.redirect_uris == (
        "https://claude.ai/api/mcp/auth_callback",
    )
    assert claude_web.registration.protocol_scopes == frozenset({"offline_access"})
    assert claude_web.allowed_tools == web_allowed_tools
    assert claude_web.denied_tools == policy.denied_tools

    with pytest.raises(PermissionError, match="scopes do not match"):
        authority.require_client(
            client_id="6cf6322fa828bb72",
            scopes=frozenset(
                {"menhir:read", "menhir:write", "offline_access", "openid"}
            ),
            tier="agent",
        )

    bridge_ids = {
        client_id: client_policy
        for client_id, client_policy in authority.clients.items()
        if client_id.startswith(
            "https://memory.ctharvey.me/oauth/client-metadata/agent-smith.json?client="
        )
    }
    assert len(bridge_ids) == 12
    assert len({entry.label for entry in bridge_ids.values()}) == 12
    agent_base_tools = frozenset(
        {
            "add_memory",
            "build_context",
            "list_todos",
            "query_structure",
            "read_flagged_memories",
            "recall_context_memories",
            "recall_memories",
        }
    )
    expected_agent_tools = {
        "agent-smith-antigravity-ide": agent_base_tools,
        "agent-smith-claude": agent_base_tools,
        "agent-smith-cline": agent_base_tools,
        "agent-smith-codex": agent_base_tools | {"add_memory_and_track"},
        "agent-smith-gemini": agent_base_tools,
        "agent-smith-gemini-config": agent_base_tools,
        "agent-smith-goose": agent_base_tools,
        "agent-smith-opencode": agent_base_tools,
        "agent-smith-qwen": agent_base_tools,
        "agent-smith-wsl-claude": agent_base_tools,
        "agent-smith-wsl-opencode": agent_base_tools,
        "agent-smith-zcode": agent_base_tools,
    }
    assert {
        entry.label: entry.allowed_tools for entry in bridge_ids.values()
    } == expected_agent_tools
    assert all("ingest_project" in entry.denied_tools for entry in bridge_ids.values())
    assert expected_agent_tools["agent-smith-codex"] != web_allowed_tools
    assert not any("reasonix" in client_id for client_id in authority.clients)
    assert (
        "https://memory.ctharvey.me/oauth/client-metadata/agent-smith.json"
        not in authority.clients
    )


def test_cloudflared_example_exposes_agent_smith_metadata_before_deny_rules() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "cloudflared-config.production.yml.example"
    )
    text = path.read_text(encoding="utf-8")

    metadata_route = "client-metadata/agent-smith\\.json"
    hostname_deny = "hostname: memory.example.com\n    service: http_status:404"
    assert metadata_route in text
    assert hostname_deny in text
    assert text.index(metadata_route) < text.index(hostname_deny)


def test_candidate_compose_uses_exact_restored_production_authorities() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    release_lib = (root / "deploy" / "release-lib.sh").read_text(encoding="utf-8")
    authority_digest = (root / "deploy" / "lib" / "authority_digest.py").read_text(
        encoding="utf-8"
    )

    assert "source: ${MENHIR_STATE_ROOT:-/srv/menhir/production/state}/oauth" in compose
    assert (
        "source: ${MENHIR_PROD_ROOT:-/srv/menhir/production}/state/oauth" not in compose
    )
    assert 'MENHIR_OAUTH_AS_REFRESH_WITHOUT_OFFLINE_ACCESS_ENABLED: "true"' in compose
    assert 'MENHIR_STATE_ROOT="${MENHIR_ROOT}/state"' in release_lib
    assert 'MENHIR_PROD_SECRETS_DIR="${MENHIR_ROOT}/secrets"' in release_lib
    assert 'MENHIR_PROD_POLICY_DIR="${MENHIR_ROOT}/policy"' in release_lib
    assert (
        'MENHIR_TELEMETRY_ROOT="${candidate_root}/probe-output/telemetry"'
        in release_lib
    )
    assert '"oauth=${MENHIR_ROOT}/state/oauth"' in release_lib
    assert '"telemetry=${MENHIR_ROOT}/state/telemetry"' in release_lib
    assert 'python3 "$(authority_digest_tool)" local-set' in release_lib
    assert '< "$(authority_digest_tool)"' in release_lib
    assert "SHOW INDEXES" in authority_digest
    assert "SHOW CONSTRAINTS" in authority_digest
    assert "SHOW DATABASES" in authority_digest
    assert "SHOW USERS" in authority_digest
    assert "SHOW ROLES WITH USERS" in authority_digest
    assert "SHOW PRIVILEGES" in authority_digest
    assert (
        'candidate_compose "$generation" run --rm --no-deps -T menhir '
        "python3 - neo4j" in release_lib
    )
    assert "toString(" not in release_lib


def test_production_compose_uses_compose_v5_compatible_pid_limits() -> None:
    compose = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "docker-compose.production.yml"
    ).read_text(encoding="utf-8")

    assert "pids_limit:" not in compose
    assert "          pids: 1024" in compose
    assert "          pids: 256" in compose
    assert "      timeout: 15s" in compose


def test_cloudflared_ingress_is_pinned_and_public_surface_is_allowlisted() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "docker-compose.cloudflared.yml").read_text(
        encoding="utf-8"
    )
    config = (
        root / "deploy" / "cloudflared.production.yml.example"
    ).read_text(encoding="utf-8")

    assert "cloudflare/cloudflared@sha256:" in compose
    assert "ipv4_address: 172.30.0.2" in compose
    assert "source: /srv/menhir/production/secrets/cloudflare/credentials.json" in compose
    assert "mcp-http(?:/.*)?" in config
    assert "oauth/(?:authorize|token|register)" in config
    assert "|livez|readyz)" in config
    assert config.count("service: http_status:404") == 2
    assert "/internal/source-fence" not in config
    assert "/api/" not in config


def test_client_policy_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    policy_path = tmp_path / "duplicate-policy.json"
    policy_path.write_text(
        '{"version":1,"version":1,"canonical_digest":"ignored","clients":{}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_client_policy(str(policy_path), "a" * 64)


def _write_policy_with_digest(path: Path, payload: dict[str, object]) -> str:
    canonical = dict(payload)
    canonical.pop("canonical_digest", None)
    digest = hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()
    payload["canonical_digest"] = digest
    path.write_text(json.dumps(payload), encoding="utf-8")
    return digest


def test_client_policy_loads_static_public_web_registration(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deploy" / "client-policy.production.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    client_id = "69c2cd871b488ff4"
    payload["clients"][client_id]["registration"] = {
        "client_name": "chatgpt-chat",
        "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
        "token_endpoint_auth_method": "none",
    }
    policy_path = tmp_path / "static-web-policy.json"
    digest = _write_policy_with_digest(policy_path, payload)

    authority = load_client_policy(str(policy_path), digest)

    registration = authority.clients[client_id].registration
    assert registration is not None
    assert registration.client_name == "chatgpt-chat"
    assert registration.redirect_uris == (
        "https://chatgpt.com/connector_platform_oauth_redirect",
    )


def test_client_policy_rejects_protocol_scope_as_permission(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deploy" / "client-policy.production.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["clients"]["6cf6322fa828bb72"]["scopes"].append("offline_access")
    policy_path = tmp_path / "protocol-scope-policy.json"
    digest = _write_policy_with_digest(policy_path, payload)

    with pytest.raises(ValueError, match="incomplete or duplicate"):
        load_client_policy(str(policy_path), digest)


@pytest.mark.parametrize(
    "registration",
    [
        {
            "client_name": "Claude web",
            "redirect_uris": ["http://claude.example/callback"],
        },
        {
            "client_name": "Claude web",
            "redirect_uris": ["https://claude.example/callback"],
            "token_endpoint_auth_method": "client_secret_post",
        },
        {
            "client_name": "Claude web",
            "redirect_uris": ["https://claude.example/callback"],
            "unreviewed": True,
        },
        {
            "client_name": "Claude web",
            "redirect_uris": ["https://claude.example/callback"],
            "protocol_scopes": ["openid"],
        },
    ],
)
def test_client_policy_rejects_unsafe_web_registration(
    tmp_path: Path, registration: dict[str, object]
) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deploy" / "client-policy.production.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["clients"]["69c2cd871b488ff4"]["registration"] = registration
    policy_path = tmp_path / "unsafe-web-policy.json"
    digest = _write_policy_with_digest(policy_path, payload)

    with pytest.raises(ValueError, match="registration"):
        load_client_policy(str(policy_path), digest)


def test_client_policy_rejects_legacy_cross_client_consent_group(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deploy" / "client-policy.production.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["clients"]["69c2cd871b488ff4"]["consent_group"] = "agent-smith"
    canonical = dict(payload)
    canonical.pop("canonical_digest", None)
    digest = hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()
    payload["canonical_digest"] = digest
    policy_path = tmp_path / "mixed-group-policy.json"
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown client fields"):
        load_client_policy(str(policy_path), digest)
