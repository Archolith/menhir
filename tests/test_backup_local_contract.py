"""Contract tests for the provider-free local Menhir backup wrapper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "menhir-backup-local.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_local_backup_contract_has_no_remote_provider_dependency() -> None:
    source = _source().lower()
    for forbidden in (
        "contabo",
        "object lock",
        "s3api",
        "aws_access_key_id",
        "/root/.aws",
        "off-host",
        "worm",
    ):
        assert forbidden not in source


def test_local_backup_uses_fixed_production_roots() -> None:
    source = _source()
    assert 'ARCHIVE_ROOT="/srv/menhir/backups/encrypted"' in source
    assert 'GENERATIONS_ROOT="/srv/menhir/backups/generations"' in source
    assert 'AGE_IDENTITY="/etc/menhir/backup-restore.agekey"' in source
    assert 'RECEIPT_PATH="${STATUS_DIR}/backup-local-receipt.json"' in source
    assert 'RETENTION_TARGET_GENERATIONS=2' in source


def test_local_backup_roundtrip_precedes_receipt_and_plaintext_cleanup() -> None:
    source = _source()
    encrypt = source.index("age --encrypt")
    decrypt = source.index("age --decrypt")
    receipt = source.index('"kind": "backup-local"')
    cleanup = source.index('"$CLEANUP_TXN" begin')
    assert encrypt < decrypt < receipt < cleanup
    assert '"roundtrip_verified": True' in source


def test_production_paths_cannot_be_redirected_without_explicit_test_mode() -> None:
    source = _source()
    assert 'TEST_MODE="${MENHIR_BACKUP_LOCAL_ALLOW_NON_ROOT_TEST:-0}"' in source
    assert 'if [ "$TEST_MODE" = 1 ]; then' in source
    assert 'else\n    [ "$(id -u)" -eq 0 ]' in source


def _bash() -> str | None:
    """A runnable bash. On Windows, System32\\bash.exe is the WSL launcher, which fails
    without an installed distro; use Git for Windows' bash instead."""
    found = shutil.which("bash")
    if found is None or Path(found).parent.name.lower() != "system32":
        return found
    git = shutil.which("git")
    if git is None:
        return None
    # git.exe sits in <Git>\cmd or <Git>\mingw64\bin; bash.exe is in <Git>\bin.
    for root in Path(git).resolve().parents[:3]:
        git_bash = root / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return None


BASH = _bash()


@pytest.mark.skipif(BASH is None, reason="bash is unavailable")
def test_local_backup_wrapper_has_valid_bash_syntax() -> None:
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-n", "deploy/menhir-backup-local.sh"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
