from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "personal_stage_vps.py"
SPEC = importlib.util.spec_from_file_location("personal_stage_vps", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _policy() -> dict:
    operator = {
        "label": "chatgpt-chat",
        "registration": {
            "client_name": "chatgpt-chat",
            "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
            "token_endpoint_auth_method": "none",
        },
        "scopes": ["menhir:read", "menhir:write", "menhir:admin"],
        "maximum_tier": "operator",
        "namespace": "",
        "allowed_tools": ["add_todo", "list_todos", "recall_memories"],
        "denied_tools": ["delete_namespace", "mint_client", "revoke_client"],
    }
    return {
        "version": 2,
        "canonical_digest": "0" * 64,
        "access_contract": {
            "primary_endpoint": "https://memory.ctharvey.me/mcp-http",
            "authentication": {
                "protocol": "oauth-2.1",
                "grant_type": "authorization_code",
                "pkce_method": "S256",
                "access_token": "signed_jwt",
                "client_identity": "policy_bound_client_id",
            },
            "products": {
                "chatgpt": {"role": "operator", "client_ids": ["chatgpt-id"]},
                "codex": {"role": "operator", "client_ids": ["codex-id"]},
                "claude": {"role": "operator", "client_ids": ["claude-id"]},
                "opencode": {"role": "agent", "client_ids": ["opencode-id"]},
            },
        },
        "clients": {
            "chatgpt-id": operator,
            "codex-id": {**operator, "label": "codex"},
            "claude-id": {**operator, "label": "claude"},
            "opencode-id": {
                **operator,
                "label": "opencode",
                "scopes": ["menhir:read", "menhir:write"],
                "maximum_tier": "agent",
            },
        },
    }


def test_staging_policy_preserves_canonical_endpoint_and_adds_isolated_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_chown", lambda *_args: None)
    source = tmp_path / "source.json"
    destination = tmp_path / "staged.json"
    source.write_text(json.dumps(_policy()), encoding="utf-8")

    digest = MODULE._stage_policy(source.resolve(), destination)
    staged = json.loads(destination.read_text(encoding="utf-8"))

    assert staged["access_contract"]["primary_endpoint"] == "https://memory.ctharvey.me/mcp-http"
    assert staged["canonical_digest"] == digest == MODULE._canonical_policy(staged)
    probe = staged["clients"][MODULE.STAGING_CLIENT]
    assert probe["namespace"] == MODULE.STAGING_NAMESPACE
    assert probe["registration"]["redirect_uris"] == ["https://client.staging.invalid/callback"]
    assert probe["denied_tools"] == ["delete_namespace", "mint_client", "revoke_client"]


def test_compose_override_uses_only_disposable_root_and_exact_image_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "_chown", lambda *_args: None)
    root = tmp_path / "stage-root"
    root.mkdir()
    bundle = tmp_path / "bundle"
    compose = bundle / "rootfs/srv/menhir/production/deploy/docker-compose.production.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="ascii")
    release = {
        "images": {
            "menhir": "sha256:" + "1" * 64,
            "neo4j": "sha256:" + "2" * 64,
            "caddy": "sha256:" + "3" * 64,
        }
    }

    base, override = MODULE._compose_files(
        root.resolve(),
        bundle.resolve(),
        release,
        {"MENHIR_IMAGE": release["images"]["menhir"]},
        "172.24.240.0/24",
    )
    text = override.read_text(encoding="utf-8")

    assert base == compose.resolve()
    assert release["images"]["menhir"] in text
    assert release["images"]["caddy"] in text
    assert str(root.resolve()) in text
    assert "/srv/menhir/production/state" not in text
    assert "127.0.0.1::443" in text
    assert "172.24.240.2" in text
    assert 'SSL_CERT_FILE: "/run/staging-ca.crt"' in text
    assert "- memory.ctharvey.me" in text
    assert "staging-ca.crt" in text


def test_tree_digest_rejects_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    root = tmp_path / "bundle"
    root.mkdir()
    target = tmp_path / "outside"
    target.write_text("outside\n", encoding="ascii")
    try:
        (root / "link").symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks requires unavailable Windows privilege")

    with pytest.raises(MODULE.StageError, match="symlink"):
        MODULE._tree_sha256(root)


def test_receipt_check_names_match_control_plane() -> None:
    control_path = Path(__file__).parents[1] / "deploy" / "personal_deploy.py"
    spec = importlib.util.spec_from_file_location("personal_deploy_for_stage_test", control_path)
    assert spec and spec.loader
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)

    assert set(MODULE.CHECKS) == control.STAGING_CHECKS
    assert MODULE.STAGING_CLIENT == "menhir-staging-probe"
    assert MODULE.STAGING_SUBJECT == "menhir-admin"


