"""A failed release command must say why, on the one line the workflow reports."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_release_image_failure_output", ROOT / "deploy" / "build_release_image.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

PREFIX = "release image operation failed:"
DENIED = "denied: permission_denied: write_package"


def _failure_line(monkeypatch, capsys, exc: Exception) -> str:
    def run(_args):
        raise exc

    monkeypatch.setattr(MODULE, "run", run)
    monkeypatch.setattr(MODULE, "parser", lambda: type("P", (), {"parse_args": lambda s, a: a})())
    assert MODULE.main([]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(PREFIX)
    return lines[0]


def _called(stderr=None, stdout=None) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["docker", "push", "x"], output=stdout, stderr=stderr)


def test_real_captured_command_failure_reports_its_stderr(monkeypatch, capsys):
    script = f"import sys; print('Preparing'); sys.stderr.write('{DENIED}\\n'); sys.exit(1)"
    with pytest.raises(subprocess.CalledProcessError) as raised:
        subprocess.run(
            [sys.executable, "-c", script], check=True, capture_output=True, text=True,
        )
    line = _failure_line(monkeypatch, capsys, raised.value)
    # The message quotes the command, so check only what follows it.
    assert line.rpartition("exit status 1.")[2] == f": {DENIED}"


@pytest.mark.parametrize("stderr", [DENIED, DENIED.encode()])
def test_text_and_bytes_output_are_both_reported(monkeypatch, capsys, stderr):
    assert _failure_line(monkeypatch, capsys, _called(stderr=stderr)).endswith(DENIED)


def test_stdout_is_used_when_stderr_is_empty(monkeypatch, capsys):
    assert _failure_line(monkeypatch, capsys, _called(stderr="", stdout=DENIED)).endswith(DENIED)


def test_uncaptured_command_keeps_the_plain_message(monkeypatch, capsys):
    line = _failure_line(monkeypatch, capsys, _called())
    assert line.endswith("returned non-zero exit status 1.")


def test_multiline_output_stays_on_one_line(monkeypatch, capsys):
    line = _failure_line(monkeypatch, capsys, _called(stderr="first\nsecond\r\n\n  third\n"))
    assert line.endswith("first second third")


def test_long_output_keeps_the_end(monkeypatch, capsys):
    line = _failure_line(monkeypatch, capsys, _called(stderr="x" * 5000 + DENIED))
    assert line.endswith(DENIED)
    assert line.count("x") <= MODULE.MAX_FAILURE_DETAIL_CHARS


@pytest.mark.parametrize("credential", [
    "Bearer abc.def-123",
    "Authorization: basic dXNlcjpwYXNz",
    "ghp_" + "a" * 36,
    "ghs_" + "b" * 36,
    "github_pat_" + "c" * 40,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
])
def test_credentials_are_redacted(monkeypatch, capsys, credential):
    line = _failure_line(monkeypatch, capsys, _called(stderr=f"{credential} {DENIED}"))
    secret = credential.split()[-1]
    assert secret not in line
    assert "[redacted]" in line
    assert line.endswith(DENIED)


def test_credential_at_the_trim_boundary_leaves_no_fragment(monkeypatch, capsys):
    token = "ghp_" + "a" * 36
    padding = "y" * (MODULE.MAX_FAILURE_DETAIL_CHARS - 20)
    line = _failure_line(monkeypatch, capsys, _called(stderr=f"{token} {padding}"))
    assert "aaaa" not in line


def test_other_failures_are_unchanged(monkeypatch, capsys):
    line = _failure_line(monkeypatch, capsys, MODULE.BuildImageError("no manifest digest"))
    assert line == f"{PREFIX} no manifest digest"
