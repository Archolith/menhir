"""The Beacon subprocess boundary must not hand Menhir's secrets to the child (PR #125 F3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from menhir.services.beacon_compat import (
    BeaconCompatError,
    beacon_python_is_usable,
    build_manifest_via_beacon,
    validate_manifest_file,
)

_SECRET = "pr125-f3-probe-secret"


def _fake_beacon_python(tmp_path: Path) -> str:
    """An 'interpreter' that ignores its argv and prints its environment as JSON."""
    script = tmp_path / "dump_env.py"
    script.write_text(
        "import json, os, sys\nsys.stdout.write(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = tmp_path / "fake-python.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}"\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "fake-python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return str(wrapper)


def _installed_beacon_python(tmp_path: Path) -> str:
    """Put a fake Beacon in a separate interpreter's site-packages."""
    venv_dir = tmp_path / "beacon-venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    if os.name == "nt":
        python = venv_dir / "Scripts" / "python.exe"
    else:
        python = venv_dir / "bin" / "python"
    site_packages = Path(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    package = site_packages / "beacon"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.3.0"\n', encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os, sys\n"
        "if '--help' in sys.argv:\n"
        "    print('trusted help')\n"
        "elif sys.argv[1] == 'build':\n"
        "    if '--note' in sys.argv and sys.argv[sys.argv.index('--note') + 1] == 'fail':\n"
        "        sys.stderr.write('secret=' + os.environ.get('NEO4J_PASSWORD', '<absent>'))\n"
        "        raise SystemExit(2)\n"
        "    print('trusted build')\n"
        "elif sys.argv[1] == 'validate':\n"
        "    print('trusted validate')\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    return str(python)


@pytest.mark.parametrize("hostile_kind", ["module", "package"])
def test_repo_local_beacon_cannot_shadow_installed_beacon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile_kind: str
) -> None:
    beacon_python = _installed_beacon_python(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    marker = tmp_path / "hostile-executed"
    hostile_code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('repo-local Beacon executed')\n"
    )
    if hostile_kind == "module":
        (repo_root / "beacon.py").write_text(hostile_code, encoding="utf-8")
    else:
        hostile_package = repo_root / "beacon"
        hostile_package.mkdir()
        (hostile_package / "__init__.py").write_text(hostile_code, encoding="utf-8")
        (hostile_package / "__main__.py").write_text(hostile_code, encoding="utf-8")

    monkeypatch.chdir(repo_root)
    beacon_python_is_usable(beacon_python)
    output = build_manifest_via_beacon(
        beacon_python,
        repo_root=repo_root,
        evidence_path=repo_root / "evidence.json",
        note="note",
    )
    validate_manifest_file(beacon_python, repo_root / "manifest.yaml")

    assert output.decode("utf-8").splitlines() == ["trusted build"]
    assert not marker.exists()


def test_beacon_failure_does_not_reveal_parent_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beacon_python = _installed_beacon_python(tmp_path)
    monkeypatch.setenv("NEO4J_PASSWORD", _SECRET)

    with pytest.raises(BeaconCompatError) as exc_info:
        build_manifest_via_beacon(
            beacon_python,
            repo_root=tmp_path,
            evidence_path=tmp_path / "evidence.json",
            note="fail",
        )

    assert _SECRET not in str(exc_info.value)
    assert "secret=<absent>" in str(exc_info.value)


def test_isolated_beacon_child_keeps_utf8_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.services.beacon_compat import _run

    monkeypatch.setenv("PYTHONUTF8", "1")
    output = _run(sys.executable, ["-c", "print(chr(233))"], cwd=None, timeout=10)
    assert output.strip() == "é"


def test_beacon_child_does_not_inherit_parent_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", _SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    monkeypatch.setenv("MENHIR_AUTH_TOKEN", _SECRET)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "menhir-import-path"))
    monkeypatch.setenv("BEACON_MANIFEST_PATH", str(tmp_path / "elsewhere.yaml"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other-repo.git"))

    output = build_manifest_via_beacon(
        _fake_beacon_python(tmp_path),
        repo_root=tmp_path,
        evidence_path=tmp_path / "evidence.json",
        note="note",
    )
    child_env = {k.upper(): v for k, v in json.loads(output.decode("utf-8")).items()}

    assert _SECRET not in child_env.values()
    for name in (
        "NEO4J_PASSWORD", "OPENAI_API_KEY", "MENHIR_AUTH_TOKEN",
        "PYTHONPATH", "BEACON_MANIFEST_PATH", "GIT_DIR",
    ):
        assert name not in child_env, name
    # Process plumbing still reaches the child, so Beacon can find git and run at all.
    assert "PATH" in child_env


def test_child_environment_is_an_allowlist() -> None:
    from menhir.services.beacon_compat import child_environment

    parent = {
        "PATH": "/bin",
        "Path": "C:/Windows",
        "SystemRoot": "C:/Windows",
        "HOME": "/home/u",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONUTF8": "1",
        "PYTHONHOME": "/menhir",
        "NEO4J_PASSWORD": _SECRET,
        "ANTHROPIC_API_KEY": _SECRET,
        "AWS_SECRET_ACCESS_KEY": _SECRET,
    }
    assert child_environment(parent) == {
        "PATH": "/bin",
        "Path": "C:/Windows",
        "SystemRoot": "C:/Windows",
        "HOME": "/home/u",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONUTF8": "1",
    }
