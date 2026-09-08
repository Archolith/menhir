from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
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


@pytest.fixture(autouse=True)
def _verified_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release_helpers.MODULE.release_spec,
        "_verify_github_attestation",
        lambda **_kwargs: None,
    )


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
    assert (output / "install.sh").is_file()
    assert (output / "rootfs/srv/menhir/production/release/release.json").read_bytes() \
        == release_path.read_bytes()
    assert MODULE._validate_bundle(
        output, _sha256(output / "install.sh")
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


def test_revalidates_ci_publication_chain_before_bundling(tmp_path: Path) -> None:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    publication = Path(spec["evidence"]["image_publication"])
    value = json.loads(publication.read_text(encoding="ascii"))
    value["registry_digest"] = "sha256:" + "f" * 64
    publication.write_text(json.dumps(value), encoding="ascii")

    with pytest.raises(ValueError, match="registry digest"):
        MODULE.build_install_bundle(release_path, spec_path, tmp_path / "bundle")


def test_rejects_publication_evidence_drift_after_release_authoring(
    tmp_path: Path,
) -> None:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    publication_path = Path(spec["evidence"]["image_publication"])
    publication = json.loads(publication_path.read_text(encoding="ascii"))
    publication["registry_digest"] = "sha256:" + "f" * 64
    publication_path.write_text(json.dumps(publication), encoding="ascii")

    with pytest.raises(ValueError, match="registry digest|publication"):
        MODULE.build_install_bundle(
            release_path, spec_path, tmp_path / "bundle"
        )


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("deployment_class", "app-only"),
        ("notes_json_sha256", "0" * 64),
        ("notes_markdown_sha256", "1" * 64),
    ),
)
def test_rejects_release_authority_binding_mismatch(
    tmp_path: Path, key: str, value: str,
) -> None:
    release_path, spec_path, spec = _release_fixture(tmp_path)
    spec[key] = value
    spec_path.write_text(json.dumps(spec), encoding="ascii")

    with pytest.raises(ValueError, match=f"release spec {key}"):
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
    installer_digest = _sha256(output / "install.sh")
    if mutation == "extra":
        (output / "unexpected").write_text("extra\n", encoding="ascii")
    else:
        (output / "rootfs/srv/menhir/production/bin/status").unlink()
    with pytest.raises(ValueError, match="census|payload"):
        MODULE._validate_bundle(output, installer_digest)


