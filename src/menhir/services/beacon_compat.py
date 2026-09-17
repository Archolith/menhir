"""Version-checked subprocess boundary to a separately installed Beacon package.

Menhir pins ``archolith-mcp-framework==0.2.0`` while Beacon requires ``>=0.3.0``,
so Beacon is NEVER imported into Menhir's environment. All Beacon contact goes
through a fixed script executed by a caller-supplied Python interpreter that has
Beacon installed. The script is a fixed string: no repository-derived text is
ever executed. Menhir emits Beacon's YAML *input* mapping and the integration
tests pin correctness by round-tripping through Beacon's own loader/validator.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

__all__ = ["BeaconCompatError", "beacon_python_is_usable", "build_manifest_via_beacon", "validate_manifest_file"]

_REQUIRED_BEACON_VERSION = "0.1.0"
_SCRIPT = r"""
import json, sys
import yaml
from beacon import __version__ as beacon_version
from beacon.core.loader import parse_manifest
from beacon.core.validator import require_valid_manifest

mode = sys.argv[1]
if beacon_version != "0.1.0":
    print(f"unsupported beacon version: {beacon_version}", file=sys.stderr)
    raise SystemExit(3)
if mode == "build":
    raw = json.load(sys.stdin)
    docs_root = raw.pop("_docs_root", None)
    manifest = parse_manifest(raw)  # Beacon's own parsing/coercion
    require_valid_manifest(manifest, docs_root=docs_root)
    clean = {k: v for k, v in raw.items() if not k.startswith("_")}
    sys.stdout.write(yaml.safe_dump(clean, sort_keys=True, allow_unicode=False))
elif mode == "validate":
    import pathlib
    from beacon.core.loader import load_beacon_manifest
    from beacon.core.validator import validate_beacon_manifest
    path = sys.argv[2]
    manifest = load_beacon_manifest(path)
    report = validate_beacon_manifest(manifest, docs_root=pathlib.Path(path).parent)
    for issue in report.errors:
        print(f"error: {issue.where}: {issue.message}", file=sys.stderr)
    for issue in report.warnings:
        print(f"warning: {issue.where}: {issue.message}", file=sys.stderr)
    raise SystemExit(0 if report.ok else 1)
else:
    raise SystemExit(2)
"""


class BeaconCompatError(RuntimeError):
    """Raised when the Beacon compatibility boundary fails or is unavailable."""


def _run(beacon_python: str, args: list[str], *, stdin_bytes: bytes | None, cwd: Path | None) -> str:
    try:
        completed = subprocess.run(
            [beacon_python, "-c", _SCRIPT, *args],
            input=stdin_bytes,
            capture_output=True,
            timeout=60,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise BeaconCompatError(f"beacon python interpreter not found: {beacon_python}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BeaconCompatError("beacon subprocess timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BeaconCompatError(f"beacon subprocess failed ({completed.returncode}): {detail}")
    return completed.stdout.decode("utf-8", errors="replace")


def beacon_python_is_usable(beacon_python: str) -> None:
    """Fail closed unless the interpreter has exactly the supported Beacon version."""
    probe = "import json,sys;from beacon import __version__ as v;print(json.dumps({'v':v}))"
    try:
        completed = subprocess.run(
            [beacon_python, "-c", probe], capture_output=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise BeaconCompatError(f"beacon python interpreter unusable: {beacon_python}") from exc
    if completed.returncode != 0:
        raise BeaconCompatError(
            f"beacon is not importable from {beacon_python}: "
            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
        )
    version = json.loads(completed.stdout.decode())["v"]
    if version != _REQUIRED_BEACON_VERSION:
        raise BeaconCompatError(
            f"unsupported beacon version {version}; this boundary is tested against "
            f"{_REQUIRED_BEACON_VERSION} only"
        )


def build_manifest_via_beacon(
    beacon_python: str, raw_manifest: dict, *, docs_root: Path
) -> bytes:
    """Serialize ``raw_manifest`` through Beacon's own parser/validator; return YAML bytes."""
    payload = dict(raw_manifest)
    payload["_docs_root"] = str(docs_root)
    stdin = json.dumps(payload).encode("utf-8")
    output = _run(beacon_python, ["build"], stdin_bytes=stdin, cwd=docs_root)
    return output.encode("utf-8")


def validate_manifest_file(beacon_python: str, manifest_path: Path) -> None:
    """Validate an on-disk manifest with Beacon's authoritative validator; raise on errors."""
    _run(beacon_python, ["validate", str(manifest_path)], stdin_bytes=None, cwd=manifest_path.parent)
