from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "release_spec.py"
SPEC = importlib.util.spec_from_file_location("release_spec", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _canonical_digest(value: dict) -> str:
    canonical = dict(value)
    canonical.pop("canonical_digest", None)
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")).hexdigest()


def _run(*args: str) -> str:
    return subprocess.run(
        list(args), check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, remote: str, files: dict[str, bytes]) -> str:
    path.mkdir()
    _run("git", "init", "-q", str(path))
    _run("git", "-C", str(path), "config", "user.email", "test@example.com")
    _run("git", "-C", str(path), "config", "user.name", "Test")
    _run("git", "-C", str(path), "remote", "add", "origin", remote)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _run("git", "-C", str(path), "add", ".")
    _run("git", "-C", str(path), "commit", "-qm", "fixture")
    head = _run("git", "-C", str(path), "rev-parse", "HEAD")
    _run(
        "git", "-C", str(path), "update-ref",
        "refs/remotes/origin/main", head,
    )
    return head


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )


@pytest.fixture
def release_fixture(tmp_path: Path, monkeypatch):
    policy = {"version": 2, "access_contract": {}, "clients": {}}
    policy["canonical_digest"] = _canonical_digest(policy)
    policy_bytes = (
        json.dumps(policy, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")

    files_by_repo = {name: {} for name in MODULE.REPOSITORIES}
    for source in MODULE.ARTIFACT_SOURCES.values():
        if source["kind"] == "git":
            files_by_repo[source["repository"]][source["path"]] = (
                source["path"] + "\n"
            ).encode("ascii")
    files_by_repo["menhir"]["deploy/installed-artifacts.json"] = (
        MODULE_PATH.parent / "installed-artifacts.json"
    ).read_bytes()
    files_by_repo["menhir"]["deploy/client-policy.production.json"] = policy_bytes
    files_by_repo["menhir"]["deploy/docker-compose.production.yml"] = (
        b"name: menhir-prod\n"
    )
    files_by_repo["yawn_deploy"]["docker-compose.yml"] = b"services: {}\n"
    files_by_repo["yawn_deploy"]["Caddyfile"] = b"example.invalid {}\n"
    files_by_repo["yawn_deploy"]["releases.json"] = b"{}\n"
    files_by_repo["archolith_oauth"]["src/archolith_oauth/__init__.py"] = b""

    repos = {}
    commits = {}
    for name in sorted(MODULE.REPOSITORIES):
        repo = tmp_path / name
        commits[name] = _repo(
            repo, MODULE.menhir_schema.EXPECTED_REPO_REMOTES[name],
            files_by_repo[name],
        )
        repos[name] = str(repo.resolve())

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "archolith_oauth-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "archolith_oauth-1.0.dist-info/METADATA",
            "Name: archolith-oauth\nVersion: 1.0\n",
        )
        archive.writestr("archolith_oauth/__init__.py", "")
    (wheelhouse / "SHA256SUMS").write_text(
        hashlib.sha256(wheel.read_bytes()).hexdigest() + "  " + wheel.name + "\n",
        encoding="ascii",
    )
    sbom = tmp_path / "sbom.json"
    scan = tmp_path / "scan.json"
    prior = tmp_path / "prior-release.json"
    route = tmp_path / "prior-route.json"
    for path in (sbom, scan, prior, route):
        path.write_text("{}\n", encoding="ascii")
    baseline = tmp_path / "production.env"
    baseline.write_text(
        "MENHIR_IMAGE=old\n"
        "NEO4J_IMAGE=old\n"
        "MENHIR_RELEASE_COMMIT=old\n"
        "MENHIR_RELEASE_ID=old\n"
        "MENHIR_CLIENT_POLICY_DIGEST=" + "0" * 64 + "\n"
        "MENHIR_RUNTIME_MODE=production\n",
        encoding="ascii",
    )
    public_key = tmp_path / "oauth-public.pem"
    public_key.write_text(
        "-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----\n",
        encoding="ascii",
    )
    runtime = tmp_path / "runtime.sha256"
    runtime.write_text("sha256:" + "9" * 64 + "\n", encoding="ascii")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret_versions = {
        name: "version-" + name for name in MODULE.SECRET_VERSIONS
    }
    secret_versions["client-policy"] = "sha256-" + policy["canonical_digest"]
    operations = {
        "schema": 1,
        "issuer": "https://memory.example",
        "audience": "https://memory.example/ops/mcp",
        "base_url": "https://memory.example/ops",
        "clients": {
            "client-id": {
                "tier": "operator",
                "scopes": ["menhir:read", "menhir:write"],
                "tools": ["menhir_status"],
            }
        },
    }
    operations_path = tmp_path / "operations.json"
    _write_json(operations_path, operations)
    inputs = {
        "schema": 1,
        "release_id": "menhir-prod-1.2.3-9",
        "release_author": "operator@example.com",
        "release_workspace_root": str(workspace.resolve()),
        "repositories": repos,
        "images": {
            name: {
                "digest": "sha256:" + str(index) * 64,
                "ref": (
                    f"registry.example/{name}:1@sha256:"
                    + str(index) * 64
                ),
            }
            for index, name in enumerate(sorted(MODULE.IMAGES), start=1)
        },
        "evidence": {
            "wheelhouse": str(wheelhouse.resolve()),
            "sbom": str(sbom.resolve()),
            "scan": str(scan.resolve()),
        },
        "baseline_production_env": str(baseline.resolve()),
        "operations_policy": str(operations_path.resolve()),
        "oauth_public_key": str(public_key.resolve()),
        "python_runtime_digest": str(runtime.resolve()),
        "prior_release": str(prior.resolve()),
        "prior_route": str(route.resolve()),
        "yawn_env_sha256": "sha256:" + "8" * 64,
        "secret_version_ids": secret_versions,
    }
    inputs_path = tmp_path / "release-inputs.json"
    _write_json(inputs_path, inputs)
    monkeypatch.setattr(
        MODULE.menhir_schema,
        "validate_release",
        lambda unused: {
            "release_id": "menhir-prod-1.2.3-8",
            "rendered": {"yawn_env_sha256": "a" * 64},
        },
    )
    return {
        "inputs": inputs,
        "inputs_path": inputs_path,
        "workspace": workspace,
        "output": workspace / "release-spec.json",
        "repos": repos,
        "commits": commits,
        "operations": operations,
        "operations_path": operations_path,
        "baseline": baseline,
        "policy_digest": policy["canonical_digest"],
    }


def _save(fixture) -> None:
    _write_json(fixture["inputs_path"], fixture["inputs"])


def test_prepares_deterministic_release_author_spec(release_fixture) -> None:
    fixture = release_fixture
    first = MODULE.prepare_release_spec(
        fixture["inputs_path"], fixture["output"]
    )
    first_bytes = fixture["output"].read_bytes()
    first_assets = {
        item.name: item.read_bytes()
        for item in (fixture["workspace"] / "release-spec-inputs").iterdir()
    }
    second = MODULE.prepare_release_spec(
        fixture["inputs_path"], fixture["output"], overwrite=True
    )

    assert first == second
    assert fixture["output"].read_bytes() == first_bytes
    assert first_assets == {
        item.name: item.read_bytes()
        for item in (fixture["workspace"] / "release-spec-inputs").iterdir()
    }
    assert first["repositories"] == fixture["repos"]
    assert first["initial_release"] is False
    env = Path(first["rendered"]["production_env_sha256"]).read_text(
        encoding="ascii"
    )
    assert f"MENHIR_RELEASE_COMMIT={fixture['commits']['menhir']}" in env
    assert "MENHIR_CLIENT_POLICY_DIGEST=" + fixture["policy_digest"] in env


def test_refuses_dirty_and_non_tip_repositories(release_fixture) -> None:
    fixture = release_fixture
    menhir = Path(fixture["repos"]["menhir"])
    (menhir / "dirty.txt").write_text("dirty\n", encoding="ascii")
    with pytest.raises(MODULE.ReleaseSpecError, match="not clean"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])
    (menhir / "dirty.txt").unlink()
    _run(
        "git", "-C", str(menhir), "update-ref", "-d",
        "refs/remotes/origin/main",
    )
    with pytest.raises(MODULE.ReleaseSpecError, match="remote-tracking tip"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_remote_mismatch(release_fixture) -> None:
    fixture = release_fixture
    repo = fixture["repos"]["yawn_vps"]
    _run(
        "git", "-C", repo, "remote", "set-url", "origin",
        "https://github.com/attacker/yawn.vps.git",
    )
    with pytest.raises(MODULE.ReleaseSpecError, match="origin mismatch"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_duplicate_unknown_and_missing_json_keys(release_fixture) -> None:
    fixture = release_fixture
    text = fixture["inputs_path"].read_text(encoding="ascii")
    fixture["inputs_path"].write_text(
        text.replace('"schema": 1,', '"schema": 1,\n  "schema": 1,', 1),
        encoding="ascii",
    )
    with pytest.raises(MODULE.ReleaseSpecError, match="duplicate JSON key"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])
    fixture["inputs"]["unexpected"] = True
    _save(fixture)
    with pytest.raises(MODULE.ReleaseSpecError, match="keys mismatch"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])
    del fixture["inputs"]["unexpected"]
    del fixture["inputs"]["evidence"]["scan"]
    _save(fixture)
    with pytest.raises(MODULE.ReleaseSpecError, match="keys mismatch"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_duplicate_env_keys(release_fixture) -> None:
    fixture = release_fixture
    with fixture["baseline"].open("a", encoding="ascii") as handle:
        handle.write("MENHIR_RELEASE_ID=duplicate\n")
    with pytest.raises(MODULE.ReleaseSpecError, match="duplicate production.env"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_symlinked_input(release_fixture, tmp_path: Path) -> None:
    fixture = release_fixture
    link = tmp_path / "linked-route.json"
    try:
        link.symlink_to(Path(fixture["inputs"]["prior_route"]))
    except OSError:
        pytest.skip("symlinks are unavailable")
    fixture["inputs"]["prior_route"] = str(link.absolute())
    _save(fixture)
    with pytest.raises(MODULE.ReleaseSpecError, match="non-symlink"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_client_policy_secret_version_mismatch(release_fixture) -> None:
    fixture = release_fixture
    fixture["inputs"]["secret_version_ids"]["client-policy"] = "wrong-version"
    _save(fixture)
    with pytest.raises(
        MODULE.ReleaseSpecError, match="must bind the client policy digest"
    ):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_operations_policy_schema_drift(release_fixture) -> None:
    fixture = release_fixture
    fixture["operations"]["unexpected"] = True
    _write_json(fixture["operations_path"], fixture["operations"])
    with pytest.raises(MODULE.ReleaseSpecError, match="keys mismatch"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_installed_artifact_mapping_drift(
    release_fixture, monkeypatch
) -> None:
    fixture = release_fixture
    mapping = dict(MODULE.ARTIFACT_SOURCES)
    mapping.pop(next(iter(mapping)))
    monkeypatch.setattr(MODULE, "ARTIFACT_SOURCES", mapping)
    with pytest.raises(MODULE.ReleaseSpecError, match="mapping drift"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_artifact_source_exceptions_match_proven_release_layout() -> None:
    assert MODULE.ARTIFACT_SOURCES[
        "/srv/menhir/production/bin/caddy-release.sh"
    ] == {
        "kind": "git",
        "repository": "yawn_deploy",
        "path": "caddy-release.sh",
    }
    assert MODULE.ARTIFACT_SOURCES[
        "/srv/menhir/production/bin/verify_python_runtime.py"
    ] == {
        "kind": "git",
        "repository": "yawn_vps",
        "path": "ops/menhir/bin/verify_python_runtime.py",
    }
    for name in (
        "menhir-caddy-reconcile.path",
        "menhir-caddy-reconcile.service",
        "menhir-oauth-operations.service",
        "menhir-op@.service",
    ):
        assert MODULE.ARTIFACT_SOURCES[f"/etc/systemd/system/{name}"] == {
            "kind": "git",
            "repository": "yawn_vps",
            "path": f"ops/menhir/systemd/{name}",
        }


@pytest.mark.parametrize("field", ["digest", "ref"])
def test_refuses_malformed_image_digest_or_ref(
    release_fixture, field: str
) -> None:
    fixture = release_fixture
    fixture["inputs"]["images"]["menhir"][field] = "not-immutable"
    _save(fixture)
    with pytest.raises(MODULE.ReleaseSpecError, match="images.menhir"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_secret_looking_env_and_config_values(release_fixture) -> None:
    fixture = release_fixture
    with fixture["baseline"].open("a", encoding="ascii") as handle:
        handle.write("OPENAI_API_KEY=sk_test_secret_material_123456\n")
    with pytest.raises(MODULE.ReleaseSpecError, match="secret-looking"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])
    fixture["baseline"].write_text(
        fixture["baseline"].read_text(encoding="ascii").split(
            "OPENAI_API_KEY", 1
        )[0],
        encoding="ascii",
    )
    fixture["operations"]["clients"]["client-id"]["scopes"] = (
        ["sk_secret_material_123456"]
    )
    _write_json(fixture["operations_path"], fixture["operations"])
    with pytest.raises(MODULE.ReleaseSpecError, match="secret-looking"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_refuses_output_escape_and_existing_output(
    release_fixture, tmp_path: Path
) -> None:
    fixture = release_fixture
    escaped = tmp_path / "release-spec.json"
    with pytest.raises(MODULE.ReleaseSpecError, match="release_workspace_root"):
        MODULE.prepare_release_spec(fixture["inputs_path"], escaped)
    traversal = fixture["workspace"] / "nested" / ".." / "release-spec.json"
    with pytest.raises(MODULE.ReleaseSpecError, match="release_workspace_root"):
        MODULE.prepare_release_spec(fixture["inputs_path"], traversal)
    fixture["output"].write_text("{}\n", encoding="ascii")
    with pytest.raises(MODULE.ReleaseSpecError, match="already exists"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])


def test_cleans_temporary_state_on_failure(
    release_fixture, monkeypatch
) -> None:
    fixture = release_fixture
    original = MODULE._write_json

    def fail_on_provenance(path: Path, value: dict) -> None:
        if path.name == "provenance.json":
            raise MODULE.ReleaseSpecError("injected failure")
        original(path, value)

    monkeypatch.setattr(MODULE, "_write_json", fail_on_provenance)
    with pytest.raises(MODULE.ReleaseSpecError, match="injected failure"):
        MODULE.prepare_release_spec(fixture["inputs_path"], fixture["output"])
    assert not fixture["output"].exists()
    assert not (fixture["workspace"] / "release-spec-inputs").exists()
    assert not list(fixture["workspace"].glob(".release-spec.*"))