def test_mcp_exercise_does_not_reset_checks_owned_by_the_outer_runner() -> None:
    source = MODULE._exercise.__code__
    assert "CHECKS" not in source.co_names


def test_exact_allowlist_denial_accepts_only_the_fail_closed_payload() -> None:
    message = (
        "PermissionError: Client is not permitted to invoke `delete_namespace` "
        "(restricted by MENHIR_CLIENT_TOOLS allowlist)"
    )
    denied = {
        "result": {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": {"message": message},
                    "ok": False,
                    "tool": "delete_namespace",
                }),
            }],
            "isError": False,
        }
    }
    assert MODULE._is_exact_allowlist_denial(200, denied, "delete_namespace")

    succeeded = json.loads(json.dumps(denied))
    succeeded["result"]["content"][0]["text"] = json.dumps({"ok": True})
    assert not MODULE._is_exact_allowlist_denial(200, succeeded, "delete_namespace")


def test_fake_provider_has_deterministic_embedding_dimension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    bundle = tmp_path / "bundle"
    policy = bundle / "rootfs/srv/menhir/production/policy/client-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps(_policy()), encoding="utf-8")
    root.mkdir()
    # The generated provider is data, not executed in this unit test. Its exact
    # 1536-vector response is the compatibility boundary used by staging recall.
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert '"embedding":[0.0]*1536' in source
    assert '"id": "staging-chat"' in source
    assert '"id": "text-embedding-3-small"' in source
    assert "http://fake-llm:8080/v1" in source


def test_runner_never_contains_production_data_mount() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert 'STAGING_ROOT = Path("/srv/menhir/staging")' in source
    assert 'source: /srv/menhir/production/state' not in source
    assert 'production authority or container identity changed during staging' in source
    assert '"down", "--volumes", "--remove-orphans"' in source


def test_transferred_private_image_uses_tag_and_checks_release_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    expected_id = "sha256:" + "4" * 64
    commit = "a" * 40
    wheel_manifest = "b" * 64
    oauth_wheel = "c" * 64

    def fake_run(*args: str, **kwargs: object) -> SimpleNamespace:
        calls.append(args)
        reference = args[-1]
        optional = kwargs.get("check") is False
        if optional and "@sha256:" in reference and "menhir:0.2.0-12" in reference:
            return SimpleNamespace(returncode=1, stdout="")
        identity = expected_id if "menhir:0.2.0-12" in reference else "sha256:" + "5" * 64
        labels = {
            "org.opencontainers.image.revision": commit,
            "org.archolith.menhir.wheel-manifest.sha256": wheel_manifest,
            "org.archolith.oauth.wheel.sha256": oauth_wheel,
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Id": identity, "Config": {"Labels": labels}}]),
        )

    monkeypatch.setattr(MODULE, "_run", fake_run)
    menhir = "ghcr.io/archolith/menhir:0.2.0-12@sha256:" + "1" * 64
    neo4j = "ghcr.io/archolith/menhir-neo4j:5.26.30-1@sha256:" + "2" * 64
    caddy = "sha256:" + "3" * 64

    environment = {"MENHIR_IMAGE": menhir, "NEO4J_IMAGE": neo4j}
    runtime = MODULE._ensure_images(
        environment,
        {
            "images": {"caddy": caddy},
            "repos": {"menhir": commit},
            "dockerfile_wheel_manifest_sha256": wheel_manifest,
            "oauth_wheel_sha256": oauth_wheel,
        },
    )

    assert environment["MENHIR_IMAGE"] == menhir.split("@", 1)[0]
    assert runtime["menhir"] == expected_id
    assert ("docker", "pull", menhir) not in calls
    assert calls == [
        ("docker", "image", "inspect", menhir),
        ("docker", "image", "inspect", menhir.split("@", 1)[0]),
        ("docker", "image", "inspect", neo4j),
        ("docker", "image", "inspect", menhir.split("@", 1)[0]),
        ("docker", "image", "inspect", neo4j),
        ("docker", "image", "inspect", caddy),
        ("docker", "image", "inspect", menhir.split("@", 1)[0]),
    ]


def test_desktop_wrapper_transfers_private_registry_image_before_remote_stage() -> None:
    wrapper = (Path(__file__).parents[1] / "deploy" / "personal_stage.ps1").read_text(
        encoding="utf-8"
    )

    assert "docker save --output $localImageTar $menhirImageTag" in wrapper
    assert "sudo -n docker load --input '$remoteImageTar'" in wrapper
    assert "--expected-menhir-image-archive-sha256 '$menhirImageArchiveSha256'" in wrapper
    assert wrapper.index("docker load --input") < wrapper.index("sudo -n python3 '$remoteRunner'")