def test_validator_rejects_manifest_digest_drift(tmp_path: Path) -> None:
    output, _, _, _ = _build(tmp_path)
    installer_digest = _sha256(output / "install.sh")
    status = output / "rootfs/srv/menhir/production/bin/status"
    status.write_bytes(status.read_bytes() + b"drift\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        MODULE._validate_bundle(output, installer_digest)


def test_fixed_destination_mode_policy() -> None:
    assert MODULE._destination_mode(
        "/srv/menhir/scaffold/bin/menhir_stage_vps.py"
    ) == "0755"
    assert MODULE._destination_mode(
        "/srv/menhir/production/release/release.json"
    ) == "0400"
    assert MODULE._destination_mode(
        "/srv/menhir/production/release/production.env"
    ) == "0400"
    assert MODULE._destination_mode("/etc/sudoers.d/menhir-production") == "0440"
    assert MODULE._destination_mode(
        "/srv/menhir/production/bin/release-run.sh"
    ) == "0755"
    assert MODULE._destination_mode(
        "/srv/menhir/production/bin/menhir_schema.py"
    ) == "0644"
    assert MODULE._destination_mode(
        "/srv/menhir/production/bin/lib.sh"
    ) == "0644"
    assert MODULE._destination_mode(
        "/etc/systemd/system/menhir-oauth-operations.service"
    ) == "0644"


def test_validator_rejects_manifest_mode_change(tmp_path: Path) -> None:
    output, _, _, _ = _build(tmp_path)
    manifest_path = output / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    destination = "/srv/menhir/production/bin/status"
    manifest["files"][destination]["mode"] = "0777"
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    with pytest.raises(ValueError, match="mode/digest"):
        MODULE._validate_bundle(output, _sha256(output / "install.sh"))


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
    for forbidden in ("groupadd", "usermod", '"${runner_source}"', "production_up"):
        assert forbidden not in source
    assert "bundle manifest destination allowlist mismatch" in source
    assert "bundle file census mismatch" in source
    assert "durable rollback evidence retained" in source
    assert "/srv/menhir/production/bin/verify-artifacts" in source
    assert "systemctl daemon-reload" in source
    assert "systemctl restart menhir-oauth-operations.service" in source
    assert "menhir-caddy-reconcile.path" in source
    assert "menhir-caddy-reconcile.service" in source
    assert "retire_obsolete_writers" in source
    assert "retired writer remains loaded or active" in source
    assert "production cutover was not started" in source


def test_installer_independently_binds_the_active_maintenance_holder() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    binding_validation = source.index(
        'path, release_id, release_sha, runner_sha = sys.argv[1:]'
    )
    helper_assertion = source.index("\nassert_maintenance\n", binding_validation)
    mutation_lock = source.index('exec 9>"$mutation_lock"', helper_assertion)
    second_assertion = source.index("\nassert_maintenance\n", mutation_lock)
    first_phase = source.index("journal_action phase retiring-caddy")

    assert 'maintenance_state="/var/lib/menhir-production/release-run.json"' in source
    assert 'require_safe_root_file "$maintenance_state" "maintenance journal"' in source
    assert '"release_manifest_sha256": release_sha' in source
    assert '"runner_sha256": runner_sha' in source
    assert 'value.get("completed_utc") is not None' in source
    assert helper_assertion < mutation_lock < second_assertion < first_phase
    assert 'python3 "$admission_helper" assert-maintenance "${admission_args[@]}"' \
        in source


def test_installer_journal_persists_complete_maintenance_binding() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    journal = source[
        source.index("journal_action() {"):source.index(
            "validate_destination_parents() {"
        )
    ]
    recovery = source[
        source.index('journal_output="$(journal_action inspect)"'):source.index(
            'if [ "$same_binding" -ne 1 ]'
        )
    ]

    assert '"approved_utc": approved_utc' in journal
    assert '"promotion_started_utc": promotion_started_utc' in journal
    assert '"approval_sha256", "promotion_attempt_id", "approved_utc",' in journal
    assert '"promotion_started_utc",' in journal
    assert '[ "$journal_approved_utc" = "$approved_utc" ]' in recovery
    assert '[ "$journal_promotion_started_utc" = "$promotion_started_utc" ]' \
        in recovery


def test_installer_obeys_admission_then_nonblocking_mutation_lock_order() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    first_assertion = source.index("\nassert_maintenance\n")
    mutation_lock = source.index('exec 9>"$mutation_lock"')
    nonblocking = source.index("flock -n 9", mutation_lock)
    retirement = source.index("retire_obsolete_writers\n", nonblocking)

    assert first_assertion < mutation_lock < nonblocking < retirement
    assert 'exit 75' in source[nonblocking:retirement]
    assert "menhir-production-admission.lock" not in source
    assert "flock -n 8" not in source


def test_installer_snapshots_every_retirement_surface_before_arming() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    snapshot = source[source.index("create_snapshot() {"):source.index("unit_property() {")]
    retirement = source[source.index("retire_obsolete_writers() {"):]

    assert 'snapshot_path "retired-${index}" "/etc/systemd/system/${unit}" file' \
        in snapshot
    assert 'snapshot_unit "$unit"' in snapshot
    assert 'snapshot_path "retired-${index}" "$path" file' in snapshot
    assert 'snapshot_path "retired-${index}" "$path" tree' in snapshot
    for property_name in ("LoadState", "UnitFileState", "ActiveState", "SubState"):
        assert f"--property={property_name}" in source
    assert snapshot.index('fsync_tree "$snapshot_root"') \
        < snapshot.index("journal_action phase armed")
    assert source.index("journal_action phase armed") \
        < source.index("journal_action phase retiring-caddy")
    for path in (
        "menhir-caddy-reconcile.path",
        "menhir-caddy-reconcile.service",
        "/srv/menhir/production/bin/caddy-release.sh",
        "/srv/menhir/production/bin/caddy-route-apply",
        "/srv/menhir/production/bin/caddy-route-rollback",
        "/srv/yawn/releases/menhir-route-candidate",
        "menhir-op@.service",
        "/srv/menhir/production/bin/worker",
        "/srv/menhir/production/bin/candidate-deploy",
        "/srv/menhir/production/bin/candidate-accept",
        "/srv/menhir/production/bin/backup",
        "/srv/menhir/production/bin/restore-rehearsal",
        "/srv/menhir/production/bin/restore-production",
        "/srv/menhir/production/bin/promote",
        "/srv/menhir/production/bin/rollback",
    ):
        assert path in snapshot + source[source.index("retired_caddy_units=("):source.index("fsync_directory() {")]
    assert 'for unit in "${retired_units[@]}"' in retirement
    assert 'for path in "${retired_scripts[@]}"' in retirement
    assert 'for path in "${retired_caddy_routes[@]}"' in retirement


def test_installer_refuses_active_transient_worker_before_lane_retirement() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    guard = source[
        source.index("assert_no_active_legacy_workers() {"):
        source.index("retire_obsolete_writers() {")
    ]
    stop_gateway = source.index("systemctl stop menhir-oauth-operations.service")
    first_guard = source.index("assert_no_active_legacy_workers\n", stop_gateway)
    retirement_phase = source.index("journal_action phase retiring-caddy", first_guard)
    second_guard = source.index("assert_no_active_legacy_workers\n", retirement_phase)
    retire = source.index("retire_obsolete_writers\n", second_guard)

    assert "systemctl list-units --type=service" in guard
    assert "--state=activating,active,reloading,deactivating" in guard
    assert "'menhir-op-*.service'" in guard
    assert "return 75" in guard
    assert stop_gateway < first_guard < retirement_phase < second_guard < retire
    retirement = source[source.index("retire_obsolete_writers() {"):]
    template_branch = retirement.split('if [[ "$unit" == *@.service ]]', 1)[1].split(
        "else", 1
    )[0]
    assert "disable --now" not in template_branch
    assert "systemctl stop" not in template_branch
    assert 'masked-runtime) systemctl unmask --runtime "$unit"' in template_branch


def test_installer_restores_exact_template_unit_file_state_on_rollback() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    arrays = source[
        source.index("retired_gateway_units=("):
        source.index("fsync_directory() {")
    ]
    snapshot = source[source.index("snapshot_unit() {"):source.index("create_snapshot() {")]
    restore = source[
        source.index("restore_unit_enablement() {"):
        source.index("rollback_install() {")
    ]
    rollback = source[source.index("rollback_install() {"):source.index("finish_install() {")]

    assert "menhir-op@.service" in arrays
    for property_name in ("LoadState", "UnitFileState", "ActiveState", "SubState"):
        assert f"--property={property_name}" in snapshot
        assert property_name in restore
    for state in ("enabled", "enabled-runtime", "masked", "masked-runtime"):
        assert state in restore
    assert '[[ "$unit" == *@.service ]]' in restore
    assert '[ "$state" = inactive ]' in restore
    assert 'for unit in "${retired_units[@]}"' in rollback
    assert 'systemctl unmask --runtime "$unit"' in rollback
    assert 'restore_unit_enablement "$unit"' in rollback
    assert 'restore_unit_activity "$unit"' in rollback
    assert "verify_restored_units" in rollback


def test_installer_journals_and_fsyncs_each_mutation_boundary() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    journal = source[source.index("journal_action() {"):source.index("validate_destination_parents() {")]

    for phase in (
        "snapshotting", "armed", "retiring-caddy", "installing", "verifying",
        "rolling-back", "rolled-back", "committed",
    ):
        assert f'"{phase}"' in journal
    for durability_step in (
        "handle.flush()", "os.fsync(handle.fileno())", "os.replace(temporary, path)",
        "os.fsync(directory)",
    ):
        assert durability_step in journal
    assert source.index("journal_action phase retiring-caddy") \
        < source.index("retire_obsolete_writers\n")
    assert source.index("journal_action phase installing") \
        < source.index('install -o root -g root -m "$mode" "$source" "$temporary"')
    verifying = source.index("journal_action phase verifying")
    assert verifying < source.index(
        "/srv/menhir/production/bin/verify-artifacts", verifying
    )
    assert source.index("assert_maintenance\n", source.index("journal_action phase verifying")) \
        < source.index("journal_action phase committed")


@pytest.mark.parametrize(
    "crash_phase",
    ["armed", "retiring-caddy", "installing", "verifying", "rolling-back"],
)
def test_crash_in_any_mutating_phase_recovers_without_rebaselining(
    crash_phase: str,
) -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    retirement = source.index("retire_obsolete_writers() {")
    recovery_start = source.rindex('case "$phase" in', 0, retirement)
    recovery = source[recovery_start:retirement]
    crash_arm = "armed|retiring-caddy|installing|verifying|rolling-back)"

    assert crash_phase in crash_arm
    assert crash_arm in recovery
    branch = recovery[recovery.index(crash_arm):recovery.index("rolled-back)")]
    assert branch.index("validate_snapshot_census") < branch.index("rollback_install")
    assert "create_snapshot" not in branch
    assert "retry the exact bundle" in branch


def test_installer_rejects_cross_binding_partial_state_and_only_archives_terminal_state() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    active = source[source.index('if [ -e "$transaction_root" ]'):source.index('if [ -z "$phase" ]')]

    assert 'transaction_root="${install_root}/active"' in source
    assert "active install transaction has no safe journal; refusing to re-baseline" in active
    assert 'committed|rolled-back) archive_terminal_transaction' in active
    assert "unfinished install transaction belongs to another maintenance binding" in active
    assert "snapshotting" not in active.split("case \"$phase\" in", 1)[1].split("esac", 1)[0]


def test_failure_trap_and_rolled_back_retry_are_idempotent() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    rollback = source[source.index("rollback_install() {"):source.index("finish_install() {")]
    finish = source[source.index("finish_install() {"):source.index("archive_terminal_transaction() {")]
    retirement = source.index("retire_obsolete_writers() {")
    recovery_start = source.rindex('case "$phase" in', 0, retirement)
    recovery = source[recovery_start:retirement]
    retry = recovery[recovery.index("rolled-back)"):recovery.index("committed)")]

    assert "journal_action phase rolling-back" in rollback
    assert 'rm -f -- "$destination"' in rollback
    assert 'rm -rf -- "$destination"' in rollback
    assert "systemctl disable --now" in rollback
    assert "journal_action phase rolled-back" in rollback
    assert 'if [ "$status" -ne 0 ] && [ "$transaction_active" -eq 1 ]' in finish
    assert "rollback_install" in finish
    assert retry.count("rollback_install") == 1
    assert retry.index("rollback_install") < retry.index("journal_action phase armed")
    assert "create_snapshot" not in retry


def test_install_journal_transition_graph_rejects_unsafe_recovery_edges() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    helper_start = source.index("import datetime", source.index("journal_action() {"))
    helper_end = source.index("\nPY\n}", helper_start)
    tree = ast.parse(source[helper_start:helper_end])
    transitions = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "allowed"
            for target in node.targets
        ):
            transitions = ast.literal_eval(node.value)
            break

    assert transitions is not None
    for crash_phase in (
        "armed", "retiring-caddy", "installing", "verifying", "rolling-back",
    ):
        assert "rolling-back" in transitions[crash_phase]
    assert transitions["committed"] == {"committed"}
    assert "armed" not in transitions["retiring-caddy"]
    assert "armed" not in transitions["installing"]
    assert "armed" not in transitions["verifying"]
    assert transitions["rolled-back"] >= {"rolling-back", "armed"}


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


