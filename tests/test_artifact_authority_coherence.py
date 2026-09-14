"""Coherence of the installed-artifact authorities.

Since release 0.2.0-18 there is one authority, deploy/artifact-authority.json,
and deploy/lib/artifact_authority.py renders three of the four consumers from
it while release_spec loads it directly. The first test below proves the
rendered files are current; the cross-checks that follow are belt and braces
and document what used to be four independently hand-maintained lists:

  1. deploy/installed-artifacts.json    "destinations"
  2. deploy/release_spec.py             ARTIFACT_SOURCES
  3. deploy/release-install.sh          allowed frozenset (SystemExit)
  4. pipeline/bin/verify-artifacts      required (sys.exit(1))

Nothing compared (4) against (1)-(3). A divergence was therefore undetectable
off-host: the first honest signal was a failed install on production, after the
maintenance window had already been taken. Three release attempts failed here.

The same applies to the retirement lists: verify-artifacts' `obsolete` must
match release-install.sh's retired_caddy_* + retired_gateway_*, or the verifier
stops being an independent check on paths the installer just deleted.

Until release 0.2.0-16 the verifier lived in the yawn.vps repository and this
test skipped whenever that checkout was absent -- a blind spot in itself.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


VERIFIER = ROOT / "pipeline" / "bin" / "verify-artifacts"


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
    verifier = (VERIFIER).read_text(
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
    verifier = (VERIFIER).read_text(
        encoding="utf-8"
    )
    obsolete = _brace_set(verifier, "obsolete")
    retired = _installer_retired()
    assert obsolete == retired, (
        "verify-artifacts obsolete diverges from release-install.sh retirement "
        f"arrays: {sorted(obsolete ^ retired)}"
    )


def test_required_and_obsolete_are_disjoint() -> None:
    verifier = (VERIFIER).read_text(
        encoding="utf-8"
    )
    overlap = _brace_set(verifier, "required") & _brace_set(verifier, "obsolete")
    assert not overlap, f"paths both required and obsolete: {sorted(overlap)}"


def _authority_module():
    spec = importlib.util.spec_from_file_location(
        "_artifact_authority_coherence", DEPLOY / "lib" / "artifact_authority.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_targets_are_current() -> None:
    authority = _authority_module()
    stale = authority.drift()
    assert not stale, (
        "rendered files are behind deploy/artifact-authority.json; run "
        f"deploy/lib/artifact_authority.py --write: {[p.name for p in stale]}"
    )


def test_release_spec_loads_the_authority() -> None:
    authority = _authority_module()
    assert _artifact_sources() == set(authority.destinations())
    assert authority.sources()["/srv/menhir/production/bin/verify-artifacts"] == {
        "kind": "git", "repository": "menhir", "path": "pipeline/bin/verify-artifacts",
    }


def test_editing_the_authority_moves_every_consumer_together(tmp_path: Path) -> None:
    # Add one installed artifact and one retired path, render, and see the
    # census, verifier and installer all change in lockstep.
    authority = _authority_module()
    value = authority.load()
    value["artifacts"]["/srv/menhir/production/bin/zz-new-tool"] = {
        "kind": "git", "repository": "menhir", "path": "pipeline/bin/zz-new-tool",
    }
    value["artifacts"] = dict(sorted(value["artifacts"].items()))
    value["retired"]["gateway_scripts"].append("/srv/menhir/production/bin/zz-old-tool")
    rendered = authority.render_all(value)
    census = set(json.loads(rendered[authority.CENSUS])["destinations"])
    verifier_required = _brace_set(rendered[authority.VERIFIER], "required")
    verifier_obsolete = _brace_set(rendered[authority.VERIFIER], "obsolete")
    installer = rendered[authority.INSTALLER]
    allowed = {
        line for line in re.search(
            r'allowed = frozenset\(line for line in """(.*?)"""', installer, re.S
        ).group(1).splitlines() if line
    }
    retired_scripts = _bash_array(installer, "retired_gateway_scripts")
    new_tool = "/srv/menhir/production/bin/zz-new-tool"
    old_tool = "/srv/menhir/production/bin/zz-old-tool"
    assert new_tool in census and new_tool in verifier_required and new_tool in allowed
    assert old_tool in verifier_obsolete and old_tool in retired_scripts
    assert census == verifier_required == allowed
    assert verifier_obsolete == authority.retired_paths(value)


def test_authority_refuses_overlap_and_unsafe_paths(tmp_path: Path) -> None:
    authority = _authority_module()
    value = authority.load()
    value["retired"]["gateway_scripts"].append(next(iter(value["artifacts"])))
    broken = tmp_path / "authority.json"
    broken.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(authority.AuthorityError, match="both installed and retired"):
        authority.load(broken)
    value = authority.load()
    value["artifacts"]["../etc/evil"] = {"kind": "git", "repository": "menhir", "path": "x"}
    broken.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(authority.AuthorityError, match="unsafe destination"):
        authority.load(broken)
    for bad in ("/srv/menhir/../root/x", "/srv/x\n", "/srv//x"):
        value = authority.load()
        value["retired"]["gateway_scripts"].append(bad)
        broken.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(authority.AuthorityError, match="unsafe"):
            authority.load(broken)
    value = authority.load()
    value["retired"]["gateway_units"].append("-x.service")
    broken.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(authority.AuthorityError, match="unsafe"):
        authority.load(broken)


def test_replace_block_refuses_nested_or_miscounted_blocks() -> None:
    authority = _authority_module()
    begin, end = authority.BEGIN, authority.END
    nested = f"a\n{begin}\nold\n{begin}\nold2\n{end}\nc\n"
    with pytest.raises(authority.AuthorityError, match="expected 1"):
        authority._replace_block(nested, "R\n", "t")
    with pytest.raises(authority.AuthorityError, match="another block start"):
        authority._replace_block(nested, "R\n", "t", 0, 2)
    unterminated = f"a\n{begin}\nold\nfoo {end}\nc\n"
    with pytest.raises(authority.AuthorityError, match="unterminated"):
        authority._replace_block(unterminated, "R\n", "t")
    clean = f"a\n{begin}\nold\n{end}\nc\n"
    assert authority._replace_block(clean, "R\n", "t") == "a\nR\nc\n"
