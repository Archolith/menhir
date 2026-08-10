"""Migrate the existing artifact corpus into :WorkArtifact nodes.

Dry run by default; ``--apply`` writes. Ordered so nothing is hidden before it
is representable -- the Phase A discipline, where filtering before backfilling
would have hidden 41 open todos.

What this migration deliberately does NOT do:

* **No relationships.** There is no declared source for them and inferring them
  from prose is the rejected path. They accrue as authors declare them. A sparse
  real graph beats a fabricated dense one.
* **No code locations by default.** Backticked paths in prose are free to write
  and therefore numerous; extracting all of them yields volume rather than
  signal. ``--with-locations`` measures the rate without committing to it.
* **No guessing.** An unrecognized ``Status:`` header lands the artifact in its
  type's initial state and is reported as unmapped, rather than being coerced
  into a state the document never claimed.

Identity is minted fresh. Paths are recorded in ArtifactSource, never as
identity -- archive/restore moves files, and a path-keyed identity would orphan
every relationship on the first archive.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from menhir.config.settings_model import MemorySettings
from menhir.domain.work_artifact import (
    ArtifactMedium,
    ArtifactSourceSpec,
    ArtifactType,
    INITIAL_STATUS,
    status_from_header,
)
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

MENHIR_ROOT = Path(__file__).resolve().parents[1]
# Second artifact tree outside the repo, if the operator keeps one. Defaults to the repo
# itself so a plain clone scans only what it ships; set WORKSPACE_ROOT to point elsewhere.
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or MENHIR_ROOT)

#: Directory conventions, not prose inference: the folder an author filed a
#: document in is itself a declaration of what kind of document it is.
DIR_TYPES: dict[str, str] = {
    "plans": ArtifactType.PLAN,
    "reviews": ArtifactType.REVIEW,
    "for-review": ArtifactType.IMPLEMENTATION_REPORT,
    "handoffs": ArtifactType.HANDOFF,
}

#: Matches `Status: X`, `**Status**: X`, `- Status: X` and the bolded variants
#: the corpus actually uses. Anything else is reported unmapped.
STATUS_RE = re.compile(r"^\s*[-*]?\s*\*{0,2}status\*{0,2}\s*:\s*(.+?)\s*$", re.IGNORECASE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$")
CODE_REF_RE = re.compile(r"`([^`\s]+/[^`\s]+\.[A-Za-z0-9]{1,5})`")


def _git_sha(repo: Path, rel_path: str) -> str | None:
    """The revision last observed at this locator -- ONE value, not a history."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H", "--", rel_path],
            capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _scan(path: Path) -> tuple[str | None, str | None, list[str]]:
    """Return (h1_title, raw_status_header, code_ref_tokens)."""
    title: str | None = None
    status: str | None = None
    refs: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None, []

    for line in lines[:40]:
        if title is None:
            m = H1_RE.match(line)
            if m:
                title = m.group(1).strip()
                continue
        if status is None:
            m = STATUS_RE.match(line)
            if m:
                status = m.group(1).strip()
    for line in lines:
        refs.extend(CODE_REF_RE.findall(line))
    return title, status, refs


def _collect(root: Path, namespace: str) -> list[dict]:
    items: list[dict] = []
    for dirname, artifact_type in DIR_TYPES.items():
        directory = root / ".agent" / dirname
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            rel = path.relative_to(root).as_posix()
            title, raw_status, refs = _scan(path)
            mapped, reason = status_from_header(raw_status, artifact_type)
            items.append({
                "path": path,
                "rel": rel,
                "root": root,
                "namespace": namespace,
                "artifact_type": artifact_type,
                "title": title or path.stem,
                "raw_status": raw_status,
                "status": mapped or INITIAL_STATUS[artifact_type],
                "status_mapped": mapped is not None,
                "unresolved_reason": reason,
                "code_refs": refs,
            })
    return items


def _connect() -> WorkArtifactRepository:
    load_dotenv()
    s = MemorySettings.from_env()
    return WorkArtifactRepository(Neo4jRepository(
        uri=s.neo4j_uri, user=s.neo4j_user, password=s.neo4j_password, database=s.neo4j_database
    ))


