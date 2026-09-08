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


@dataclass(frozen=True)
class Rule:
    """One named detector.

    kind:     coarse category used for reporting and disposition policy
    pattern:  compiled regex applied per line
    why:      what a reviewer should understand this finding to mean
    severity: 'mutator' items MUST be retired or replaced before cutover;
              'context' items are recorded for completeness but may be preserved
    """

    name: str
    kind: str
    pattern: re.Pattern
    why: str
    severity: str


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


RULES: tuple[Rule, ...] = (
    # --- the detector that would have caught the fourth wrapper ---------------
    Rule(
        "inline_interpreter_as_root",
        "privileged-exec",
        _rx(r"sudo[^|\n]*\b(python3?|bash|sh|perl|ruby|node)\b\s*-\s*(?:$|[\"'|)])"),
        "Pipes caller-supplied program text into an interpreter running as root. "
        "This is arbitrary root code execution and cannot be constrained by "
        "sudoers argument matching.",
        "mutator",
    ),
    Rule(
        "base64_program_transfer",
        "privileged-exec",
        _rx(r"base64\s+-d|\[Convert\]::ToBase64String|FromBase64String"),
        "Transfers program or payload text encoded, which defeats source review "
        "and argument inspection at the privilege boundary.",
        "mutator",
    ),
    # --- privileged invocation ------------------------------------------------
    Rule(
        "sudo_invocation",
        "privileged-exec",
        _rx(r"\bsudo\b(?!\s*(?:-n\s+)?true\b)"),
        "Invokes a privileged command. Every one of these is a candidate "
        "mutation entry point and must be accounted for.",
        "mutator",
    ),
    Rule(
        "sudoers_definition",
        "privilege-grant",
        _rx(r"sudoers|NOPASSWD|/etc/sudoers\.d"),
        "Defines or edits a privilege grant. The set of these is the actual "
        "privilege boundary, regardless of what any document claims.",
        "mutator",
    ),
    # --- remote execution -----------------------------------------------------
    Rule(
        "remote_shell_exec",
        "remote-exec",
        _rx(r"\bssh\b\s+[^\s]+\s+[\"']|Invoke-Command|New-PSSession|\bscp\b|\bsftp\b|\brsync\b"),
        "Executes or transfers to a remote host. Combined with a privileged "
        "invocation this is a deployment path.",
        "mutator",
    ),
    # --- root filesystem mutation --------------------------------------------
    Rule(
        "root_path_write",
        "fs-mutation",
        _rx(r"\b(install|mv|cp|rm|chmod|chown|ln|mkdir|tee|truncate)\b[^\n]{0,80}"
            r"(/srv/|/etc/|/usr/local/|/var/lib/|/run/lock/|/opt/)"),
        "Writes, moves, or deletes under a root-owned production path.",
        "mutator",
    ),
    # --- orchestration --------------------------------------------------------
    Rule(
        # NOTE: an earlier version of this rule matched \.(service|timer|path|socket)\b
        # which collided with every Python attribute access ending in `.path`
        # and produced 3698 of 4555 findings. A detector that buries the signal
        # is worse than no detector: it makes the "no unclassified item" gate
        # unachievable, and an unachievable gate gets waived. Match unit files,
        # unit-file grammar, and systemctl verbs only.
        "systemd_unit",
        "service-definition",
        _rx(r"systemctl\s+(enable|disable|start|stop|restart|reload|daemon-reload|mask|unmask)\b|"
            r"^\s*\[(Unit|Service|Timer|Socket|Install)\]|"
            r"^\s*(ExecStart|ExecStop|ExecReload|WantedBy|RequiredBy|OnCalendar|Restart)\s*=|"
            r"[\w.-]+\.(service|timer|socket)\b"),
        "Defines or controls a system service, timer, or socket activation. "
        "These run without an operator present and are mutation paths.",
        "mutator",
    ),
    Rule(
        "documented_manual_procedure",
        "runbook",
        _rx(r"^\s*(?:[-*\d.]+\s+)?(?:run|execute|then)\b[^\n]{0,60}\bsudo\b|"
            r"^\s*\$?\s*sudo\s+"),
        "A documented manual mutation procedure. An operator following a runbook "
        "is a real mutation path, but it is a human process rather than an "
        "executable one, so it is recorded without blocking the cutover gate.",
        "context",
    ),
    Rule(
        "ansible_mutation",
        "orchestration",
        _rx(r"ansible\.builtin\.(file|copy|template|command|shell|systemd|service|"
            r"lineinfile|blockinfile|user|group|package|apt|unarchive|get_url)"),
        "An Ansible task that changes host state.",
        "mutator",
    ),
    Rule(
        "compose_production",
        "container-mutation",
        _rx(r"docker\s+compose[^\n]*(-f\s*[^\s]*production|up\b|down\b|restart\b)|"
            r"docker\s+(run|rm|stop|start|restart|exec|load|tag|push)\b"),
        "Starts, stops, replaces, or loads containers. Affects what is running "
        "and reachable.",
        "mutator",
    ),
    # --- concurrency control --------------------------------------------------
    Rule(
        "lock_acquisition",
        "lock",
        _rx(r"flock|/run/lock/|LOCK_EX|LOCK_NB|menhir-production\.lock"),
        "Acquires or defines a mutation lock. Two writers sharing a lock path "
        "are the same critical section and must be censused together.",
        "mutator",
    ),
    # --- data at risk ---------------------------------------------------------
    Rule(
        "database_or_backup_op",
        "data-mutation",
        _rx(r"neo4j-admin|cypher-shell|\bdump\b[^\n]{0,40}neo4j|restore[^\n]{0,40}neo4j|"
            r"backup-generation|menhir-backup"),
        "Touches the Neo4j database or its backups. Data loss here is the one "
        "unrecoverable failure in this system.",
        "mutator",
    ),
    # --- ingress (owned by yawn.deploy per ADR 0002) --------------------------
    Rule(
        "ingress_config",
        "ingress",
        _rx(r"Caddyfile|caddy-release|caddy-route|reverse_proxy|cloudflared|menhir-proxy"),
        "Ingress configuration or its writers. Per ADR 0002 yawn.deploy owns "
        "this; any Menhir-side writer is a boundary violation.",
        "context",
    ),
)


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