def test_installer_excludes_obsolete_release_run_sudo_wrapper() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(
        encoding="ascii"
    )
    assert "\n/srv/menhir/production/bin/release-run\n" not in source
    assert "\n/srv/menhir/production/bin/release-run.sh\n" in source


def test_clean_install_excludes_obsolete_gateway_lane_but_keeps_canonical_scripts() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(encoding="ascii")
    allowlist = source.split('allowed = frozenset(line for line in """\n', 1)[1].split(
        '\n""".splitlines()', 1
    )[0]
    for obsolete in (
        "/etc/systemd/system/menhir-op@.service",
        "/srv/menhir/production/bin/worker",
        "/srv/menhir/production/bin/candidate-deploy",
        "/srv/menhir/production/bin/candidate-accept",
        "/srv/menhir/production/bin/backup",
        "/srv/menhir/production/bin/restore-rehearsal",
        "/srv/menhir/production/bin/restore-production",
        "/srv/menhir/production/bin/promote",
        "/srv/menhir/production/bin/rollback",
    ):
        assert obsolete not in allowlist.splitlines()
    for authoritative in (
        "/srv/menhir/production/bin/release-run.sh",
        "/srv/menhir/production/bin/backup-generation.sh",
        "/srv/menhir/production/bin/candidate-deploy.sh",
        "/srv/menhir/production/bin/candidate-accept.sh",
        "/srv/menhir/production/bin/promote.sh",
        "/srv/menhir/production/bin/rollback.sh",
    ):
        assert authoritative in allowlist.splitlines()


def test_installer_mode_policy_matches_bundle_builder() -> None:
    source = MODULE_PATH.with_name("release-install.sh").read_text(
        encoding="ascii"
    )
    assert 'and not destination.endswith(".py")' in source
    assert 'destination != "/srv/menhir/production/bin/lib.sh"' in source
