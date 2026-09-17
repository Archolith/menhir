"""P3 gate: does a snapshot of a REAL repository scan the same as the repository?

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3).
Gate wording: "structural parity is explained for every fixture".

    python scripts/probe/p3_parity_report.py <repo> [<repo> ...]

**"Explained", not "identical".** The bundler is supposed to drop things: untracked files, excluded
directories, oversized files, anything the policy refuses. So a local scan legitimately sees files
the extraction does not, and a run where the two fingerprints matched would mean the policy had
stopped working. The question this answers is the useful one:

* **Explained**: present locally, absent remotely, and absent from the bundle manifest. The policy
  chose to drop it, which is the system working.
* **UNEXPLAINED**: in the manifest but missing from the extraction -- a bundle, transport or
  extraction bug -- or present remotely and not locally, which should be impossible.

Only the second kind is a finding. Counting them together, which is what a fingerprint comparison
alone does, would report a hundred intentional omissions as failures and teach a reader to ignore
the result.

Nothing is written into the repositories being scanned: they are read, bundled in memory, and
extracted into a temporary root that is deleted afterwards.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import tempfile
import time
from pathlib import Path

from menhir.snapshot.archive_plan import plan_archive
from menhir.snapshot.bundler import build_plan, write_bundle
from menhir.snapshot.extraction_lease import LeaseStore
from menhir.snapshot.extraction_writer import materialize
from menhir.snapshot.protocol import CONTENT_PREFIX
from menhir.snapshot.shadow_scan import fingerprint_scan


def _scan(root: Path):
    from menhir.infrastructure.project_scanner import ProjectScanner

    return ProjectScanner().scan(root)


def _files_of(scan) -> set[str]:
    return {f.rel_path.replace("\\", "/") for f in scan.files}


def compare(repo: Path) -> dict:
    """Bundle, extract and scan one repository at both ends."""
    started = time.perf_counter()
    plan = build_plan(repo)
    if plan.blocked:
        return {
            "repo": repo.name,
            "skipped": "the selection policy refused a path (secret-risk); "
            f"{len(plan.refusals)} refusal(s)",
        }

    buffer = io.BytesIO()
    write_bundle(plan, buffer)
    blob = buffer.getvalue()

    manifest_paths = {f.path for f in plan.manifest.files}

    work = Path(tempfile.mkdtemp(prefix="p3-parity-"))
    try:
        leases = LeaseStore(work / "leases", ttl_s=1800.0)
        lease = leases.acquire(
            project_key=repo.name, upload_id="parity", owner="parity-probe"
        )
        root = work / "root"
        materialize(blob, plan_archive(blob), root, lease=lease, leases=leases)

        # The bundle nests files under `content/` and keeps the manifest beside them, so the
        # project root inside an extraction is one level down. Scanning the extraction root itself
        # was this probe's first mistake and it looked exactly like total data loss: every path
        # reported both "missing after extraction" and "present remotely but not locally", 3,147
        # of them, because the two sides were comparing `x` against `content/x`.
        local = _scan(repo)
        remote = _scan(root / CONTENT_PREFIX.rstrip("/"))

        local_files = _files_of(local)
        remote_files = _files_of(remote)

        only_local = local_files - remote_files
        only_remote = remote_files - local_files

        # A local file the bundle never carried is the policy working. One the bundle DID carry
        # and the extraction lost is a real defect.
        explained = {p for p in only_local if p not in manifest_paths}
        unexplained_missing = only_local - explained

        return {
            "repo": repo.name,
            "seconds": round(time.perf_counter() - started, 1),
            "archive_bytes": len(blob),
            "manifest_files": len(manifest_paths),
            "local_files": len(local_files),
            "remote_files": len(remote_files),
            "local_symbols": len(local.symbols),
            "remote_symbols": len(remote.symbols),
            "explained_omissions": len(explained),
            "unexplained_missing": sorted(unexplained_missing)[:10],
            "unexplained_missing_count": len(unexplained_missing),
            "only_remote": sorted(only_remote)[:10],
            "only_remote_count": len(only_remote),
            "fingerprints_match": fingerprint_scan(local) == fingerprint_scan(remote),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repos", nargs="+", type=Path)
    args = parser.parse_args(argv)

    findings = 0
    for repo in args.repos:
        if not (repo / ".git").exists():
            print(f"  {repo.name:24s} SKIP (not a git repository)")
            continue
        try:
            result = compare(repo)
        except Exception as exc:  # noqa: BLE001 -- one bad repo must not end the run
            print(f"  {repo.name:24s} ERROR {type(exc).__name__}: {str(exc)[:120]}")
            findings += 1
            continue

        if "skipped" in result:
            print(f"  {result['repo']:24s} SKIP ({result['skipped']})")
            continue

        bad = result["unexplained_missing_count"] + result["only_remote_count"]
        findings += bad
        status = "OK " if bad == 0 else "!! "
        print(
            f"  {status}{result['repo']:22s} "
            f"files {result['local_files']:5d} local / {result['remote_files']:5d} remote  "
            f"symbols {result['local_symbols']:6d}/{result['remote_symbols']:6d}  "
            f"explained {result['explained_omissions']:5d}  "
            f"unexplained {bad:3d}  "
            f"{result['seconds']:5.1f}s"
        )
        if result["unexplained_missing"]:
            print(
                f"      in the manifest but missing after extraction: {result['unexplained_missing']}"
            )
        if result["only_remote"]:
            print(f"      present remotely but not locally: {result['only_remote']}")

    print()
    if findings == 0:
        print(
            "  Every difference is explained by the selection policy. No unexplained loss."
        )
    else:
        print(f"  {findings} unexplained difference(s) -- see above.")
    return 0 if findings == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
