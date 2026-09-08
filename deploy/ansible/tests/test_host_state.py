"""SSH testinfra assertions for a provisioned Menhir host."""

import pytest


DIRECTORIES = {
    "/srv/menhir": 0o755,
    "/srv/menhir/production": 0o755,
    "/srv/menhir/backups": 0o755,
    "/srv/menhir/backups/encrypted": 0o700,
    "/srv/menhir/scaffold": 0o755,
    "/srv/menhir/scaffold/bin": 0o755,
    "/srv/menhir/staging-transactions": 0o700,
    "/srv/menhir/install-transactions": 0o700,
    "/srv/menhir/scaffold-transactions": 0o700,
    "/var/lib/menhir-production": 0o755,
    "/var/log/menhir-production": 0o755,
    "/etc/menhir": 0o700,
    "/etc/yawn-vps": 0o755,
}

FILES = {
    "/etc/tmpfiles.d/menhir-production.conf": 0o644,
    "/etc/systemd/system/menhir-scaffold-audit.service": 0o644,
    "/etc/systemd/system/menhir-scaffold-audit.timer": 0o644,
}

RETIRED_CADDY_WRITER_SCRIPTS = (
    "/srv/menhir/production/bin/caddy-release.sh",
    "/srv/menhir/production/bin/caddy-route-apply",
    "/srv/menhir/production/bin/caddy-route-rollback",
)

SCAFFOLD_AUDIT_SHA256 = (
    "33b746f26fed20a6f1ea5081f7b3f8b0aafeb6821f6dec7315ce54f5bca1c012"
)


@pytest.mark.parametrize("path,mode", DIRECTORIES.items())
def test_root_owned_directory_contract(host, path, mode):
    directory = host.file(path)
    assert directory.is_directory
    assert directory.user == "root"
    assert directory.group == "root"
    assert directory.mode == mode


@pytest.mark.parametrize("path,mode", FILES.items())
def test_root_owned_file_contract(host, path, mode):
    managed_file = host.file(path)
    assert managed_file.is_file
    assert managed_file.user == "root"
    assert managed_file.group == "root"
    assert managed_file.mode == mode


def test_scaffold_audit_executable_is_exact_and_safe(host):
    executable = host.file("/srv/menhir/scaffold/bin/menhir_scaffold.py")
    assert executable.is_file
    assert not executable.is_symlink
    assert executable.user == "root"
    assert executable.group == "root"
    assert executable.mode == 0o755
    assert executable.sha256sum == SCAFFOLD_AUDIT_SHA256


@pytest.mark.parametrize(
    "unit",
    ("menhir-scaffold-audit.timer",),
)
def test_host_monitor_is_enabled_and_active(host, unit):
    service = host.service(unit)
    assert service.is_enabled
    assert service.is_running


def test_no_failed_menhir_units(host):
    result = host.run(
        "systemctl list-units --failed --no-legend --plain 'menhir-*'"
    )
    assert result.rc == 0
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    "unit",
    ("menhir-caddy-reconcile.path", "menhir-caddy-reconcile.service"),
)
def test_legacy_caddy_writer_is_absent(host, unit):
    definition = host.file(f"/etc/systemd/system/{unit}")
    assert not definition.exists
    assert not definition.is_symlink
    enabled = host.run(f"systemctl is-enabled {unit}")
    active = host.run(f"systemctl is-active {unit}")
    assert enabled.rc == 4
    assert enabled.stdout.strip() == "not-found"
    assert active.rc in {3, 4}
    assert active.stdout.strip() in {"inactive", "unknown"}


@pytest.mark.parametrize("path", RETIRED_CADDY_WRITER_SCRIPTS)
def test_legacy_caddy_writer_script_is_absent(host, path):
    script = host.file(path)
    assert not script.exists
    assert not script.is_symlink