def _revalidate() -> int:
    """Re-read every ingested artifact and store a fresh shape verdict.

    This is the 'update' half: a verdict describes the document as it is now,
    so it has to be recomputed rather than inherited from ingest time.
    """
    repo = _connect()
    rows = repo.neo4j.execute(
        "MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource) "
        "RETURN a.artifact_uuid AS uuid, a.artifact_type AS t, "
        "       s.locator_repository AS repo, s.locator_path AS path",
        {},
    )

    roots = {"menhir": MENHIR_ROOT, "IdeaProjects": WORKSPACE_ROOT}
    tally: Counter = Counter()
    violations: Counter = Counter()
    missing = 0

    for row in rows:
        root = roots.get(row["repo"])
        if root is None or not row["path"]:
            missing += 1
            continue
        path = root / row["path"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # The file is gone; that is a real finding, not a crash.
            text = None
            missing += 1
        report = repo.record_shape(row["uuid"], row["t"], text)
        tally[report.status] += 1
        for finding in report.required_failures:
            violations[finding.code] += 1

    print(f"revalidated       : {len(rows)} artifacts")
    print(f"  status          : {dict(tally)}")
    print(f"  unreadable      : {missing}")
    print("  top violations  :")
    for code, count in violations.most_common(10):
        print(f"    {count:>4}  {code}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--with-locations", action="store_true",
                    help="also normalize backticked code refs into ArtifactLocation")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-run shape validation over already-ingested artifacts")
    args = ap.parse_args(argv)

    if args.revalidate:
        return _revalidate()

    items = _collect(MENHIR_ROOT, "menhir") + _collect(WORKSPACE_ROOT, "workspace")

    by_type = Counter(i["artifact_type"] for i in items)
    by_status = Counter(i["status"] for i in items)
    unmapped = [i for i in items if not i["status_mapped"]]
    reasons = Counter(i["unresolved_reason"] for i in unmapped)
    ref_total = sum(len(i["code_refs"]) for i in items)

    print(f"corpus            : {len(items)} artifacts")
    print(f"  by namespace    : {dict(Counter(i['namespace'] for i in items))}")
    print(f"  by type         : {dict(by_type)}")
    print(f"  status mapped   : {len(items) - len(unmapped)}/{len(items)}")
    print(f"  unmapped reasons: {dict(reasons)}")
    print(f"  status spread   : {dict(by_status)}")
    print(f"  code refs seen  : {ref_total} (locations {'ON' if args.with_locations else 'OFF'})")

    # The leading token of every header we could not map. This is the corpus's
    # actual vocabulary; extending STATUS_HEADER_ALIASES is a decision for a
    # human to make from this list, not something the migration should guess.
    vocab = Counter(
        (i["raw_status"] or "").strip().strip("*").split()[0].lower().strip("*")
        for i in unmapped
        if i["raw_status"] and i["raw_status"].split()
    )
    print("\nunrecognized leading tokens (candidates for STATUS_HEADER_ALIASES):")
    for token, count in vocab.most_common(12):
        print(f"  {count:>3}  {token}")

    print("\nunmapped sample (lands in the type's initial state, reported not guessed):")
    for i in unmapped[:10]:
        raw = (i["raw_status"] or "")[:38]
        print(f"  {i['unresolved_reason']:<22} {raw:<40} {i['rel']}")

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0

    load_dotenv()
    s = MemorySettings.from_env()
    n = Neo4jRepository(
        uri=s.neo4j_uri, user=s.neo4j_user, password=s.neo4j_password, database=s.neo4j_database
    )
    repo = WorkArtifactRepository(n)

    existing = {
        r["path"]
        for r in n.execute(
            "MATCH (:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource) "
            "WHERE s.locator_path IS NOT NULL RETURN s.locator_path AS path",
            {},
        )
    }

    created = skipped = 0
    for i in items:
        if i["rel"] in existing:
            skipped += 1
            continue
        source = ArtifactSourceSpec(
            medium=ArtifactMedium.MARKDOWN,
            locator={"repository": i["root"].name, "path": i["rel"]},
            version=_git_sha(i["root"], i["rel"]),
        )
        repo.create_artifact(
            artifact_type=i["artifact_type"],
            title=i["title"],
            source=source,
            namespace=i["namespace"],
            status=i["status"],
            status_raw=i["raw_status"],
            status_unresolved_reason=i["unresolved_reason"],
            code_refs=", ".join(i["code_refs"]) if args.with_locations else None,
            structure_project=i["root"].name,
        )
        created += 1

    print(f"\napplied: {created} created, {skipped} already present (idempotent by locator path)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
