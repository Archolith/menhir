"""Cross-repo coherence of the installed-artifact authorities.

The installed artifact list is hardcoded in four places across two repositories,
and each enforces it independently with a hard failure:

  1. menhir  deploy/installed-artifacts.json    "destinations"
  2. menhir  deploy/release_spec.py             ARTIFACT_SOURCES
  3. menhir  deploy/release-install.sh          allowed frozenset (SystemExit)
  4. yawn.vps ops/menhir/bin/verify-artifacts   required (sys.exit(1))

Nothing compared (4) against (1)-(3). A divergence was therefore undetectable
off-host: the first honest signal was a failed install on production, after the
maintenance window had already been taken. Three release attempts failed here.

The same applies to the retirement lists: verify-artifacts' `obsolete` must
match release-install.sh's retired_caddy_* + retired_gateway_*, or the verifier
stops being an independent check on paths the installer just deleted.

yawn.vps lives in a separate repository, so its location is supplied by
MENHIR_YAWN_VPS_ROOT. When it is absent this test SKIPS rather than passes --
a silent pass here would recreate exactly the blind spot it exists to close.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _yawn_vps_root() -> Path:
    raw = os.environ.get("MENHIR_YAWN_VPS_ROOT")
    if not raw:
        pytest.skip("MENHIR_YAWN_VPS_ROOT is not set; cannot check the verifier")
    root = Path(raw)
    if not (root / "ops/menhir/bin/verify-artifacts").is_file():
        pytest.skip(f"verify-artifacts not found under {root}")
    return root


def _brace_set(source: str, name: str) -> set[str]:
    match = re.search(name + r"\s*=\s*(\{.*?\n\})", source, re.S)
    assert match, f"{name} set not found"
    return set(ast.literal_eval(match.group(1)))


def _census() -> set[str]:
    data = json.loads((DEPLOY / "installed-artifacts.json").read_text(encoding="utf-8"))
    return set(data["destinations"])


def _artifact_sources() -> set[str]:
    # Built from a dict literal plus several loops that add f-string keys, so it
    # must be imported. Scraping the literal statically undercounts it.
    spec = importlib.util.spec_from_file_location(
        "_release_spec_coherence", DEPLOY / "release_spec.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.ARTIFACT_SOURCES)


def _installer() -> str:
    return (DEPLOY / "release-install.sh").read_text(encoding="utf-8")


def _allowed() -> set[str]:
    match = re.search(
        r'allowed = frozenset\(line for line in """(.*?)"""', _installer(), re.S
    )
    assert match, "allowed frozenset not found"
    return {line for line in match.group(1).splitlines() if line}


def _bash_array(source: str, name: str) -> set[str]:
    match = re.search(name + r"=\((.*?)\n\)", source, re.S)
    assert match, f"{name} array not found"
    return {line.strip() for line in match.group(1).split() if line.strip()}


def _installer_retired() -> set[str]:
    src = _installer()
    scripts = _bash_array(src, "retired_caddy_scripts") | _bash_array(
        src, "retired_gateway_scripts"
    )
    units = _bash_array(src, "retired_caddy_units") | _bash_array(
        src, "retired_gateway_units"
    )
    return scripts | {f"/etc/systemd/system/{unit}" for unit in units}


def test_menhir_side_authorities_agree() -> None:
    census, sources, allowed = _census(), _artifact_sources(), _allowed()
    assert sources == census, (
        f"ARTIFACT_SOURCES vs census: "
        f"{sorted(sources ^ census)}"
    )
    assert allowed == census, (
        f"release-install.sh allowed vs census: {sorted(allowed ^ census)}"
    )


def test_verifier_required_matches_census() -> None:
    verifier = (_yawn_vps_root() / "ops/menhir/bin/verify-artifacts").read_text(
        encoding="utf-8"
    )
    required = _brace_set(verifier, "required")
    census = _census()
    assert required == census, (
        "verify-artifacts required diverges from installed-artifacts.json; "
        "a release built from this would fail on the host, after the "
        f"maintenance window: {sorted(required ^ census)}"
    )


def test_verifier_obsolete_matches_installer_retirement() -> None:
    verifier = (_yawn_vps_root() / "ops/menhir/bin/verify-artifacts").read_text(
        encoding="utf-8"
    )
    obsolete = _brace_set(verifier, "obsolete")
    retired = _installer_retired()
    assert obsolete == retired, (
        "verify-artifacts obsolete diverges from release-install.sh retirement "
        f"arrays: {sorted(obsolete ^ retired)}"
    )


def test_required_and_obsolete_are_disjoint() -> None:
    verifier = (_yawn_vps_root() / "ops/menhir/bin/verify-artifacts").read_text(
        encoding="utf-8"
    )
    overlap = _brace_set(verifier, "required") & _brace_set(verifier, "obsolete")
    assert not overlap, f"paths both required and obsolete: {sorted(overlap)}"
