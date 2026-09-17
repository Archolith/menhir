"""Scan an extracted snapshot, report what is there, and delete it.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3).
Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md` (decision 3).

Every other part of P3 refuses things. This is the only one that produces a RESULT, and its
correctness question is different: not "was the attack stopped" but "does scanning a snapshot
remotely give the same answer as scanning the repository locally".

**It calls the existing `ProjectScanner`; it does not reimplement one.** Parity is then true by
construction, and a difference that survives is a real one -- something the bundle dropped or the
extraction changed -- rather than a disagreement between two scanners. `project_scanner` is READ
ONLY to this module: nothing here modifies it, wraps it in a way that changes local behaviour, or
asks it to behave differently when the caller is a snapshot.

**The fingerprint covers structure and deliberately excludes four things** that differ between a
repository and an extraction of it for reasons that are not about the code:

* `root_path` -- absolute, and the extraction root is somewhere else by definition.
* `file_mtime` -- extraction writes new files, so every mtime differs. It exists for incremental
  diffing, not for identity.
* `name` -- derived from the directory basename, and the extraction root is named for an upload.
* `project_id` / `identity_generation` / `scan_fingerprint` -- identity and the scanner's own
  stamp. P3 adopts no identity, and fingerprinting a fingerprint is circular.

Including any of those would make every parity check fail for a reason that has nothing to do with
the snapshot, which is worse than not checking: it trains a reader to ignore the result.

**Counts are reported; `partial_index` is not asserted.** `ProjectScanResult` derives it from three
counts and the plan's invariant 4 keeps that derivation on the side that can see the whole picture.
The report carries the counts -- the source of truth -- so a reader can derive it knowing what it
means.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["ShadowReport", "fingerprint_scan", "shadow_scan"]

#: Domain separator, so a structure fingerprint can never collide with any other sha256 this
#: system compares -- the same reason `compute_tree_digest` has one.
_FINGERPRINT_DOMAIN = b"menhir-shadow-scan-v1\n"


@dataclass(frozen=True)
class ShadowReport:
    """What a snapshot contained. Carries no content and no absolute paths."""

    fingerprint: str
    file_count: int
    directory_count: int
    symbol_count: int
    import_count: int
    endpoint_count: int
    #: The three coverage counts, verbatim from the scan. `partial_index` is derived from these by
    #: whoever reads them; see the module docstring.
    files_discovered: int
    files_eligible: int
    files_indexed: int


def _structure_of(scan: Any) -> dict[str, Any]:
    """The structural projection a fingerprint is computed over.

    Every list is sorted. The scanner's traversal order is a filesystem fact -- `os.scandir` makes
    no ordering promise and differs between filesystems -- so an unsorted fingerprint would differ
    between two scans of identical content on two machines.
    """
    return {
        "stack": scan.stack,
        "description": scan.description,
        "dependencies": sorted(scan.dependencies),
        "directories": sorted(d.rel_path for d in scan.directories),
        # rel_path and role only: `file_mtime` is excluded on purpose, and `description` is
        # inferred from the path rather than read from content, so it adds nothing here.
        "files": sorted([f.rel_path, f.role] for f in scan.files),
        "imports": sorted([i.source_path, i.target_path] for i in scan.imports),
        "test_edges": sorted([t.test_path, t.source_path] for t in scan.test_edges),
        "endpoints": sorted([e.name, e.file_path, e.kind] for e in scan.endpoints),
        "symbols": sorted(
            [s.file_path, s.name, s.kind, s.signature, s.parent, s.decorator]
            for s in scan.symbols
        ),
        "call_edges": sorted([c.caller_path, c.callee_path] for c in scan.call_edges),
        "nested_repos": sorted([n.rel_path, n.name] for n in scan.nested_repos),
        "truncated_symbol_files": sorted(scan.truncated_symbol_files),
    }


def fingerprint_scan(scan: Any) -> str:
    """A stable digest of a scan's structure.

    Stable means: two scans of the same content, on different machines, at different times, under
    different absolute paths, produce the same string. That is the only property that makes a
    parity check meaningful, and it is why the exclusions in the module docstring are not
    incidental tidying.
    """
    payload = json.dumps(
        _structure_of(scan), sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_DOMAIN)
    digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def shadow_scan(
    root: Path, *, scanner: Any = None, delete_root: bool = True
) -> ShadowReport:
    """Scan `root`, report it, and delete it.

    Deletion is the default and happens even when the scan raises. `shadow` mode exists to answer
    a question without keeping the answer's materials: a root that outlives its report is an
    unattributed copy of someone's repository sitting on a disk.
    """
    from menhir.infrastructure.project_scanner import ProjectScanner

    root = Path(root)
    try:
        scan = (scanner or ProjectScanner()).scan(root)
        return ShadowReport(
            fingerprint=fingerprint_scan(scan),
            file_count=len(scan.files),
            directory_count=len(scan.directories),
            symbol_count=len(scan.symbols),
            import_count=len(scan.imports),
            endpoint_count=len(scan.endpoints),
            files_discovered=scan.files_discovered,
            files_eligible=scan.files_eligible,
            files_indexed=scan.files_indexed,
        )
    finally:
        if delete_root:
            shutil.rmtree(root, ignore_errors=True)
