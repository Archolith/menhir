"""The Beacon subprocess boundary must not hand Menhir's secrets to the child (PR #125 F3)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from menhir.services.beacon_compat import build_manifest_via_beacon

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
