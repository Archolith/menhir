"""Project-metadata readers for the structure graph: coverage, fingerprints, guards."""

from __future__ import annotations

from typing import Any

def _normalize_structure_path(path: str) -> str:
    """Normalize a caller-supplied path to the stored `structure_path` spelling.

    Stored paths are forward-slashed and repo-relative with no leading `./`. Callers pass
    paths through verbatim, so without this a Windows-style or dot-prefixed path silently
    fails to match an indexed file.
    """
    p = str(path).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/").rstrip("/")


class StructureGraphMetadataMixin:
    """Mixin part of ``StructureGraphWriter`` carrying per-project metadata reads."""

    def get_project_coverage(self, project_name: str) -> dict[str, Any]:
        """Index-coverage state for a project, used to qualify negative answers.

        A structural query that finds nothing has two very different meanings: the thing
        genuinely has no dependents, or it was never indexed. Callers need this to tell them
        apart -- reporting absence as fact is how a truncated index produces a false all-clear.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $p, structure_role: 'project'})
            RETURN n.files_discovered AS discovered,
                   n.files_eligible   AS eligible,
                   n.files_indexed    AS indexed,
                   n.partial_index    AS partial
            """,
            {"p": project_name},
        )
        if not rows:
            # Project absent, or scanned before coverage was recorded. Unknown, not complete.
            return {"known": False, "partial_index": False}
        r = rows[0]
        return {
            "known": r.get("indexed") is not None,
            "files_discovered": r.get("discovered"),
            "files_eligible": r.get("eligible"),
            "files_indexed": r.get("indexed"),
            "partial_index": bool(r.get("partial")),
        }

    def which_paths_indexed(self, project: str, file_paths: list[str]) -> set[str]:
        """Subset of *file_paths* that exist as indexed entities for *project*.

        Matching is on the caller's ORIGINAL string, but each path is also probed in its
        normalized form. `structure_path` is stored forward-slashed and repo-relative, while
        callers pass paths through verbatim -- a Windows `src\\a.py` or a `./src/a.py` would
        otherwise miss and be reported as un-indexed, producing a false refusal for a file
        that is present.
        """
        if not file_paths:
            return set()
        variants: dict[str, str] = {}
        for original in file_paths:
            variants.setdefault(_normalize_structure_path(original), original)
        rows = self.neo4j.execute(
            """
            UNWIND $paths AS p
            MATCH (n:Entity {structure_project: $proj, structure_path: p})
            RETURN DISTINCT n.structure_path AS path
            """,
            {"proj": project, "paths": list(variants)},
        )
        # Map hits back to the caller's spelling so the caller can compare against its input.
        return {
            variants[str(r["path"])]
            for r in rows
            if str(r.get("path") or "") in variants
        }

    def get_scan_fingerprint(self, project_name: str) -> str | None:
        """Read stored fingerprint for a project entity."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $name, structure_role: 'project'})
            RETURN n.scan_fingerprint AS fp
            """,
            {"name": project_name},
        )
        if rows and rows[0].get("fp"):
            return str(rows[0]["fp"])
        return None

    def get_project_root_path(self, project_name: str) -> str | None:
        """Read the directory a project was last scanned from, or None if unknown.

        CF-257 phase 0. This is the only witness that a project has already been claimed by a
        directory, and it is what catches a FORK -- an independent clone passes every git check,
        so filesystem shape alone cannot refuse it.

        Returns None both for "no such project" and for a project entity that exists only as the
        MERGE target of a cross-project reference (measured: 2 of 63 carry no root_path). Neither
        is a claim, so neither should refuse a scan.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $name, structure_role: 'project'})
            RETURN n.root_path AS root_path
            """,
            {"name": project_name},
        )
        if rows and rows[0].get("root_path"):
            return str(rows[0]["root_path"])
        return None

    def get_beacon_evidence_guard(self, project_name: str) -> dict[str, Any]:
        """Return the project facts and writer revision that fence one evidence read.

        Every production structure writer registers on ``ProjectIdentity.active_writers`` and
        stamps ``last_structure_writer_id`` when it releases. Reading this row before and after
        Beacon's independent graph queries therefore detects a writer active at either boundary
        or one that completed entirely between them.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $name, structure_role: 'project'})
            OPTIONAL MATCH (p:ProjectIdentity {project_id: n.structure_project_id})
            RETURN n.root_path AS root_path,
                   n.scan_fingerprint AS scan_fingerprint,
                   n.files_discovered AS files_discovered,
                   n.files_eligible AS files_eligible,
                   n.files_indexed AS files_indexed,
                   n.partial_index AS partial_index,
                   n.structure_project_id AS project_id,
                   p IS NOT NULL AS identity_known,
                   coalesce(p.active_writers, []) AS active_writers,
                   coalesce(p.last_structure_writer_id, '') AS writer_revision
            LIMIT 1
            """,
            {"name": project_name},
        )
        if not rows:
            return {"project_known": False}
        row = rows[0]
        return {
            "project_known": True,
            "root_path": str(row.get("root_path") or ""),
            "scan_fingerprint": str(row.get("scan_fingerprint") or ""),
            "files_discovered": row.get("files_discovered"),
            "files_eligible": row.get("files_eligible"),
            "files_indexed": row.get("files_indexed"),
            "partial_index": bool(row.get("partial_index")),
            "project_id": str(row.get("project_id") or ""),
            "identity_known": bool(row.get("identity_known")),
            "active_writers": tuple(str(item) for item in row.get("active_writers") or []),
            "writer_revision": str(row.get("writer_revision") or ""),
        }
