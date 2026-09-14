#!/usr/bin/env python3
"""The one authority for what a Menhir release installs and retires.

``deploy/artifact-authority.json`` names every installed destination with its
source and every retired path. Three files are rendered from it and must never
be edited by hand: ``deploy/installed-artifacts.json`` (the census, itself an
installed artifact), the ``required``/``obsolete`` sets compiled into
``pipeline/bin/verify-artifacts``, and the ``allowed``/``retired_*`` blocks in
``deploy/release-install.sh``. ``release_spec.ARTIFACT_SOURCES`` loads the
authority directly. The host-executed scripts keep compiled-in copies on
purpose: a tampered host census cannot relax the verifier.

    python3 deploy/lib/artifact_authority.py --check   # exit 1 on drift
    python3 deploy/lib/artifact_authority.py --write   # regenerate
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORITY = REPO_ROOT / "deploy" / "artifact-authority.json"
CENSUS = REPO_ROOT / "deploy" / "installed-artifacts.json"
VERIFIER = REPO_ROOT / "pipeline" / "bin" / "verify-artifacts"
INSTALLER = REPO_ROOT / "deploy" / "release-install.sh"

RETIRED_GROUPS = (
    "caddy_units", "caddy_scripts", "caddy_routes", "gateway_units", "gateway_scripts",
)
UNIT_GROUPS = ("caddy_units", "gateway_units")
BEGIN = "# BEGIN GENERATED from deploy/artifact-authority.json -- do not edit by hand"
END = "# END GENERATED"
_DESTINATION_RE = re.compile(r"^/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9._@-]+\.(?:service|timer|path)$")


class AuthorityError(ValueError):
    pass


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityError(f"duplicate key: {key}")
        result[key] = value
    return result


def load(path: Path = AUTHORITY) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"artifact authority unreadable: {path}") from exc
    if set(value) != {"schema", "kind", "artifacts", "retired"} or value["schema"] != 1 \
            or value["kind"] != "menhir-artifact-authority":
        raise AuthorityError("artifact authority schema is invalid")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise AuthorityError("artifact authority has no artifacts")
    for destination, source in artifacts.items():
        if not _DESTINATION_RE.match(destination):
            raise AuthorityError(f"unsafe destination: {destination}")
        if not isinstance(source, dict):
            raise AuthorityError(f"artifacts[{destination}] must be an object")
        kind = source.get("kind")
        if kind == "git":
            if set(source) != {"kind", "repository", "path"} \
                    or not isinstance(source["repository"], str) \
                    or not isinstance(source["path"], str) \
                    or source["path"].startswith("/") or ".." in source["path"].split("/"):
                raise AuthorityError(f"artifacts[{destination}] git source is invalid")
        elif kind == "rendered":
            if set(source) != {"kind", "rendered_key"} \
                    or not isinstance(source["rendered_key"], str):
                raise AuthorityError(f"artifacts[{destination}] rendered source is invalid")
        else:
            raise AuthorityError(f"artifacts[{destination}].kind must be git or rendered")
    retired = value["retired"]
    if not isinstance(retired, dict) or set(retired) != set(RETIRED_GROUPS):
        raise AuthorityError("artifact authority retired groups are invalid")
    seen: set[str] = set()
    for group in RETIRED_GROUPS:
        rows = retired[group]
        if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
            raise AuthorityError(f"retired.{group} must be a list of strings")
        pattern = _UNIT_RE if group in UNIT_GROUPS else _DESTINATION_RE
        for row in rows:
            if not pattern.match(row):
                raise AuthorityError(f"retired.{group} entry is unsafe: {row}")
            if row in seen:
                raise AuthorityError(f"retired path listed twice: {row}")
            seen.add(row)
    if list(artifacts) != sorted(artifacts):
        raise AuthorityError("artifact authority artifacts must be sorted by destination")
    overlap = set(artifacts) & retired_paths(value)
    if overlap:
        raise AuthorityError(f"paths both installed and retired: {sorted(overlap)}")
    return value


def sources(value: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    value = load() if value is None else value
    return {destination: dict(source) for destination, source in value["artifacts"].items()}


def destinations(value: dict[str, Any] | None = None) -> list[str]:
    value = load() if value is None else value
    return list(value["artifacts"])


def retired_paths(value: dict[str, Any] | None = None) -> set[str]:
    """Every retired path as the verifier's obsolete set sees it."""
    value = load() if value is None else value
    retired = value["retired"]
    paths = set(retired["caddy_scripts"]) | set(retired["gateway_scripts"])
    paths |= {f"/etc/systemd/system/{unit}" for group in UNIT_GROUPS for unit in retired[group]}
    return paths


def render_census(value: dict[str, Any]) -> str:
    return json.dumps({"schema": 1, "destinations": destinations(value)}, indent=2) + "\n"


def _python_set(name: str, rows: list[str]) -> str:
    body = "".join(f'    "{row}",\n' for row in rows)
    return f"{name} = {{\n{body}}}\n"


def render_verifier_block(value: dict[str, Any]) -> str:
    required = destinations(value)
    obsolete = sorted(retired_paths(value))
    return BEGIN + "\n" + _python_set("required", required) + _python_set("obsolete", obsolete) + END + "\n"


def render_installer_allowed_block(value: dict[str, Any]) -> str:
    rows = "".join(f"{row}\n" for row in destinations(value))
    return (
        BEGIN + "\n"
        'allowed = frozenset(line for line in """\n' + rows + '""".splitlines() if line)\n'
        + END + "\n"
    )


def render_installer_retired_block(value: dict[str, Any]) -> str:
    retired = value["retired"]
    out = [BEGIN]
    for group in RETIRED_GROUPS:
        out.append(f"retired_{group}=(")
        out.extend(f"    {row}" for row in retired[group])
        out.append(")")
    out.append(END)
    return "\n".join(out) + "\n"


def _replace_block(text: str, rendered: str, label: str, occurrence: int = 0) -> str:
    starts = [match.start() for match in re.finditer(re.escape(BEGIN) + r"\n", text)]
    if len(starts) <= occurrence:
        raise AuthorityError(f"{label}: generated block {occurrence} not found")
    start = starts[occurrence]
    end = text.find(END + "\n", start)
    if end < 0:
        raise AuthorityError(f"{label}: generated block is unterminated")
    return text[:start] + rendered + text[end + len(END) + 1:]


def render_all(value: dict[str, Any]) -> dict[Path, str]:
    verifier = VERIFIER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    installer = _replace_block(installer, render_installer_allowed_block(value), "installer allowed", 0)
    installer = _replace_block(installer, render_installer_retired_block(value), "installer retired", 1)
    return {
        CENSUS: render_census(value),
        VERIFIER: _replace_block(verifier, render_verifier_block(value), "verifier", 0),
        INSTALLER: installer,
    }


def drift(value: dict[str, Any] | None = None) -> list[Path]:
    value = load() if value is None else value
    stale = []
    for path, rendered in render_all(value).items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != rendered:
            stale.append(path)
    return stale


def write(value: dict[str, Any] | None = None) -> list[Path]:
    value = load() if value is None else value
    written = []
    for path, rendered in render_all(value).items():
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            path.write_text(rendered, encoding="utf-8", newline="\n")
            written.append(path)
    return written


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        stale = drift()
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}")
        return 1 if stale else 0
    if argv == ["--write"]:
        for path in write():
            print(f"wrote: {path.relative_to(REPO_ROOT)}")
        return 0
    print("usage: artifact_authority.py --check | --write", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
