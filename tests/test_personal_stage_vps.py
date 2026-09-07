from __future__ import annotations

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


@pytest.fixture
def production_preflight_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    production = tmp_path / "production"
    release_path = production / "release/release.json"
    release_path.parent.mkdir(parents=True)
    live_release = {
        "release_id": "menhir-prod-0.2.0-12",
        "images": {
            "menhir": "sha256:" + "1" * 64,
            "neo4j": "sha256:" + "2" * 64,
        },
        "network": {
            "external_network": "menhir-proxy",
            "alias": "menhir-prod-app",
            "peers": ["172.30.0.2"],
        },
    }
    release_path.write_text(json.dumps(live_release), encoding="utf-8")
    status = tmp_path / "status"
    status.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    app = {
        "State": {
            "Running": True,
            "Status": "running",
            "Health": {"Status": "healthy"},
        },
        "Config": {
            "Image": "ghcr.io/archolith/menhir:0.2.0-12@" + live_release["images"]["menhir"],
            "Env": [
                "MENHIR_RELEASE_ID=menhir-prod-0.2.0-12",
                "MENHIR_RUNTIME_MODE=production",
                "PRIVATE_PROVIDER_TOKEN=swordfish",
            ],
            "Labels": {"com.docker.compose.service": "menhir"},
        },
        "HostConfig": {"Memory": 2 * 1024**3},
        "NetworkSettings": {"Networks": {
            "menhir-prod_internal": {
                "IPAddress": "172.31.0.3",
                "Aliases": ["menhir"],
            },
            "menhir-proxy": {
                "IPAddress": "172.30.0.3",
                "Aliases": ["menhir-prod-app"],
            },
        }},
    }
    neo4j = {
        "State": {
            "Running": True,
            "Status": "running",
            "Health": {"Status": "healthy"},
        },
        "Config": {
            "Image": "neo4j:5-community@" + live_release["images"]["neo4j"],
            "Env": [],
            "Labels": {"com.docker.compose.service": "neo4j"},
        },
        "HostConfig": {"Memory": 4 * 1024**3},
        "NetworkSettings": {"Networks": {
            "menhir-prod_internal": {
                "IPAddress": "172.31.0.2",
                "Aliases": ["neo4j"],
            },
        }},
    }
    ingress = {
        "State": {"Running": True, "Status": "running"},
        "Config": {"Labels": {
            "com.docker.compose.project": "yawndeploy",
            "com.docker.compose.service": "caddy",
        }},
    }
    inspect_values = {
        "menhir-prod-app": app,
        "menhir-prod-neo4j": neo4j,
        "yawndeploy-caddy-1": ingress,
    }
    network = [{
        "Name": "menhir-proxy",
        "Containers": {
            "app-id": {
                "Name": "menhir-prod-app",
                "IPv4Address": "172.30.0.3/24",
            },
            "ingress-id": {
                "Name": "yawndeploy-caddy-1",
                "IPv4Address": "172.30.0.2/24",
            },
        },
    }]

    def fake_run(*args: str, **_kwargs: object) -> SimpleNamespace:
        assert args == ("docker", "network", "inspect", "menhir-proxy")
        return SimpleNamespace(returncode=0, stdout=json.dumps(network))

    monkeypatch.setattr(MODULE, "PRODUCTION_ROOT", production)
    monkeypatch.setattr(MODULE, "PRODUCTION_RELEASE", release_path)
    monkeypatch.setattr(MODULE, "PRODUCTION_STATUS_ROOT", status)
    monkeypatch.setattr(MODULE, "RELEASE_JOURNAL", status / "release-run.json")
    monkeypatch.setattr(MODULE, "_run", fake_run)
    monkeypatch.setattr(MODULE, "_inspect", lambda name: inspect_values[name])
    monkeypatch.setattr(
        MODULE.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=MODULE.MIN_STAGING_DISK_BYTES + 1),
    )
    monkeypatch.setattr(
        MODULE,
        "_available_memory_bytes",
        lambda: MODULE.MIN_STAGING_MEMORY_BYTES + 1,
    )
    candidate_release = {
        "release_id": "menhir-prod-0.2.0-13",
        "network": dict(live_release["network"]),
    }
    return {
        "bundle": bundle,
        "candidate_release": candidate_release,
        "inspect_values": inspect_values,
        "network": network,
        "tmp_path": tmp_path,
    }


