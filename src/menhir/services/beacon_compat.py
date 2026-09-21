"""Contract-checked subprocess boundary to a separately installed Beacon package.

Menhir and Beacon pin incompatible framework versions, so Beacon is NEVER
imported into Menhir's environment. All Beacon contact is a subprocess run by
a caller-supplied interpreter, using fixed argument vectors only: no shell, no
repository-derived text is ever executed.

Compatibility gate (issue #120 debt, removed 2026-09-18): the old boundary
hard-failed unless ``beacon.__version__ == "0.1.0"`` exactly, which refused
the actual v0.2/v0.3 implementations while the manifest schema stayed
``"0.1"``. The gate is now the **supported build contract**: the interpreter
must provide a Beacon whose CLI supports the two operations Menhir relies on
— ``beacon build`` (generation from the evidence document) and
``beacon validate`` (authoritative validation). The manifest schema version
is what the artifact carries; the product version is irrelevant and is no
longer inspected.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "BeaconCompatError",
    "beacon_python_is_usable",
    "build_manifest_via_beacon",
    "child_environment",
    "validate_manifest_file",
]

#: The build contract: the manifest/schema contract Beacon must support for
#: Menhir's generated artifacts. Beacon's ``beacon_version`` manifest field
#: stays ``"0.1"`` across these product versions.
_REQUIRED_COMMANDS = ("build", "validate")

_SUBPROCESS_TIMEOUT_SECONDS = 120


#: Environment variables a Beacon child (and the git it runs) may inherit. Everything else is
#: dropped: Menhir's process holds NEO4J_PASSWORD, provider API keys, and auth tokens loaded by
#: ``load_menhir_env``, and a caller-chosen ``--beacon-python`` must never see them (PR #125 F3).
#: Process plumbing only -- executable lookup, the Windows runtime, home/config lookup for git,
#: temp space, and locale.
_ALLOWED_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
    }
)
#: Interpreter behaviour switches that cannot redirect imports. PYTHONPATH, PYTHONHOME,
#: PYTHONSTARTUP and friends are deliberately NOT forwarded: they would splice Menhir's import
#: path into Beacon's isolated interpreter, which is the dependency clash the separate venv exists
#: to prevent. No BEACON_* variable is forwarded because Menhir sets none; the child's behaviour
#: is fully determined by the fixed argv.
_ALLOWED_PYTHON_ENV = frozenset({"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"})


class BeaconCompatError(RuntimeError):
    """Raised when the Beacon compatibility boundary fails or is unavailable."""


def child_environment(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the minimal allowlisted environment for a Beacon (or git) child process."""
    source = os.environ if parent is None else parent
    env: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in _ALLOWED_ENV or upper in _ALLOWED_PYTHON_ENV or upper.startswith("LC_"):
            env[key] = value
    return env


def _run(beacon_python: str, args: list[str], *, cwd: Path | None, timeout: int) -> str:
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv, no shell
            [beacon_python, *args],
            capture_output=True,
            check=False,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=child_environment(),
        )
    except FileNotFoundError as exc:
        raise BeaconCompatError(
            f"beacon python interpreter not found: {beacon_python}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BeaconCompatError("beacon subprocess timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BeaconCompatError(
            f"beacon subprocess failed ({completed.returncode}): {detail}"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def beacon_python_is_usable(beacon_python: str) -> None:
    """Fail closed unless the interpreter provides the supported build contract.

    Probes ``beacon build --help`` and ``beacon validate --help``: Menhir
    requires those two commands, not any particular product version.
    """
    probe = "from beacon import __version__ as v; print(v)"
    try:
        _run(beacon_python, ["-c", probe], cwd=None, timeout=30)
    except BeaconCompatError as exc:
        raise BeaconCompatError(
            f"beacon is not importable from {beacon_python}: {exc}"
        ) from exc
    for command in _REQUIRED_COMMANDS:
        try:
            _run(
                beacon_python, ["-m", "beacon", command, "--help"], cwd=None, timeout=60
            )
        except BeaconCompatError as exc:
            raise BeaconCompatError(
                f"beacon at {beacon_python} does not support the required "
                f"'{command}' command; unsupported build contract"
            ) from exc


def build_manifest_via_beacon(
    beacon_python: str,
    *,
    repo_root: Path,
    evidence_path: Path,
    note: str,
) -> bytes:
    """Generate manifest YAML through ``beacon build``; return the exact bytes.

    Beacon owns projection, serialization, and validation; the manifest is
    streamed to stdout (``--out -``) so Menhir keeps publication ownership
    (atomic replace, advisory lock, compare-and-swap refresh) without ever
    mapping manifest fields itself.
    """
    output = _run(
        beacon_python,
        [
            "-m",
            "beacon",
            "build",
            "--repo",
            str(repo_root),
            "--menhir-evidence",
            str(evidence_path),
            "--out",
            "-",
            "--note",
            note,
        ],
        cwd=repo_root,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
    return output.encode("utf-8")


def validate_manifest_file(beacon_python: str, manifest_path: Path) -> None:
    """Validate an on-disk manifest with Beacon's authoritative validator; raise on errors."""
    _run(
        beacon_python,
        ["-m", "beacon", "validate", str(manifest_path)],
        cwd=manifest_path.parent,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
