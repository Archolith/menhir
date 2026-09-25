#!/usr/bin/env python3
"""Menhir production mutation census.

Enumerates every source path that can mutate the Menhir production host, across
every repository that participates in deployment.

Why this exists
---------------
A fourth privileged PowerShell wrapper (`scripts/menhir-backup-archive.ps1`,
which pipes base64-decoded Python into `sudo -n python3 -`) survived two weeks
of deployment remediation, ~46 commits, and two declared audit closures. It was
found by listing a directory. Nothing had ever enumerated what can actually
deploy to the box.

Design constraints, learned the hard way
----------------------------------------
1. Findings are produced by NAMED DETECTION RULES, never by a hand-written
   inventory. A hand list goes stale silently; a rule set can be re-run and
   diffed, so a newly added mutator shows up as an unclassified item.

2. Every finding must carry a DISPOSITION and an OWNER, supplied out-of-band in
   a dispositions file. An unclassified finding fails the census. This is the
   mechanical "no unclassified item" check.

3. Findings have STABLE IDs derived from (repo, path, detector, evidence), so a
   disposition survives line-number drift but a genuinely changed call site
   becomes a new unclassified item that must be re-judged.

Read-only. Touches no host. Exits non-zero when the census does not pass.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    # Script mode (python pipeline/census.py) puts this directory on sys.path;
    # the relative fallback covers package-style import of pipeline.census.
    from census_rules import RULES, Rule as Rule
except ImportError:
    from .census_rules import RULES, Rule as Rule

SCHEMA = "menhir.census.v1"

# Files we never scan: binaries, caches, vendored trees, and our own outputs.
SKIP_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".tox",
    # Vendored/downloaded dependency trees. These are third-party code that
    # cannot be a Menhir deployment path, and scanning them buries the real
    # findings: the first census run produced 3978 menhir findings, the bulk of
    # them from setuptools, pytest and pywin32 inside .uv-cache.
    ".uv-cache", "site-packages", ".cache", "wheelhouse", "vendor",
}
SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif",
    ".pdf", ".zip", ".gz", ".tar", ".whl", ".ico", ".woff", ".woff2", ".db",
    ".sqlite", ".sqlite3", ".bak", ".lock",
}
MAX_BYTES = 2_000_000


@dataclass
class Finding:
    id: str
    detector: str
    kind: str
    severity: str
    repo: str
    path: str
    line: int
    evidence: str
    why: str


def stable_id(repo: str, path: str, detector: str, evidence: str) -> str:
    """ID is independent of line number so dispositions survive edits above the
    call site, but bound to the evidence text so a changed call site is new."""
    norm = re.sub(r"\s+", " ", evidence.strip())[:200]
    h = hashlib.sha256(f"{repo}\0{path}\0{detector}\0{norm}".encode()).hexdigest()
    return h[:16]


def iter_files(root: Path, excludes: list[str]):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        rel = p.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        yield p, rel


def scan_repo(name: str, root: Path, excludes: list[str]) -> list[Finding]:
    out: list[Finding] = []
    for path, rel in iter_files(root, excludes):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:1024]:
            continue
        is_doc = path.suffix.lower() in {".md", ".rst", ".txt", ".adoc"}
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 4000:
                line = line[:4000]
            stripped = line.strip()
            # Skip pure comment lines: they document, they do not execute.
            if stripped.startswith(("#", "//", "*", "<!--")):
                continue
            for rule in RULES:
                if rule.pattern.search(line):
                    ev = stripped[:200]
                    # Documentation describes a mutation; it does not perform
                    # one. Record it so a runbook procedure is never invisible,
                    # but do not let prose block the cutover gate.
                    severity = rule.severity
                    if is_doc and severity == "mutator":
                        severity = "context"
                    out.append(
                        Finding(
                            id=stable_id(name, rel, rule.name, ev),
                            detector=rule.name,
                            kind=rule.kind,
                            severity=severity,
                            repo=name,
                            path=rel,
                            line=lineno,
                            evidence=ev,
                            why=rule.why,
                        )
                    )
    return out


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


VALID_DISPOSITIONS = {"preserve", "replace", "retire"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--dispositions", type=Path)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--md-out", type=Path)
    ap.add_argument(
        "--require-classified",
        action="store_true",
        help="Exit non-zero if any mutator finding has no disposition.",
    )
    args = ap.parse_args()

    cfg = load_json(args.config)
    dispositions = load_json(args.dispositions) if args.dispositions else {}
    dmap = dispositions.get("dispositions", {})

    findings: list[Finding] = []
    scanned = []
    for repo in cfg.get("repos", []):
        root = Path(repo["root"])
        if not root.exists():
            print(f"ERROR: scan root does not exist: {root}", file=sys.stderr)
            return 2
        found = scan_repo(repo["name"], root, repo.get("exclude", []))
        findings.extend(found)
        scanned.append({"name": repo["name"], "root": str(root), "findings": len(found)})

    findings.sort(key=lambda f: (f.repo, f.path, f.line, f.detector))

    mutators = [f for f in findings if f.severity == "mutator"]

    # The unit of disposition is the ENTRY POINT (one file), not the line.
    # A file is preserved, replaced, or retired as a whole; asking an operator
    # to classify 479 individual lines produces a gate nobody completes.
    # Line findings remain attached as the evidence for each entry point.
    entries: dict[str, dict] = {}
    for f in mutators:
        key = f"{f.repo}/{f.path}"
        e = entries.setdefault(key, {
            "id": hashlib.sha256(key.encode()).hexdigest()[:16],
            "repo": f.repo,
            "path": f.path,
            "detectors": set(),
            "mutator_lines": 0,
            "first_line": f.line,
        })
        e["detectors"].add(f.detector)
        e["mutator_lines"] += 1
        e["first_line"] = min(e["first_line"], f.line)

    entry_points = []
    for e in sorted(entries.values(), key=lambda x: (x["repo"], x["path"])):
        d = dmap.get(e["id"], {})
        entry_points.append({
            "id": e["id"],
            "repo": e["repo"],
            "path": e["path"],
            "detectors": sorted(e["detectors"]),
            "mutator_lines": e["mutator_lines"],
            "first_line": e["first_line"],
            "disposition": d.get("disposition"),
            "owner": d.get("owner"),
            "note": d.get("note"),
        })

    unclassified = [
        e for e in entry_points
        if e["disposition"] not in VALID_DISPOSITIONS
    ]

    # Dispositions naming an id that no longer exists: the entry point was
    # renamed or removed. Surfacing these is how a retirement gets proven
    # complete rather than assumed.
    live_ids = {e["id"] for e in entry_points}
    stale = sorted(set(dmap) - live_ids)

    report = {
        "schema": SCHEMA,
        "scanned": scanned,
        "totals": {
            "findings": len(findings),
            "mutator_lines": len(mutators),
            "entry_points": len(entry_points),
            "classified": len(entry_points) - len(unclassified),
            "unclassified": len(unclassified),
            "stale_dispositions": len(stale),
        },
        "by_detector": {
            r.name: sum(1 for f in findings if f.detector == r.name) for r in RULES
        },
        "entry_points": entry_points,
        "findings": [asdict(f) for f in findings],
        "unclassified_ids": [e["id"] for e in unclassified],
        "stale_disposition_ids": stale,
    }

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_md(report), encoding="utf-8")

    t = report["totals"]
    print(f"findings={t['findings']} mutator_lines={t['mutator_lines']} "
          f"entry_points={t['entry_points']} unclassified={t['unclassified']} "
          f"stale={t['stale_dispositions']}")
    for row in scanned:
        print(f"  {row['name']:<16} {row['findings']:>5}  {row['root']}")

    if args.require_classified and unclassified:
        print(f"\nCENSUS FAILED: {len(unclassified)} mutator findings have no "
              f"disposition.", file=sys.stderr)
        for f in unclassified[:20]:
            print(f"  {f.id}  {f.repo}/{f.path}:{f.line}  [{f.detector}]",
                  file=sys.stderr)
        if len(unclassified) > 20:
            print(f"  ... and {len(unclassified) - 20} more", file=sys.stderr)
        return 1
    return 0


def render_md(report: dict) -> str:
    t = report["totals"]
    lines = [
        "# Menhir production mutation census",
        "",
        f"Schema `{report['schema']}`. Generated by `pipeline/census.py` from named",
        "detection rules. Do not hand-edit: re-run the tool.",
        "",
        "## Totals",
        "",
        f"- findings: **{t['findings']}**",
        f"- mutator lines: **{t['mutator_lines']}**",
        f"- entry points (must each carry a disposition): **{t['entry_points']}**",
        f"- classified: **{t['classified']}**",
        f"- unclassified: **{t['unclassified']}**",
        f"- stale dispositions (id no longer present): **{t['stale_dispositions']}**",
        "",
        "## Scan roots",
        "",
        "| Repository | Findings | Root |",
        "|---|---:|---|",
    ]
    for row in report["scanned"]:
        lines.append(f"| `{row['name']}` | {row['findings']} | `{row['root']}` |")

    lines += ["", "## Findings by detector", "", "| Detector | Count |", "|---|---:|"]
    for name, count in sorted(report["by_detector"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{name}` | {count} |")

    lines += ["", "## Entry points", "",
              "One file is one unit of disposition: it is preserved, replaced, or",
              "retired as a whole. Line-level findings are the evidence and live in",
              "`census.report.json`.", "",
              "| ID | Repo | Path | Lines | Detectors | Disposition | Owner |",
              "|---|---|---|---:|---|---|---|"]
    for e in report["entry_points"]:
        disp = e["disposition"] or "**UNCLASSIFIED**"
        lines.append(
            f"| `{e['id']}` | `{e['repo']}` | `{e['path']}` | {e['mutator_lines']} "
            f"| {', '.join(e['detectors'])} | {disp} | {e['owner'] or '-'} |"
        )

    if report["stale_disposition_ids"]:
        lines += ["", "## Stale dispositions", "",
                  "These ids carry a disposition but no longer appear in the scan.",
                  "For a `retire` disposition that is the proof of retirement; for",
                  "any other it means the entry point moved and must be re-judged.", ""]
        for sid in report["stale_disposition_ids"]:
            lines.append(f"- `{sid}`")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