def test_app_only_preflight_does_not_require_route_assets_and_is_sanitized(
    production_preflight_host: dict[str, object],
) -> None:
    report = MODULE._production_preflight(
        production_preflight_host["bundle"],
        production_preflight_host["candidate_release"],
        "app-only",
    )

    assert report["schema"] == 1
    assert report["kind"] == "menhir-production-readiness-preflight"
    assert report["checks"]["maintenance_route"] == {"applicable": False}
    assert report["checks"]["network_roles"]["ingress_peer_count"] == 1
    assert "caddy" not in json.dumps(report).lower()
    assert "swordfish" not in json.dumps(report)
    digest = report["canonical_sha256"]
    content = dict(report)
    content.pop("canonical_sha256")
    assert digest == MODULE._canonical_json_sha256(content)


def test_preflight_rejects_unfinished_conflicting_release_journal(
    production_preflight_host: dict[str, object],
) -> None:
    MODULE.RELEASE_JOURNAL.write_text(json.dumps({
        "release_id": "menhir-prod-0.2.0-11",
        "stage": "candidate",
    }), encoding="utf-8")

    with pytest.raises(MODULE.StageError, match="unfinished conflicting"):
        MODULE._production_preflight(
            production_preflight_host["bundle"],
            production_preflight_host["candidate_release"],
            "app-only",
        )


def test_security_config_does_not_bypass_maintenance_route_preflight(
    production_preflight_host: dict[str, object],
) -> None:
    with pytest.raises(MODULE.StageError, match="bundled route transaction runner"):
        MODULE._production_preflight(
            production_preflight_host["bundle"],
            production_preflight_host["candidate_release"],
            "security-config",
        )


def test_preflight_rejects_unhealthy_live_service(
    production_preflight_host: dict[str, object],
) -> None:
    app = production_preflight_host["inspect_values"]["menhir-prod-app"]
    app["State"]["Health"]["Status"] = "unhealthy"

    with pytest.raises(MODULE.StageError, match="not running and healthy"):
        MODULE._production_preflight(
            production_preflight_host["bundle"],
            production_preflight_host["candidate_release"],
            "app-only",
        )


def test_preflight_rejects_insufficient_memory_headroom(
    production_preflight_host: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MODULE,
        "_available_memory_bytes",
        lambda: MODULE.MIN_STAGING_MEMORY_BYTES - 1,
    )

    with pytest.raises(MODULE.StageError, match="insufficient memory headroom"):
        MODULE._production_preflight(
            production_preflight_host["bundle"],
            production_preflight_host["candidate_release"],
            "app-only",
        )


