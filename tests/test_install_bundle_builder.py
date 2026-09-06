from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests import test_release_author as release_helpers


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "build_install_bundle.py"
SPEC = importlib.util.spec_from_file_location("build_install_bundle", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    spec_path, release_path, spec = release_helpers._fixture(tmp_path)
    release_helpers._author(spec_path, release_path)
    return release_path, spec_path, spec


def _build(tmp_path: Path, name: str = "install-bundle") -> tuple[Path, Path, Path, dict]:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    output = tmp_path / name
    manifest = MODULE.build_install_bundle(release_path, spec_path, output)
    return output, release_path, spec_path, {"spec": spec, "manifest": manifest}


def _snapshot(root: Path) -> list[tuple[str, str, int, int]]:
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_file():
            result.append((relative, _sha256(path), stat.S_IMODE(info.st_mode),
                           info.st_mtime_ns))
    return result


def _git(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _mode_repo(tmp_path: Path, mode: str, name: str) -> tuple[Path, str, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    payload = repo / "payload"
    payload.write_text("target\n", encoding="ascii")
    _git(["add", "payload"], repo)
    _git(["commit", "-qm", "base"], repo)
    base_commit = _git(["rev-parse", "HEAD"], repo)
    oid = base_commit if mode == "160000" \
        else _git(["hash-object", "-w", "payload"], repo)
    _git(["update-index", "--add", "--cacheinfo", f"{mode},{oid},unsafe"], repo)
    _git(["commit", "-qm", "unsafe mode"], repo)
    commit = _git(["rev-parse", "HEAD"], repo)
    return repo, commit, oid


def test_builds_release_bound_bundle_from_real_git_fixture(tmp_path: Path) -> None:
    output, release_path, _, values = _build(tmp_path)
    manifest = values["manifest"]
    assert manifest["kind"] == "menhir-release-install-bundle"
    assert manifest["release_sha256"] == _sha256(release_path)
    assert (output / "release-install.sh").is_file()
    assert (output / "rootfs/srv/menhir/production/release/release.json").read_bytes() \
        == release_path.read_bytes()
    assert MODULE._validate_bundle(
        output, _sha256(output / "release-install.sh")
    ) == manifest


@pytest.mark.parametrize(
    "value",
    ["../escape", "a/../escape", r"a\escape", "/absolute", "a//b", "./a"],
)
def test_rejects_hostile_git_source_paths(value: str) -> None:
    with pytest.raises(ValueError, match="canonical repository-relative"):
        MODULE._canonical_source_path(value, "source")


@pytest.mark.parametrize(
    "value",
    ["relative", "/srv/menhir/production/bin/../escape", r"/srv/menhir\escape"],
)
def test_rejects_hostile_destination_paths(value: str) -> None:
    with pytest.raises(ValueError, match="approved"):
        MODULE._canonical_destination(value, frozenset({value}), "destination")


def test_rejects_symlink_release_input(tmp_path: Path) -> None:
    release_path, spec_path, _ = _release_fixture(tmp_path)
    link = tmp_path / "linked-release.json"
    try:
        link.symlink_to(release_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(ValueError, match="non-symlink"):
        MODULE.build_install_bundle(link, spec_path, tmp_path / "bundle")


def test_rejects_symlink_installer_input(tmp_path: Path) -> None:
    release_path, spec_path, _ = _release_fixture(tmp_path)
    installer = MODULE_PATH.with_name("release-install.sh")
    link = tmp_path / "linked-installer.sh"
    try:
        link.symlink_to(installer)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(ValueError, match="non-symlink"):
        MODULE.build_install_bundle(
            release_path, spec_path, tmp_path / "bundle", link
        )


def test_rejects_duplicate_release_spec_json_key(tmp_path: Path) -> None:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    body = json.dumps(spec, sort_keys=True)
    spec_path.write_text(
        body.replace("{", '{"schema":1,', 1), encoding="ascii"
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        MODULE.build_install_bundle(release_path, spec_path, tmp_path / "bundle")


def test_rejects_duplicate_release_json_key(tmp_path: Path) -> None:
    release_path, spec_path, _ = _release_fixture(tmp_path)
    body = release_path.read_text(encoding="ascii")
    release_path.chmod(0o600)
    release_path.write_text(
        body.replace("{", '{"schema":1,', 1), encoding="ascii"
    )
    with pytest.raises(ValueError, match="duplicate"):
        MODULE.build_install_bundle(release_path, spec_path, tmp_path / "bundle")


def test_rejects_rendered_digest_drift(tmp_path: Path) -> None:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    rendered = Path(spec["rendered"]["production_env_sha256"])
    rendered.write_bytes(rendered.read_bytes() + b"DRIFT\n")
    with pytest.raises(ValueError, match="digest drift"):
        MODULE.build_install_bundle(release_path, spec_path, tmp_path / "bundle")


def test_rejects_output_outside_spec_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release_path, spec_path, _ = _release_fixture(workspace)
    escaped = tmp_path / "escaped-bundle"
    with pytest.raises(ValueError, match="workspace root"):
        MODULE.build_install_bundle(release_path, spec_path, escaped)


def test_rejects_existing_output(tmp_path: Path) -> None:
    release_path, spec_path, _ = _release_fixture(tmp_path)
    output = tmp_path / "bundle"
    output.mkdir()
    with pytest.raises(ValueError, match="already exist"):
        MODULE.build_install_bundle(release_path, spec_path, output)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_rejects_git_symlink_and_gitlink_modes(tmp_path: Path, mode: str) -> None:
    repo, commit, oid = _mode_repo(tmp_path, mode, "unsafe-repo")
    with pytest.raises(ValueError, match="unsafe or unknown git mode"):
        MODULE._git_blob(repo, commit, "unsafe", oid, "artifact")


def test_rejects_missing_git_blob(tmp_path: Path) -> None:
    repo, commit, _ = _mode_repo(tmp_path, "100644", "regular-repo")
    with pytest.raises(ValueError, match="missing"):
        MODULE._git_blob(repo, commit, "absent", "0" * 40, "artifact")


def test_rejects_git_blob_oid_mismatch(tmp_path: Path) -> None:
    repo, commit, _ = _mode_repo(tmp_path, "100644", "regular-repo")
    with pytest.raises(ValueError, match="object id"):
        MODULE._git_blob(repo, commit, "unsafe", "0" * 40, "artifact")


def test_rejects_inconsistent_repository_checkout(tmp_path: Path) -> None:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    spec["repositories"]["menhir"] = spec["repositories"]["yawn_vps"]
    spec_path.write_text(json.dumps(spec), encoding="ascii")
    with pytest.raises(ValueError, match="inconsistent|missing"):
        MODULE.build_install_bundle(release_path, spec_path, tmp_path / "bundle")


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_validator_rejects_extra_or_missing_bundle_files(
    tmp_path: Path, mutation: str
) -> None:
    output, _, _, _ = _build(tmp_path)
    installer_digest = _sha256(output / "release-install.sh")
    if mutation == "extra":
        (output / "unexpected").write_text("extra\n", encoding="ascii")
    else:
        (output / "rootfs/srv/menhir/production/bin/worker").unlink()
    with pytest.raises(ValueError, match="census|payload"):
        MODULE._validate_bundle(output, installer_digest)


def test_validator_rejects_manifest_digest_drift(tmp_path: Path) -> None:
    output, _, _, _ = _build(tmp_path)
    installer_digest = _sha256(output / "release-install.sh")
    worker = output / "rootfs/srv/menhir/production/bin/worker"
    worker.write_bytes(worker.read_bytes() + b"drift\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        MODULE._validate_bundle(output, installer_digest)


def test_fixed_destination_mode_policy() -> None:
    assert MODULE._destination_mode(
        "/srv/menhir/production/release/release.json"
    ) == "0400"
    assert MODULE._destination_mode(
        "/srv/menhir/production/release/production.env"
    ) == "0400"
    assert MODULE._destination_mode("/etc/sudoers.d/menhir-production") == "0440"
    assert MODULE._destination_mode(
        "/srv/menhir/production/bin/release-run"
    ) == "0755"
    assert MODULE._destination_mode(
        "/srv/menhir/production/bin/menhir_schema.py"
    ) == "0644"
    assert MODULE._destination_mode(
        "/srv/menhir/production/bin/lib.sh"
    ) == "0644"
    assert MODULE._destination_mode(
        "/etc/systemd/system/menhir-op@.service"
    ) == "0644"


def test_validator_rejects_manifest_mode_change(tmp_path: Path) -> None:
    output, _, _, _ = _build(tmp_path)
    manifest_path = output / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    destination = "/srv/menhir/production/bin/worker"
    manifest["files"][destination]["mode"] = "0777"
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    with pytest.raises(ValueError, match="mode/digest"):
        MODULE._validate_bundle(output, _sha256(output / "release-install.sh"))


def test_cleans_temporary_sibling_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_path, spec_path, _ = _release_fixture(tmp_path)
    output = tmp_path / "bundle"

    def fail_validation(root: Path, installer_digest: str) -> dict:
        raise ValueError("forced validation failure")

    monkeypatch.setattr(MODULE, "_validate_bundle", fail_validation)
    with pytest.raises(ValueError, match="forced validation"):
        MODULE.build_install_bundle(release_path, spec_path, output)
    assert not output.exists()
    assert list(tmp_path.glob(".bundle.tmp-*")) == []


def test_bundle_output_is_deterministic(tmp_path: Path) -> None:
    release_path, spec_path, _ = _release_fixture(tmp_path)
    first = tmp_path / "bundle-one"
    second = tmp_path / "bundle-two"
    MODULE.build_install_bundle(release_path, spec_path, first)
    MODULE.build_install_bundle(release_path, spec_path, second)
    assert _snapshot(first) == _snapshot(second)


def test_installer_keeps_scaffold_and_cutover_out_of_routine_install() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    for forbidden in ("groupadd", "usermod", "systemctl enable", "release-run.sh\""):
        assert forbidden not in source
    assert "bundle manifest destination allowlist mismatch" in source
    assert "bundle file census mismatch" in source
    assert "replaced files restored" in source
    assert "/srv/menhir/production/bin/verify-artifacts" in source
    assert "production cutover was not started" in source


def test_installer_allowlist_matches_installed_artifact_census() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(
        encoding="ascii"
    )
    match = re.search(
        r'allowed = frozenset\(line for line in """\n(.*?)\n"""\.splitlines\(\)',
        source,
        re.DOTALL,
    )
    assert match is not None
    installer_destinations = set(match.group(1).splitlines())
    census = json.loads(
        MODULE_PATH.with_name("installed-artifacts.json").read_text(
            encoding="ascii"
        )
    )
    assert installer_destinations == set(census["destinations"])


def test_installer_mode_policy_matches_bundle_builder() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(
        encoding="ascii"
    )
    assert 'and not destination.endswith(".py")' in source
    assert 'destination != "/srv/menhir/production/bin/lib.sh"' in source
