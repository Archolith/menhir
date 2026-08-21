"""Regenerate THIRD-PARTY-LICENSES.txt from the lockfile's runtime closure.

CF-169: the shipped manifest listed 51 packages against a 94-package extras-aware runtime closure,
because it was hand-maintained. CF-98 is the same disease in the other artifact -- `sbom.json` was
generated from a transient environment and drifted 32 versions ahead of the lock.

The fix for both is that neither file should be written by hand again. This derives the manifest
from `uv export --no-dev --all-extras`, which is the lock's own answer to "what does a user who
installs this actually get", so the manifest cannot disagree with the lock by construction.

Usage (from the repo root):

    .venv/Scripts/python.exe scripts/generate_license_manifest.py            # write the file
    .venv/Scripts/python.exe scripts/generate_license_manifest.py --check    # CI: fail on drift

``--check`` is the half that matters long term: it turns "someone remembered to regenerate" into a
build failure.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "THIRD-PARTY-LICENSES.txt"

#: Packages in the runtime closure that cannot be installed on this platform, so their metadata is
#: unreadable locally. Declared explicitly rather than silently omitted: they DO ship to a Linux
#: user, and a manifest that lists only what the generating machine happened to install is exactly
#: the environment-derived mistake CF-98 was about.
_OFF_PLATFORM: dict[str, tuple[str, str]] = {
    "jeepney": ("MIT", "https://gitlab.com/takluyver/jeepney"),
    "secretstorage": ("BSD-3-Clause", "https://github.com/mitya57/secretstorage"),
}

_NORM = lambda s: re.sub(r"[-_.]+", "-", s).lower()  # noqa: E731  (PEP 503)


def runtime_closure() -> dict[str, str]:
    """{normalized name: version} for the extras-aware, dev-excluded lock resolution."""
    out = subprocess.run(
        ["uv", "export", "--no-dev", "--all-extras", "--no-emit-project", "--no-hashes", "-q"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    pins: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line.strip())
        if m:
            pins[_NORM(m.group(1))] = m.group(2)
    if not pins:
        raise SystemExit("uv export produced no pins -- refusing to write an empty manifest")
    return pins


def _distributions() -> dict[str, md.Distribution]:
    found: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        name = dist.metadata.get("Name")
        if name:
            found.setdefault(_NORM(name), dist)
    return found


def _license_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        # "License :: OSI Approved :: MIT License" -> "MIT License"
        return classifiers[0].rsplit("::", 1)[-1].strip()
    raw = (meta.get("License") or "").strip()
    if raw and "\n" not in raw and len(raw) < 60:
        return raw
    # Last resort: read the bundled licence text. `caio` declares no License-Expression, no
    # License field and no classifiers, but ships `licenses/COPYING` containing Apache-2.0 -- so
    # reporting UNKNOWN would have been a failure of this generator, not an unlicensed package.
    return _license_from_bundled_text(dist)


#: Opening phrase of each licence's canonical text, which is how a bundled COPYING/LICENSE begins.
_TEXT_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("apache license", "Apache-2.0"),
    ("mit license", "MIT"),
    ("bsd 3-clause", "BSD-3-Clause"),
    ("bsd 2-clause", "BSD-2-Clause"),
    ("mozilla public license", "MPL-2.0"),
    ("isc license", "ISC"),
    ("gnu lesser general public", "LGPL"),
    ("gnu general public", "GPL"),
)


def _license_from_bundled_text(dist: md.Distribution) -> str:
    for entry in dist.files or []:
        name = str(entry).lower()
        if "licen" not in name and "copying" not in name:
            continue
        try:
            head = dist.locate_file(entry).read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        low = head.lower()
        for needle, spdx in _TEXT_SIGNATURES:
            if needle in low:
                return spdx
    return "UNKNOWN"


def _source_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    for key in ("Home-page", "Download-URL"):
        if meta.get(key):
            return str(meta[key]).strip()
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("homepage", "source", "repository", "source code"):
            return url.strip()
    for entry in meta.get_all("Project-URL") or []:
        return entry.partition(",")[2].strip()
    return ""


def render() -> str:
    pins = runtime_closure()
    dists = _distributions()
    rows: list[tuple[str, str, str, str]] = []
    unknown: list[str] = []
    for name in sorted(pins):
        version = pins[name]
        dist = dists.get(name)
        if dist is not None:
            lic, src = _license_of(dist), _source_of(dist)
        elif name in _OFF_PLATFORM:
            lic, src = _OFF_PLATFORM[name]
        else:
            lic, src = "UNKNOWN", ""
        if lic == "UNKNOWN":
            unknown.append(name)
        rows.append((name, version, lic, src))
    if unknown:
        print(f"warning: no license metadata for {len(unknown)}: {', '.join(unknown)}", file=sys.stderr)

    w_name = max(len(r[0]) for r in rows)
    w_ver = max(len(r[1]) for r in rows)
    w_lic = max(len(r[2]) for r in rows)
    lines = [
        "THIRD-PARTY LICENSES",
        "=" * 70,
        "",
        "Menhir (archolith-menhir) is licensed under the Apache License, Version 2.0.",
        "See LICENSE and NOTICE.",
        "",
        "This file lists the runtime dependency closure resolved from uv.lock (all extras,",
        "development and test-only dependencies excluded) and the license each package declares",
        "in its distribution metadata. Each package remains under its own license; this list is",
        "informational and does not relicense anything.",
        "",
        "GENERATED FILE -- do not edit by hand. Regenerate with:",
        "    python scripts/generate_license_manifest.py",
        "",
        f"Generated: {date.today().isoformat()}  |  derived from uv.lock  |  {len(rows)} packages",
        "",
        f"{'Package'.ljust(w_name)}  {'Version'.ljust(w_ver)}  {'License'.ljust(w_lic)}  Source",
        f"{'-' * w_name}  {'-' * w_ver}  {'-' * w_lic}  {'-' * 6}",
    ]
    for name, version, lic, src in rows:
        lines.append(f"{name.ljust(w_name)}  {version.ljust(w_ver)}  {lic.ljust(w_lic)}  {src}".rstrip())
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if the manifest is out of date")
    args = ap.parse_args()

    rendered = render()
    if args.check:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        # The Generated: line is a date and would fail every day; compare everything else.
        strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("Generated:"))
        if strip(current) != strip(rendered):
            print("THIRD-PARTY-LICENSES.txt is out of date; run scripts/generate_license_manifest.py",
                  file=sys.stderr)
            return 1
        print("THIRD-PARTY-LICENSES.txt is up to date")
        return 0

    MANIFEST.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {MANIFEST.relative_to(REPO)} ({rendered.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