@pytest.fixture
def maintenance_preflight_host(
    production_preflight_host: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    bundle = production_preflight_host["bundle"]
    tmp_path = production_preflight_host["tmp_path"]
    route_source = bundle / "rootfs/srv/yawn/projects/yawn.deploy"
    route_source.mkdir(parents=True)
    route_runner = bundle / "rootfs/srv/menhir/production/bin/caddy-release.sh"
    route_runner.parent.mkdir(parents=True)
    route_runner.write_text("#!/bin/sh\n", encoding="ascii")
    route_runner.chmod(0o755)

    tls_paths = {}
    for key in MODULE.TLS_ROUTE_KEYS:
        path = tmp_path / "tls" / f"{key}.pem"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"fixture {key}\n", encoding="ascii")
        tls_paths[key] = str(path.resolve())
    registry = {
        "proxy": {
            "menhir_network": "menhir-proxy",
            **tls_paths,
        },
    }
    route_source.joinpath("Caddyfile").write_text("memory.example {}\n", encoding="ascii")
    route_source.joinpath("docker-compose.yml").write_text(
        "services:\n  caddy:\n    networks:\n      menhir-proxy:\n"
        "        ipv4_address: 172.30.0.2\n",
        encoding="ascii",
    )
    route_source.joinpath("releases.json").write_text(
        json.dumps(registry), encoding="utf-8",
    )

    live_candidate = tmp_path / "route-candidate"
    live_candidate.mkdir()
    # A stale target may exist from an earlier preparation. Preflight validates
    # only that it is safely replaceable; the transaction owns atomic creation
    # of the new release-specific candidate bytes.
    live_candidate.joinpath("stale-marker").write_text("old release\n", encoding="ascii")
    live_runner = tmp_path / "live-caddy-release.sh"
    live_runner.write_text("#!/bin/sh\n", encoding="ascii")
    live_runner.chmod(0o755)
    live_env = tmp_path / "route.env"
    live_env.write_text("PRIVATE_VALUE=not-reported\n", encoding="ascii")

    monkeypatch.setattr(MODULE, "LIVE_ROUTE_RUNNER", live_runner.resolve())
    monkeypatch.setattr(MODULE, "ROUTE_CANDIDATE", live_candidate.resolve())
    monkeypatch.setattr(MODULE, "ROUTE_ENV", live_env.resolve())
    monkeypatch.setattr(MODULE, "ROUTE_PHASE_JOURNAL", tmp_path / "route.phase")
    return {
        **production_preflight_host,
        "route_source": route_source,
        "live_candidate": live_candidate,
        "registry": registry,
        "tls_paths": tls_paths,
    }


def _sync_route_registry(fixture: dict[str, object]) -> None:
    source = fixture["route_source"] / "releases.json"
    source.write_text(json.dumps(fixture["registry"]), encoding="utf-8")


@pytest.mark.parametrize("deployment_class", ["security-config", "maintenance"])
def test_maintenance_classes_prove_route_transaction_prerequisites(
    maintenance_preflight_host: dict[str, object], deployment_class: str,
) -> None:
    report = MODULE._production_preflight(
        maintenance_preflight_host["bundle"],
        maintenance_preflight_host["candidate_release"],
        deployment_class,
    )

    route = report["checks"]["maintenance_route"]
    assert route["applicable"] is True
    assert route["candidate_target_state"] == "replaceable-directory"
    assert route["tls_files"] == len(MODULE.TLS_ROUTE_KEYS)
    assert route["fixed_ip_roles"] == ["caddy"]
    assert route["fixed_ip_collisions"] == 0
    assert "PRIVATE_VALUE" not in json.dumps(report)


def test_maintenance_preflight_rejects_tls_directory(
    maintenance_preflight_host: dict[str, object],
) -> None:
    bad = maintenance_preflight_host["tmp_path"] / "tls-directory"
    bad.mkdir()
    maintenance_preflight_host["registry"]["proxy"]["memory_tls_key"] = str(bad.resolve())
    _sync_route_registry(maintenance_preflight_host)

    with pytest.raises(MODULE.StageError, match="regular non-symlink file"):
        MODULE._production_preflight(
            maintenance_preflight_host["bundle"],
            maintenance_preflight_host["candidate_release"],
            "maintenance",
        )


def test_maintenance_preflight_rejects_fixed_ip_occupied_by_another_role(
    maintenance_preflight_host: dict[str, object],
) -> None:
    ingress = maintenance_preflight_host["inspect_values"]["yawndeploy-caddy-1"]
    ingress["Config"]["Labels"] = {
        "com.docker.compose.project": "menhir-ingress",
        "com.docker.compose.service": "cloudflared",
    }

    with pytest.raises(MODULE.StageError, match="fixed IP is occupied"):
        MODULE._production_preflight(
            maintenance_preflight_host["bundle"],
            maintenance_preflight_host["candidate_release"],
            "maintenance",
        )


def test_preflight_runs_before_image_loading_or_disposable_tree_creation() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    runner = source[source.index("def run_stage"):]

    assert runner.index("_production_preflight(") < runner.index("_ensure_images(")
    assert runner.index("_production_preflight(") < runner.index("STAGING_ROOT.mkdir(")
    assert '"production_preflight": production_preflight' in runner


def test_staging_sidecars_have_bounded_memory() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert source.count("mem_limit: 256m") == 2
