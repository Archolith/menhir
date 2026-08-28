"""Cypher writer for structural project entities and edges.

Writes deterministic structural data directly to Neo4j — not through
Graphiti — so the project skeleton is always complete and reliable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.todo_location import (
    DEFAULT_TODO_NAMESPACE as _DEFAULT_TODO_NAMESPACE,
)
from menhir.domain.truth.kinds import SOURCE_CONFIDENCE_AGENT
from menhir.domain.utils import source_confidence_for, symbol_structure_path
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.project_scanner import ProjectScanResult, SymbolEntry

logger = logging.getLogger(__name__)


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


#: The source label every node written by the project scanner carries.
STRUCTURE_SOURCE = "project-scan"

#: Derived, never restated. This module used to name SOURCE_CONFIDENCE_STRUCTURAL directly, which is
#: how it silently drifted from `source_confidence_for` -- both were right in isolation and disagreed
#: with each other for 48,781 production entities. Deriving it means the two cannot part again.
STRUCTURE_SOURCE_CONFIDENCE = source_confidence_for(STRUCTURE_SOURCE)

# Frozen namespace for project identities that exist only because another project's scan named
# them. These ids are placeholders until the project is scanned directly and its settled identity
# overwrites the inferred one. A namespace constant (rather than NAMESPACE_URL at each call site)
# makes the allocation contract explicit and prevents the CALLS and CONTAINS_REPO writers from
# drifting into independent identity spaces.
_INFERRED_PROJECT_ID_NAMESPACE = UUID("b872ff47-dd11-5845-9702-6e0eef2730fd")


def _inferred_project_id(project_name: str) -> str:
    """Return the stable placeholder identity shared by every inferred-project writer."""
    return str(uuid5(_INFERRED_PROJECT_ID_NAMESPACE, project_name))


# Both inferred edge writers splice in this exact prefix. New targets MERGE on the live composite
# uniqueness constraint, so concurrent writers all contend for one (project_id, path) key. The
# legacy lookup is a compatibility bridge: once a direct scan has replaced the placeholder id with
# the settled id, later inferred edges must reuse that row rather than recreate the placeholder.
_INFERRED_PROJECT_TARGET_CYPHER = """
            OPTIONAL MATCH (legacy:Entity {
                structure_project: $target_name,
                structure_path: '.',
                structure_role: 'project'
            })
            WITH head(collect(legacy)) AS existing
            CALL {
                WITH existing
                WITH existing WHERE existing IS NOT NULL
                RETURN existing AS target
                UNION
                WITH existing
                WITH existing WHERE existing IS NULL
                MERGE (target:Entity {
                    structure_project_id: $target_project_id,
                    structure_path: '.'
                })
                ON CREATE SET
                    target.uuid = $uuid,
                    target.identity_source = 'inferred',
                    target.content = $target_name,
                    target.type = 'SEMANTIC',
                    target.scope = 'PERSISTENT',
                    target.source = 'project-scan',
                    target.source_confidence = $sc_inferred,
                    target.user_flagged = false,
                    target.group_id = '',
                    target.session_id = $session_id,
                    target.user_id = $user_id,
                    target.created_at = $now,
                    target.last_accessed = $now
                RETURN target
            }
            SET target.structure_project = $target_name,
                target.structure_role = 'project',
                target.name = $target_name,
                target.structure_project_id = coalesce(
                    target.structure_project_id, $target_project_id
                ),
                target.identity_source = coalesce(target.identity_source, 'inferred')
            WITH target
"""

_ENTITY_DEFAULTS: dict[str, Any] = {
    "type": "SEMANTIC",
    "scope": "PERSISTENT",
    "source": STRUCTURE_SOURCE,
    "source_confidence": STRUCTURE_SOURCE_CONFIDENCE,
    "user_flagged": False,
    "group_id": "",
    "summary": "",
}


@dataclass
class StructureGraphWriter:
    neo4j: Neo4jRepository

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_project(
        self,
        scan: ProjectScanResult,
        session_id: str,
        user_id: str,
    ) -> dict[str, int]:
        """MERGE structural entities + edges from a scan result.

        Returns ``{"entities": N, "edges": M}``.
        """
        now = datetime.now(timezone.utc).isoformat()
        entity_count = 0
        edge_count = 0

        # 0. Incremental diff — compare stored file mtimes to determine which
        #    files actually changed so we can skip symbol rewrites for the rest.
        stored_mtimes = self.get_file_mtimes(scan.name)
        scan_mtime_map = {f.rel_path: f.file_mtime for f in scan.files}

        if stored_mtimes:
            changed_paths: set[str] | None = {
                p
                for p, mtime in scan_mtime_map.items()
                if stored_mtimes.get(p) != mtime
            }
            deleted_paths = set(stored_mtimes.keys()) - set(scan_mtime_map.keys())
            logger.debug(
                "Incremental diff: project=%s changed=%s deleted=%s",
                scan.name,
                sorted(changed_paths)[:10],
                sorted(deleted_paths)[:10],
            )
            if deleted_paths:
                self._delete_file_entities(scan.name, list(deleted_paths))
            if changed_paths:
                self._increment_heat(scan.name, list(changed_paths))
        else:
            # First scan — no stored mtimes yet, process everything
            changed_paths = None
            deleted_paths = set()

        # 1. Project entity
        self._merge_entity(
            structure_project=scan.name,
            structure_project_id=getattr(scan, "project_id", None),
            structure_path=".",
            structure_role="project",
            name=scan.name,
            content=scan.description or f"{scan.stack} project",
            session_id=session_id,
            user_id=user_id,
            now=now,
            extra={
                "scan_fingerprint": scan.scan_fingerprint,
                "stack": scan.stack,
                "root_path": scan.root_path,
                # Coverage accounting. `partial_index` is persisted (not just derived) so a
                # reader that only fetches the project node can tell whether a negative
                # structural answer is trustworthy.
                "files_discovered": scan.files_discovered,
                "files_eligible": scan.files_eligible,
                "files_indexed": scan.files_indexed,
                "partial_index": scan.partial_index,
            },
        )
        entity_count += 1

        # 2. Directory entities (batched)
        dir_rows = [
            {
                "uuid": str(uuid4()),
                "structure_project": scan.name,
                "structure_project_id": getattr(scan, "project_id", None),
                "structure_path": d.rel_path,
                "structure_role": "directory",
                "name": d.rel_path.rstrip("/").split("/")[-1],
                "content": d.purpose or f"Directory: {d.rel_path}",
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
            }
            for d in scan.directories
        ]
        entity_count += self._merge_entities_batch(dir_rows)

        # 2b. Prune directories the scan no longer sees. Unlike files this is a full
        #     set-difference, not an mtime diff: directories have no mtime, and a stale one is
        #     identified purely by absence from the current scan.
        stale_dirs = self._delete_stale_directories(
            scan.name, [d.rel_path for d in scan.directories]
        )
        if stale_dirs:
            logger.info(
                "Pruned %d stale directory entities for project=%s",
                stale_dirs,
                scan.name,
            )

        # 3. File entities (batched) — includes file, entrypoint, config, test roles
        file_rows = [
            {
                "uuid": str(uuid4()),
                "structure_project": scan.name,
                "structure_project_id": getattr(scan, "project_id", None),
                "structure_path": f.rel_path,
                "structure_role": f.role,
                "name": f.rel_path.split("/")[-1],
                "content": f.description or f.rel_path,
                "file_mtime": f.file_mtime,
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
            }
            for f in scan.files
        ]
        entity_count += self._merge_entities_batch(file_rows)

        # 3b. Prune file entities the scan no longer sees.
        #
        # The mtime diff above CANNOT do this. `get_file_mtimes` only returns entities with
        # `file_mtime > 0`, so anything written before mtimes existed is absent from
        # `stored_mtimes`, never lands in `deleted_paths`, and survives every future scan.
        # Live proof: workspace-meta carried ~118 `.agent/sandboxes/sbx-test-posix/**`
        # entities for a directory that no longer exists on disk at all.
        #
        # GUARDED ON A NON-TRUNCATED SCAN. Unlike directories, files are subject to
        # `_MAX_KEY_FILES`. When the cap binds, `scan.files` is a TRUNCATED view, and a
        # set-difference against it would delete every eligible file the cap happened to
        # drop -- turning a capacity limit into data loss. Pruning is therefore skipped
        # entirely while `partial_index` is true; a truncated scan is not evidence of
        # absence here either.
        if not scan.partial_index:
            stale_files = self._delete_stale_role_entities_multi(
                scan.name,
                ["file", "entrypoint", "config", "test"],
                [f.rel_path for f in scan.files],
            )
            if stale_files:
                logger.info(
                    "Pruned %d stale file entities for project=%s",
                    stale_files,
                    scan.name,
                )
        elif scan.files:
            logger.warning(
                "Skipping stale-file pruning for project=%s: scan truncated "
                "(%d/%d eligible indexed), so absence from this scan is not evidence the "
                "file is gone.",
                scan.name,
                scan.files_indexed,
                scan.files_eligible,
            )

        # 4. Dependency entities (batched)
        dep_rows = [
            {
                "uuid": str(uuid4()),
                "structure_project": scan.name,
                "structure_project_id": getattr(scan, "project_id", None),
                "structure_path": f"dep:{dep}",
                "structure_role": "dependency",
                "name": dep,
                "content": f"Dependency: {dep}",
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
            }
            for dep in scan.dependencies
        ]
        entity_count += self._merge_entities_batch(dep_rows)

        # 4b. Prune dependencies the scan no longer sees. This role had no prune path at all,
        #     so a removed package kept its entity and its DEPENDS_ON edge forever. Gated on
        #     scan completeness for the same reason as endpoints: manifests are read from the
        #     scanned tree, so a truncated scan can under-report them.
        if not scan.partial_index:
            stale_deps = self._delete_stale_role_entities(
                scan.name, "dependency", [f"dep:{dep}" for dep in scan.dependencies]
            )
            if stale_deps:
                logger.info(
                    "Pruned %d stale dependency entities for project=%s",
                    stale_deps,
                    scan.name,
                )

        # 5. Endpoint entities (batched)
        ep_rows = [
            {
                "uuid": str(uuid4()),
                "structure_project": scan.name,
                "structure_project_id": getattr(scan, "project_id", None),
                "structure_path": f"endpoint:{ep.name}",
                "structure_role": "endpoint",
                "name": ep.name,
                "content": f"{ep.kind}: {ep.name} in {ep.file_path}",
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
            }
            for ep in scan.endpoints
        ]
        entity_count += self._merge_entities_batch(ep_rows)

        # 5b. Prune endpoints the scan no longer sees.
        #
        # GATED ON SCAN COMPLETENESS, NOT ON A NON-EMPTY KEEP-LIST. Endpoints are derived from
        # `scan.files` (see `_detect_endpoints`), so they inherit the file cap and the same rule
        # as files applies: a truncated scan is not evidence of absence. But an EMPTY keep-list
        # on a COMPLETE scan is a real answer -- "this project exposes nothing" -- and the old
        # empty-list guard made that unrepresentable. The archolith umbrella sat on 102 endpoints
        # belonging to nested repos it no longer indexes, permanently, because stopping at repo
        # boundaries dropped its own endpoint count to zero and the guard read that as failure.
        if not scan.partial_index:
            stale_eps = self._delete_stale_role_entities(
                scan.name, "endpoint", [f"endpoint:{ep.name}" for ep in scan.endpoints]
            )
            if stale_eps:
                logger.info(
                    "Pruned %d stale endpoint entities for project=%s",
                    stale_eps,
                    scan.name,
                )

        # 6. CONTAINS edges: project→dir, dir→subdir, dir→file
        contains_edges = self._build_contains_edges(scan)
        edge_count += self._write_edges_batch("CONTAINS", contains_edges, scan.name)

        # 7. DEPENDS_ON edges: project→dependency
        depends_edges = [
            {"source_path": ".", "target_path": f"dep:{dep}"}
            for dep in scan.dependencies
        ]
        edge_count += self._write_edges_batch("DEPENDS_ON", depends_edges, scan.name)

        # 8. TESTS edges: test→source
        test_edges = [
            {"source_path": te.test_path, "target_path": te.source_path}
            for te in scan.test_edges
        ]
        edge_count += self._write_edges_batch("TESTS", test_edges, scan.name)

        # 9. IMPORTS edges: file→file
        import_edges = [
            {"source_path": ie.source_path, "target_path": ie.target_path}
            for ie in scan.imports
        ]
        edge_count += self._write_edges_batch("IMPORTS", import_edges, scan.name)

        # 10. EXPOSES edges: project→endpoint
        exposes_edges = [
            {"source_path": ".", "target_path": f"endpoint:{ep.name}"}
            for ep in scan.endpoints
        ]
        edge_count += self._write_edges_batch("EXPOSES", exposes_edges, scan.name)

        # 11. CALLS edges: project→project (cross-project)
        for ref in scan.cross_project_refs:
            self._write_calls_edge(scan.name, ref, session_id, user_id, now)
            edge_count += 1

        # 11b. CONTAINS_REPO edges: umbrella project→nested repository. The scan stops at a
        #      nested repo rather than absorbing its files; this records the containment it
        #      declined to descend through.
        nested_repos = list(getattr(scan, "nested_repos", []) or [])
        for nested in nested_repos:
            self._write_contains_repo_edge(scan.name, nested, session_id, user_id, now)
            edge_count += 1

        # 11c. Prune CONTAINS_REPO edges for repos the scan no longer finds. Only the EDGE is
        #      removed; the child project keeps its own entities and is ingested on its own.
        #      Gated on scan completeness like the role prunes -- the file cap cannot truncate
        #      directory traversal, but a partial scan is not a claim about what is absent.
        if not scan.partial_index:
            stale_repos = self._delete_stale_contains_repo_edges(
                scan.name, [n.rel_path for n in nested_repos]
            )
            if stale_repos:
                logger.info(
                    "Pruned %d stale CONTAINS_REPO edges for project=%s",
                    stale_repos,
                    scan.name,
                )

        # 12. Symbols: delete stale + write new (DETACH DELETE also removes stale CALLS edges)
        #     Incremental: only touch symbols for files whose mtime changed.
        sym_entities, sym_edges = self._write_symbols(
            scan.symbols,
            scan.truncated_symbol_files,
            scan.name,
            project_id=getattr(scan, "project_id", None),
            session_id=session_id,
            user_id=user_id,
            now=now,
            changed_paths=changed_paths,
        )
        entity_count += sym_entities
        edge_count += sym_edges

        # 13. Intra-project CALLS edges: symbol→symbol (function-level call graph)
        if scan.call_edges:
            intra_calls = [
                {"source_path": ce.caller_path, "target_path": ce.callee_path}
                for ce in scan.call_edges
            ]
            edge_count += self._write_edges_batch("CALLS", intra_calls, scan.name)

        logger.info(
            "Structure write complete: project=%s entities=%d edges=%d",
            scan.name,
            entity_count,
            edge_count,
        )
        return {"entities": entity_count, "edges": edge_count}

    def write_document(
        self,
        file_path: str,
        content: str,
        *,
        project: str,
        structure_path: str,
        structure_project_id: str,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
    ) -> None:
        """MERGE a single document Entity node (structure_role='document').

        Args:
            file_path: Absolute path to the file (stored as root_path).
            content: File content excerpt (truncated to 2000 chars by caller).
            project: structure_project label (project name or parent dir).
            structure_path: Logical path for MERGE key (e.g. relative path or 'doc:<name>').
            structure_project_id: Settled durable identity stamped on the document.
            session_id: Session context.
            user_id: User context.
            document_type: Type of document (generic, wiki_article, reference_article).
                Used for filtering documents in recall/queries.
        """
        if not structure_project_id:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no "
                "structure_project_id."
            )
        now = datetime.now(timezone.utc).isoformat()
        name = Path(file_path).name
        self._merge_entity(
            structure_project=project,
            structure_project_id=structure_project_id,
            structure_path=structure_path,
            structure_role="document",
            name=name,
            content=content,
            session_id=session_id,
            user_id=user_id,
            now=now,
            extra={
                "root_path": file_path,
                "source": "document-ingest",
                "document_type": document_type,
            },
        )

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

    def get_file_mtimes(self, project_name: str) -> dict[str, float]:
        """Return stored file mtimes keyed by rel_path for a project.

        Only returns rows where ``file_mtime`` is set and non-zero — i.e.
        file/entrypoint/config/test entities written with scanner v2+.
        Returns empty dict for first-ever scan.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $name})
            WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
              AND n.file_mtime IS NOT NULL AND n.file_mtime > 0
            RETURN n.structure_path AS path, n.file_mtime AS mtime
            """,
            {"name": project_name},
        )
        return {str(r["path"]): float(r["mtime"]) for r in rows if r.get("path")}

    def _delete_file_entities(self, project_name: str, rel_paths: list[str]) -> None:
        """Delete file Entity nodes (and their Symbol children) for files removed from the project."""
        self.neo4j.execute(
            """
            UNWIND $paths AS path
            MATCH (f:Entity {structure_project: $project, structure_path: path})
            OPTIONAL MATCH (f)-[:DEFINES]->(sym:Entity {structure_role: 'symbol'})
            DETACH DELETE f, sym
            """,
            {"project": project_name, "paths": rel_paths},
        )

    def _delete_stale_role_entities(
        self, project_name: str, role: str, keep_paths: list[str]
    ) -> int:
        """Delete entities of *role* whose `structure_path` is absent from the current scan.

        An EMPTY `keep_paths` deletes every entity of *role*, and that is deliberate. Callers
        must first establish that the scan was complete (`not scan.partial_index`); given a
        complete scan, "found none" is a real answer and the only way to represent a project
        that stopped exposing anything. Guarding on emptiness instead made zero permanently
        unreachable -- see the archolith endpoint accumulation in `write_project` step 5b.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $project, structure_role: $role})
            WHERE NOT n.structure_path IN $keep
            DETACH DELETE n
            RETURN count(*) AS deleted
            """,
            {"project": project_name, "role": role, "keep": keep_paths},
        )
        return int(rows[0].get("deleted", 0)) if rows else 0

    def _delete_stale_role_entities_multi(
        self, project_name: str, roles: list[str], keep_paths: list[str]
    ) -> int:
        """Delete entities across several roles whose path is absent from the current scan.

        Files span four roles (`file`/`entrypoint`/`config`/`test`) and a single scan path
        may be classified into any of them, so the keep-set must be applied across all four
        at once -- pruning role by role would delete a file that merely changed role between
        scans (e.g. `app.py` reclassified from `file` to `entrypoint`).

        Same anti-footgun: an empty keep-list deletes nothing.
        """
        if not keep_paths:
            return 0
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $project})
            WHERE n.structure_role IN $roles
              AND NOT n.structure_path IN $keep
            OPTIONAL MATCH (n)-[:DEFINES]->(sym:Entity {structure_role: 'symbol'})
            DETACH DELETE n, sym
            RETURN count(*) AS deleted
            """,
            {"project": project_name, "roles": roles, "keep": keep_paths},
        )
        return int(rows[0].get("deleted", 0)) if rows else 0

    def _delete_stale_contains_repo_edges(
        self, project_name: str, keep_rel_paths: list[str]
    ) -> int:
        """Delete CONTAINS_REPO edges whose `rel_path` is absent from the current scan.

        Deletes the RELATIONSHIP only, never the child project entity: the child is its own
        project with its own ingest, and an umbrella that stops containing it says nothing
        about whether it still exists. An empty keep-list is meaningful on a complete scan --
        an umbrella whose sub-repos were all removed contains none.
        """
        rows = self.neo4j.execute(
            """
            MATCH (p:Entity {structure_project: $project, structure_path: '.',
                             structure_role: 'project'})-[r:CONTAINS_REPO]->()
            WHERE NOT coalesce(r.rel_path, '') IN $keep
            DELETE r
            RETURN count(*) AS deleted
            """,
            {"project": project_name, "keep": keep_rel_paths},
        )
        return int(rows[0].get("deleted", 0)) if rows else 0

    def _delete_stale_directories(
        self, project_name: str, keep_paths: list[str]
    ) -> int:
        """Delete directory entities no longer present in the scan.

        File entities are pruned via `_delete_file_entities` off the stored-mtime diff, but
        directories carry no mtime and so had no pruning path at all -- they accumulated
        forever. Making nested repos a scan boundary exposed how bad that is: the `archolith`
        umbrella dropped from 1,977 files to 8 while keeping 7,103 directory entities and
        7,066 CONTAINS edges describing subtrees it no longer indexes.

        Anti-footgun: an EMPTY `keep_paths` deletes nothing. A scan that legitimately finds no
        directories is indistinguishable here from a scan that failed to populate them, and
        wiping a project's entire directory tree on the strength of an empty list is not a
        risk worth taking for a case that saves nothing.
        """
        if not keep_paths:
            return 0
        rows = self.neo4j.execute(
            """
            MATCH (d:Entity {structure_project: $project, structure_role: 'directory'})
            WHERE NOT d.structure_path IN $keep
            WITH d, count(*) AS _
            DETACH DELETE d
            RETURN count(*) AS deleted
            """,
            {"project": project_name, "keep": keep_paths},
        )
        return int(rows[0].get("deleted", 0)) if rows else 0

    def _increment_heat(self, project_name: str, rel_paths: list[str]) -> None:
        """Increment hot_count on file entities that changed in this scan."""
        self.neo4j.execute(
            """
            UNWIND $paths AS path
            MATCH (n:Entity {structure_project: $project, structure_path: path})
            WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
            SET n.hot_count = coalesce(n.hot_count, 0) + 1
            """,
            {"project": project_name, "paths": rel_paths},
        )

    # ------------------------------------------------------------------
    # Read queries — used by query_structure MCP tool
    # ------------------------------------------------------------------

    def query_overview(self, project: str) -> dict[str, Any]:
        """Return a project summary with entity/edge counts by type."""
        # CF-73: the three independent reads become one. Each is a separate `CALL { ... }` so
        # the two aggregations cannot multiply each other's rows, and none imports a variable,
        # so there is no anchor whose absence would collapse the result.
        #
        # That last point is the trap worth naming: anchoring this on the project node would
        # turn an unknown project from "empty aggregates" into NO ROWS, and the method would
        # start raising or returning nothing where it used to return zeros. The description
        # branch is therefore OPTIONAL MATCH, and it is the only branch that touches the project
        # node at all. Pinned by `test_overview_of_an_unknown_project_is_empty_rather_than_an_error`.
        #
        # `get_project_coverage` and `query_contained_repos` stay as their own round trips.
        # Inlining their Cypher would take this to one call, but `get_project_coverage` has a
        # second caller (:1096) and both are public methods -- a copy here would be a second
        # definition of the same query, free to diverge silently from the canonical one. Five
        # round trips become three; the last two are a maintainability boundary, not an
        # oversight.
        rows = self.neo4j.execute(
            """
            CALL {
                MATCH (n:Entity {structure_project: $p})
                WITH n.structure_role AS role, count(n) AS cnt
                RETURN collect({role: role, cnt: cnt}) AS raw_entities
            }
            CALL {
                MATCH (a:Entity {structure_project: $p})-[r]->(b:Entity)
                WITH type(r) AS rel, count(r) AS cnt
                RETURN collect({rel: rel, cnt: cnt}) AS raw_edges
            }
            CALL {
                OPTIONAL MATCH (n:Entity {structure_project: $p, structure_role: 'project'})
                RETURN n.content AS description, n.stack AS stack
                LIMIT 1
            }
            RETURN raw_entities, raw_edges, description, stack
            """,
            {"p": project},
        )
        row = rows[0] if rows else {}

        def _tally(items: list[dict[str, Any]] | None, key: str) -> dict[str, int]:
            """Read the GROUPED rows back into a dict.

            The grouping happens server-side, inside each subquery, and that is load-bearing
            rather than stylistic: collecting one map per node and counting them here would
            ship a map for every entity in the project -- thousands, on a real codebase -- to
            replace a handful of aggregated rows. That would be a bandwidth regression
            introduced by a latency fix, which is worse than the finding.
            """
            counts: dict[str, int] = {}
            for item in items or []:
                name = item.get(key)
                if name is None:
                    continue
                counts[str(name)] = counts.get(str(name), 0) + int(item.get("cnt") or 0)
            return counts

        return {
            "project": project,
            "description": row.get("description") or "",
            "stack": row.get("stack") or "",
            "entities": _tally(row.get("raw_entities"), "role"),
            "edges": _tally(row.get("raw_edges"), "rel"),
            "coverage": self.get_project_coverage(project),
            "contains_repos": self.query_contained_repos(project),
        }

    def query_contained_repos(self, project: str) -> list[dict[str, str]]:
        """Nested repositories this project contains, via CONTAINS_REPO."""
        rows = self.neo4j.execute(
            """
            MATCH (p:Entity {structure_project: $p, structure_role: 'project'})
                  -[r:CONTAINS_REPO]->(child:Entity {structure_role: 'project'})
            RETURN child.structure_project AS name, r.rel_path AS rel_path
            ORDER BY rel_path
            """,
            {"p": project},
        )
        return [
            {"name": str(r["name"]), "rel_path": str(r.get("rel_path") or "")}
            for r in rows
        ]

    def query_files(self, project: str, path_filter: str = "") -> list[dict[str, str]]:
        """List files in a project, optionally filtered by path prefix."""
        if path_filter:
            rows = self.neo4j.execute(
                """
                MATCH (n:Entity {structure_project: $p})
                WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
                  AND n.structure_path STARTS WITH $prefix
                RETURN n.structure_path AS path, n.structure_role AS role,
                       n.content AS description,
                       coalesce(n.hot_count, 0) AS hot_count
                ORDER BY n.structure_path
                """,
                {"p": project, "prefix": path_filter},
            )
        else:
            rows = self.neo4j.execute(
                """
                MATCH (n:Entity {structure_project: $p})
                WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
                RETURN n.structure_path AS path, n.structure_role AS role,
                       n.content AS description,
                       coalesce(n.hot_count, 0) AS hot_count
                ORDER BY n.structure_path
                """,
                {"p": project},
            )
        return [
            {
                "path": str(r["path"]),
                "role": str(r["role"]),
                "description": str(r.get("description", "")),
                **(
                    {"hot_count": int(r["hot_count"])}
                    if int(r.get("hot_count", 0) or 0) > 0
                    else {}
                ),
            }
            for r in rows
        ]

    def query_documents(
        self, project: str, path_filter: str = "", document_type: str | None = None
    ) -> list[dict[str, str]]:
        """List document entities ingested via ingest_document for a project.

        Args:
            project: Project name.
            path_filter: Optional path prefix filter.
            document_type: Optional document_type filter (generic, wiki_article, reference_article).
        """
        params: dict[str, Any] = {"p": project}
        conditions: list[str] = []

        if path_filter:
            conditions.append("n.structure_path STARTS WITH $prefix")
            params["prefix"] = path_filter

        if document_type:
            conditions.append("n.document_type = $doc_type")
            params["doc_type"] = document_type

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{structure_project: $p, structure_role: 'document'}})
            WHERE {where_clause}
            RETURN n.name AS name, n.structure_path AS path,
                   n.content AS description, n.root_path AS root_path,
                   n.document_type AS doc_type
            ORDER BY n.name
            """,
            params,
        )
        return [
            {
                "name": str(r["name"]),
                "path": str(r["path"]),
                "description": str(r.get("description", "")),
                "root_path": str(r.get("root_path", "")),
                "doc_type": str(r.get("doc_type", "generic")),
            }
            for r in rows
        ]

    def link_episode_to_documents(
        self,
        episode_uuid: str,
        entity_names: list[str],
        project: str,
        max_links: int = 5,
    ) -> int:
        """Link an episode to up to max_links wiki/reference document entities by name match.

        Creates RELATES_TO edges from the episode to matching document entities.

        Args:
            episode_uuid: The episode node UUID to link from.
            entity_names: Extracted entity names from the episode content.
            project: Project to search documents in.
            max_links: Maximum number of links to create (default 5).

        Returns:
            Number of links created.
        """
        if not entity_names:
            return 0

        # Find matching document entities by name overlap (case-insensitive)
        rows = self.neo4j.execute(
            """
            UNWIND $names AS name
            MATCH (d:Entity {structure_project: $p, structure_role: 'document'})
            WHERE d.document_type IN ['wiki_article', 'reference_article']
              AND (toLower(d.name) CONTAINS toLower(name)
                OR toLower(d.root_path) CONTAINS toLower(name))
            WITH d LIMIT $limit
            MATCH (e:Entity {id: $episode_id})
            MERGE (e)-[r:RELATES_TO]->(d)
            ON CREATE SET r.created_at = timestamp(), r.weight = 1.0
            RETURN count(r) AS links
            """,
            {
                "names": entity_names,
                "p": project,
                "episode_id": episode_uuid,
                "limit": max_links,
            },
        )
        return rows[0].get("links", 0) if rows else 0

    def get_linked_documents(self, episode_uuids: list[str]) -> list[dict[str, str]]:
        """Get wiki/reference documents linked to episodes via RELATES_TO."""
        if not episode_uuids:
            return []
        rows = self.neo4j.execute(
            """
            MATCH (e:Entity)-[:RELATES_TO]->(d:Entity {structure_role: 'document'})
            WHERE e.id IN $uuids AND d.document_type IN ['wiki_article', 'reference_article']
            RETURN DISTINCT d.name AS name, d.root_path AS root_path, d.document_type AS doc_type
            ORDER BY d.name
            """,
            {"uuids": episode_uuids},
        )
        return [
            {
                "name": str(r["name"]),
                "root_path": str(r.get("root_path", "")),
                "doc_type": str(r.get("doc_type", "generic")),
            }
            for r in rows
        ]

    def query_imports(self, project: str, file_path: str) -> dict[str, list[str]]:
        """Return what a file imports and what imports it."""
        outgoing = self.neo4j.execute(
            """
            MATCH (a:Entity {structure_project: $p, structure_path: $f})-[:IMPORTS]->(b:Entity)
            RETURN b.structure_path AS path ORDER BY path
            """,
            {"p": project, "f": file_path},
        )
        incoming = self.neo4j.execute(
            """
            MATCH (a:Entity)-[:IMPORTS]->(b:Entity {structure_project: $p, structure_path: $f})
            RETURN a.structure_path AS path ORDER BY path
            """,
            {"p": project, "f": file_path},
        )
        return {
            "imports": [str(r["path"]) for r in outgoing],
            "imported_by": [str(r["path"]) for r in incoming],
        }

    def query_tests(self, project: str, file_path: str = "") -> list[dict[str, str]]:
        """Return test→source mappings, optionally filtered to a source file."""
        if file_path:
            rows = self.neo4j.execute(
                """
                MATCH (t:Entity)-[:TESTS]->(s:Entity {structure_project: $p, structure_path: $f})
                RETURN t.structure_path AS test_file, s.structure_path AS source_file
                ORDER BY t.structure_path
                """,
                {"p": project, "f": file_path},
            )
        else:
            rows = self.neo4j.execute(
                """
                MATCH (t:Entity)-[:TESTS]->(s:Entity {structure_project: $p})
                RETURN t.structure_path AS test_file, s.structure_path AS source_file
                ORDER BY t.structure_path
                """,
                {"p": project},
            )
        return [
            {"test": str(r["test_file"]), "source": str(r["source_file"])} for r in rows
        ]

    def query_endpoints(self, project: str) -> list[dict[str, str]]:
        """Return all endpoints exposed by a project."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $p, structure_role: 'endpoint'})
            RETURN n.name AS name, n.content AS description, n.structure_path AS path
            ORDER BY n.name
            """,
            {"p": project},
        )
        return [
            {
                "name": str(r["name"]),
                "description": str(r.get("description", "")),
                "path": str(r.get("path", "")),
            }
            for r in rows
        ]

    def query_dependencies(self, project: str) -> list[str]:
        """Return external dependency names for a project."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_project: $p, structure_role: 'dependency'})
            RETURN n.name AS name ORDER BY name
            """,
            {"p": project},
        )
        return [str(r["name"]) for r in rows]

    def query_cross_refs(self, project: str) -> list[dict[str, str]]:
        """Return cross-project CALLS edges from a project."""
        rows = self.neo4j.execute(
            """
            MATCH (a:Entity {structure_project: $p, structure_role: 'project'})-[r:CALLS]->(b:Entity)
            RETURN b.name AS target, r.mechanism AS mechanism, r.evidence AS evidence
            ORDER BY b.name
            """,
            {"p": project},
        )
        return [
            {
                "target": str(r["target"]),
                "mechanism": str(r.get("mechanism", "")),
                "evidence": str(r.get("evidence", "")),
            }
            for r in rows
        ]

    def query_blast_radius(
        self,
        project: str,
        file_paths: list[str],
        max_depth: int = 5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Trace transitive reverse-imports from changed files + map to affected tests.

        Returns {
            "changed": [...],
            "directly_affected": [...],
            "transitively_affected": [...],
            "affected_tests": [...],
            "cross_project_refs": [...],
        }
        """
        # Transitive reverse-import traversal with depth tracking
        # depth=1 → direct importers, depth>1 → transitive
        rows = self.neo4j.execute(
            """
            UNWIND $paths AS changed_path
            MATCH (changed:Entity {structure_project: $p, structure_path: changed_path})
            OPTIONAL MATCH path = (importer:Entity)-[:IMPORTS*1..]->(changed)
            WHERE ALL(n IN nodes(path) WHERE n.structure_project = $p)
              AND length(path) <= $depth
            RETURN DISTINCT importer.structure_path AS importer_path,
                   length(path) AS hop_distance
            """,
            {"p": project, "paths": file_paths, "depth": max_depth},
        )

        direct: set[str] = set()
        transitive: set[str] = set()
        changed_set = set(file_paths)

        for row in rows:
            imp = row.get("importer_path")
            if not imp or imp in changed_set:
                continue
            dist = row.get("hop_distance", 1)
            if dist == 1:
                direct.add(imp)
            else:
                transitive.add(imp)

        # Don't double-count: if a file is both direct and transitive, keep it as direct
        transitive -= direct

        # Find affected tests: tests that cover any changed/affected file
        all_impacted = list(changed_set | direct | transitive)
        test_rows = self.neo4j.execute(
            """
            UNWIND $paths AS src_path
            MATCH (t:Entity)-[:TESTS]->(s:Entity {structure_project: $p, structure_path: src_path})
            RETURN DISTINCT t.structure_path AS test_file, s.structure_path AS covers
            ORDER BY t.structure_path
            """,
            {"p": project, "paths": all_impacted},
        )
        affected_tests = [
            {"test": str(r["test_file"]), "covers": str(r["covers"])}
            for r in test_rows
            if r.get("test_file")
        ]

        # Cross-project impact: check if any changed file is an endpoint or has CALLS edges
        xref_rows = self.neo4j.execute(
            """
            MATCH (src:Entity {structure_project: $p, structure_role: 'project'})-[r:CALLS]->(tgt:Entity)
            RETURN tgt.name AS target, r.mechanism AS mechanism
            """,
            {"p": project},
        )
        cross_refs = [
            {"target": str(r["target"]), "mechanism": str(r.get("mechanism", ""))}
            for r in xref_rows
        ]

        # Related memories: semantic memories anchored to impacted files
        related_memories = self.query_linked_memories(
            project, all_impacted, limit=10, namespace=namespace
        )

        # Open TODOs at any impacted location. Matched on the todo's own
        # normalized :TodoLocation (exact project + path) rather than the
        # REFERENCES_FILE edge, which resolved only 13 of 77 todos because
        # code_ref mixes workspace- and project-relative forms while the old
        # resolver required the stored path to END WITH the ref.
        #
        # Line and symbol are narrowing detail, never identity: a todo naming a
        # file with no line still surfaces for that file.
        #
        # Namespace is read off the owning :Todo -- locations deliberately carry
        # no copy of it -- and follows the same requested-plus-default rule as
        # list_todos/get_todo so this path cannot leak across silos.
        todo_namespaces = [namespace, _DEFAULT_TODO_NAMESPACE] if namespace else None
        todo_rows = self.neo4j.execute(
            """
            UNWIND $paths AS src_path
            MATCH (t:Todo {status: 'open'})-[:HAS_LOCATION]->(l:TodoLocation)
            WHERE l.resolution_status = 'resolved'
              AND (
                    l.path = src_path
                    // A bare filename ("CardPrintingImportService.java") is a
                    // legitimate but underspecified declaration. Match it on
                    // basename only -- narrower than the old blanket ENDS WITH,
                    // which is what made an unrelated ref match any suffix.
                    OR (NOT l.path CONTAINS '/' AND src_path ENDS WITH ('/' + l.path))
                  )
              AND (l.project IS NULL OR l.project = $p)
              AND ($namespaces IS NULL OR t.namespace IN $namespaces)
            RETURN DISTINCT
                t.uuid     AS uuid,
                t.content  AS content,
                t.priority AS priority,
                t.code_ref AS code_ref,
                src_path   AS file_path
            ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END
            """,
            {"paths": all_impacted, "p": project, "namespaces": todo_namespaces},
        )
        open_todos = [
            {
                "uuid": str(r["uuid"]),
                "content": str(r["content"]),
                "priority": str(r["priority"]),
                "code_ref": r.get("code_ref"),
                "file_path": str(r["file_path"]),
            }
            for r in todo_rows
            if r.get("uuid")
        ]

        # Function-level callers via intra-project CALLS edges
        fn_caller_rows = self.neo4j.execute(
            """
            UNWIND $paths AS changed_path
            MATCH (f:Entity {structure_project: $p, structure_path: changed_path})
            MATCH (f)-[:DEFINES]->(callee_sym:Entity {structure_role: 'symbol'})
            MATCH (caller_sym:Entity {structure_role: 'symbol', structure_project: $p})-[:CALLS]->(callee_sym)
            MATCH (caller_file:Entity {structure_project: $p})-[:DEFINES]->(caller_sym)
            WHERE caller_file.structure_path <> changed_path
              AND NOT caller_file.structure_path IN $paths
            RETURN DISTINCT
                caller_sym.name AS caller_name,
                caller_sym.symbol_parent AS caller_class,
                caller_file.structure_path AS caller_file,
                callee_sym.name AS callee_name,
                callee_sym.symbol_parent AS callee_class
            ORDER BY caller_file.structure_path, caller_sym.name
            LIMIT 30
            """,
            {"p": project, "paths": file_paths},
        )
        function_callers = [
            {
                "caller": f"{r['caller_class']}.{r['caller_name']}"
                if r.get("caller_class")
                else str(r["caller_name"]),
                "caller_file": str(r["caller_file"]),
                "callee": f"{r['callee_class']}.{r['callee_name']}"
                if r.get("callee_class")
                else str(r["callee_name"]),
            }
            for r in fn_caller_rows
            if r.get("caller_file")
        ]

        # Coverage qualification: which requested paths are actually in the index, and is the
        # project's index complete? Without this the caller cannot distinguish "nothing
        # depends on this file" from "this file was never scanned".
        indexed = self.which_paths_indexed(project, list(file_paths))
        return {
            "changed": sorted(changed_set),
            "directly_affected": sorted(direct),
            "transitively_affected": sorted(transitive),
            "affected_tests": affected_tests,
            "cross_project_refs": cross_refs,
            "related_memories": related_memories,
            "open_todos": open_todos,
            "function_callers": function_callers,
            "unindexed_paths": sorted(p for p in file_paths if p not in indexed),
            "coverage": self.get_project_coverage(project),
        }

    def query_affected_tests(
        self, project: str, file_paths: list[str] | None = None, max_depth: int = 5
    ) -> dict[str, Any]:
        """Given changed files (or auto-detect from git), return the minimal test set.

        Returns {
            "changed_files": [...],
            "test_files": [...],
            "test_command": "pytest ...",
        }
        """
        if not file_paths:
            return {"changed_files": [], "test_files": [], "test_command": "pytest"}

        # Get the full blast radius first
        radius = self.query_blast_radius(project, file_paths, max_depth=max_depth)

        # Collect unique test files
        test_files = sorted({t["test"] for t in radius["affected_tests"]})

        # Build pytest command
        if test_files:
            test_cmd = "pytest " + " ".join(test_files)
        else:
            test_cmd = "pytest  # no specific tests found — run full suite"

        return {
            "changed_files": file_paths,
            "affected_source_files": sorted(
                set(radius["directly_affected"]) | set(radius["transitively_affected"])
            ),
            "test_files": test_files,
            "test_command": test_cmd,
            # Carried through from the blast-radius traversal: an empty test set is only
            # meaningful when the inputs were indexed and the project index is complete.
            "unindexed_paths": radius.get("unindexed_paths", []),
            "coverage": radius.get("coverage", {}),
        }

    def resolve_structural_neighbors(self, project: str, file_path: str) -> list[str]:
        """Return the file's UUID plus UUIDs of its imports, importers, and testers."""
        rows = self.neo4j.execute(
            """
            MATCH (f:Entity {structure_project: $p, structure_path: $path})
            // CF-224: these UUIDs are consumed by recall_support, so a foreign uuid does not
            // merely display -- it selects another project's node for retrieval.
            OPTIONAL MATCH (f)-[:IMPORTS]->(imp:Entity {structure_project: $p})
            OPTIONAL MATCH (importer:Entity {structure_project: $p})-[:IMPORTS]->(f)
            OPTIONAL MATCH (tester:Entity {structure_project: $p})-[:TESTS]->(f)
            RETURN f.uuid AS file_uuid,
                   collect(DISTINCT imp.uuid) AS import_uuids,
                   collect(DISTINCT importer.uuid) AS importer_uuids,
                   collect(DISTINCT tester.uuid) AS tester_uuids
            """,
            {"p": project, "path": file_path},
        )
        if not rows:
            return []
        row = rows[0]
        file_uuid = row.get("file_uuid")
        if not file_uuid:
            return []
        uuids: set[str] = {str(file_uuid)}
        for key in ("import_uuids", "importer_uuids", "tester_uuids"):
            for uuid in row.get(key) or []:
                if uuid:
                    uuids.add(str(uuid))
        return sorted(uuids)

    def resolve_structural_neighbors_bulk(
        self, projects: list[str], file_path: str
    ) -> tuple[str, list[str]] | None:
        """Return the first matched project and its file's UUID plus UUIDs of its imports, importers, and testers."""
        if not projects:
            return None
        rows = self.neo4j.execute(
            """
            UNWIND $projects AS p
            MATCH (f:Entity {structure_project: p, structure_path: $path})
            // CF-224: scoped to `p`, the UNWIND variable -- NOT `$p`, which does not exist on
            // this query and made it fail with ParameterMissing rather than leak. The two
            // sibling queries look identical and bind their project differently; a copied
            // predicate is wrong in exactly one of them.
            OPTIONAL MATCH (f)-[:IMPORTS]->(imp:Entity {structure_project: p})
            OPTIONAL MATCH (importer:Entity {structure_project: p})-[:IMPORTS]->(f)
            OPTIONAL MATCH (tester:Entity {structure_project: p})-[:TESTS]->(f)
            RETURN p AS matched_project,
                   f.uuid AS file_uuid,
                   collect(DISTINCT imp.uuid) AS import_uuids,
                   collect(DISTINCT importer.uuid) AS importer_uuids,
                   collect(DISTINCT tester.uuid) AS tester_uuids
            LIMIT 1
            """,
            {"projects": projects, "path": file_path},
        )
        if not rows:
            return None
        row = rows[0]
        file_uuid = row.get("file_uuid")
        if not file_uuid:
            return None

        matched_project = row["matched_project"]
        uuids: set[str] = {str(file_uuid)}
        for key in ("import_uuids", "importer_uuids", "tester_uuids"):
            for uuid in row.get(key) or []:
                if uuid:
                    uuids.add(str(uuid))
        return matched_project, sorted(uuids)

    def query_linked_memories(
        self,
        project: str,
        file_paths: list[str],
        limit: int = 10,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, str]]:
        """Find semantic memories anchored to the given structural file paths.

        The result is restricted to the given namespace when one is supplied, and
        unfiltered when it is None.
        """
        if not file_paths:
            return []
        # Isolation is opt-in (domain/namespace.py): an unspecified namespace must not filter,
        # so the predicate is omitted entirely rather than passed as a null-guarded no-op.
        # Predicated on group_id, the load-bearing isolation boundary -- `namespace` on a node
        # is only the defense-in-depth stamp. NULL group_id therefore does NOT match a scoped
        # read: this is a leak fix, so an unstamped node fails closed rather than open.
        group_ids = namespace_to_group_ids(namespace)
        params: dict[str, Any] = {"p": project, "paths": file_paths, "limit": limit}
        tenancy_filter = ""
        if group_ids is not None:
            tenancy_filter = "AND sem.group_id IN $group_ids"
            params["group_ids"] = group_ids
        rows = self.neo4j.execute(
            f"""
            UNWIND $paths AS fp
            MATCH (struct:Entity {{structure_project: $p, structure_path: fp}})
                  <-[r:ANCHORED_TO]-(sem:Entity)
            WHERE sem.structure_role IS NULL
              AND coalesce(sem.freshness, 'ACTIVE') <> 'GONE'
              {tenancy_filter}
            WITH sem, struct, r
            ORDER BY sem.last_accessed DESC
            RETURN sem.uuid AS uuid,
                   sem.name AS name,
                   left(coalesce(sem.content, sem.summary, ''), 120) AS preview,
                   collect(DISTINCT struct.structure_path)[0] AS linked_file,
                   collect(DISTINCT r.anchor_source)[0] AS anchor_source,
                   max(sem.last_accessed) AS last_accessed
            ORDER BY last_accessed DESC
            LIMIT $limit
            """,
            params,
        )
        return [
            {
                "uuid": str(r["uuid"]),
                "name": str(r.get("name", "")),
                "preview": str(r.get("preview", "")),
                "linked_file": str(r.get("linked_file", "")),
                "anchor_source": str(r.get("anchor_source", "")),
            }
            for r in rows
            if r.get("uuid")
        ]

    def list_projects(self) -> list[dict[str, str]]:
        """Return all ingested project entities."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {structure_role: 'project'})
            RETURN n.structure_project AS name, n.content AS description,
                   coalesce(properties(n)['stack'], '') AS stack,
                   coalesce(properties(n)['root_path'], '') AS root_path,
                   properties(n)['files_eligible'] AS files_eligible,
                   properties(n)['files_indexed'] AS files_indexed,
                   coalesce(properties(n)['partial_index'], false) AS partial_index
            ORDER BY name
            """,
        )
        return [
            {
                "name": str(r["name"]),
                "description": str(r.get("description", "")),
                "stack": str(r.get("stack", "")),
                "root_path": str(r.get("root_path", "")),
                "files_eligible": r.get("files_eligible"),
                "files_indexed": r.get("files_indexed"),
                "partial_index": bool(r.get("partial_index")),
            }
            for r in rows
        ]

    def list_orphan_structure_projects(self) -> list[dict[str, Any]]:
        """Return structure_project values that have entities but no project entity.

        `list_projects` can only see projects that still have a project node, so an entity set
        whose project node was deleted becomes invisible: it appears in no listing, and every
        staleness check runs off the listing. Live graph carried 3,580 such entities across 11
        names, the largest being yawn.bot (1,255) and cth.context-engine (1,156).
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.structure_project IS NOT NULL
            WITH n.structure_project AS name,
                 collect(DISTINCT n.structure_role) AS roles,
                 count(*) AS entities
            WHERE NOT 'project' IN roles
            RETURN name, entities
            ORDER BY entities DESC, name
            """,
        )
        return [
            {"name": str(r["name"]), "entities": int(r.get("entities", 0))}
            for r in rows
        ]

    def query_symbols(self, project: str, path: str = "") -> dict[str, Any]:
        """Return symbols for a file (exact path), directory prefix (trailing /), or whole project."""
        if not path:
            sym_rows = self.neo4j.execute(
                """
                MATCH (sym:Entity {structure_project: $p, structure_role: 'symbol'})
                RETURN sym.name AS name, sym.symbol_kind AS kind,
                       sym.symbol_signature AS sig, sym.content AS doc,
                       sym.symbol_line AS line, sym.symbol_parent AS parent,
                       sym.symbol_decorator AS decorator,
                       sym.structure_path AS fqpath
                ORDER BY sym.structure_path, sym.symbol_line
                """,
                {"p": project},
            )
            trunc_rows = self.neo4j.execute(
                """
                MATCH (f:Entity {structure_project: $p, symbols_truncated: true})
                RETURN count(f) > 0 AS any_truncated
                """,
                {"p": project},
            )
        elif path.endswith("/"):
            sym_rows = self.neo4j.execute(
                """
                MATCH (sym:Entity {structure_project: $p, structure_role: 'symbol'})
                WHERE sym.structure_path STARTS WITH $prefix
                RETURN sym.name AS name, sym.symbol_kind AS kind,
                       sym.symbol_signature AS sig, sym.content AS doc,
                       sym.symbol_line AS line, sym.symbol_parent AS parent,
                       sym.symbol_decorator AS decorator,
                       sym.structure_path AS fqpath
                ORDER BY sym.structure_path, sym.symbol_line
                """,
                {"p": project, "prefix": path},
            )
            trunc_rows = self.neo4j.execute(
                """
                MATCH (f:Entity {structure_project: $p, symbols_truncated: true})
                WHERE f.structure_path STARTS WITH $prefix
                RETURN count(f) > 0 AS any_truncated
                """,
                {"p": project, "prefix": path},
            )
        else:
            sym_rows = self.neo4j.execute(
                """
                MATCH (sym:Entity {structure_project: $p, structure_role: 'symbol'})
                WHERE sym.structure_path STARTS WITH $prefix
                RETURN sym.name AS name, sym.symbol_kind AS kind,
                       sym.symbol_signature AS sig, sym.content AS doc,
                       sym.symbol_line AS line, sym.symbol_parent AS parent,
                       sym.symbol_decorator AS decorator,
                       sym.structure_path AS fqpath
                ORDER BY sym.symbol_line
                """,
                {"p": project, "prefix": path + "::"},
            )
            trunc_rows = self.neo4j.execute(
                """
                MATCH (f:Entity {structure_project: $p, structure_path: $file_path})
                RETURN coalesce(f.symbols_truncated, false) AS any_truncated
                """,
                {"p": project, "file_path": path},
            )

        symbols = [
            {
                "name": str(r.get("name", "")),
                "kind": str(r.get("kind", "")),
                "sig": str(r.get("sig", "")),
                "doc": str(r.get("doc", "")),
                "line": int(r["line"]) if r.get("line") is not None else 0,
                "parent": str(r.get("parent", "")),
                "decorator": str(r.get("decorator", "")),
            }
            for r in sym_rows
        ]
        any_truncated = (
            bool(trunc_rows[0].get("any_truncated", False)) if trunc_rows else False
        )
        return {"symbols": symbols, "truncated": any_truncated}

    def query_context(self, project: str, path: str = "") -> dict[str, Any]:
        """Return full context for a single file: summary, symbols, imports, imported_by, tested_by."""
        if not path:
            raise ValueError("path is required for context query")

        # CF-73: one round trip, not five. The five statements this replaces all anchored on
        # the same `(f:Entity {structure_project, structure_path})` pattern and were independent
        # of one another, so they compose as OPTIONAL MATCH branches over one anchor.
        #
        # Each branch is collected in its OWN `CALL { ... }` subquery, which is the part that
        # matters. Chaining OPTIONAL MATCHes in a single scope multiplies rows -- two symbols
        # times two importers is four rows -- and the counts then come out wrong in a way that
        # only shows on a file that has several of both. A per-branch subquery aggregates before
        # the next branch runs, so each returns exactly one list.
        #
        # `[x IN collect(...) WHERE x IS NOT NULL]` is not defensive noise: OPTIONAL MATCH on a
        # file with no symbols yields one row of nulls, so an unfiltered collect returns `[null]`
        # rather than `[]`, and a null path would reach the MCP caller as the string "None".
        #
        # The importer and tester branches are deliberately NOT filtered by `structure_project`,
        # because the originals were not either -- `MATCH (importer:Entity)` qualified only the
        # anchor. That is a cross-project leak (filed CF-224) but fixing it here would change
        # what this returns under cover of a performance change. Pinned by
        # `test_context_does_not_cross_project_boundaries`.
        rows = self.neo4j.execute(
            """
            MATCH (f:Entity {structure_project: $p, structure_path: $path})
            CALL {
                WITH f
                OPTIONAL MATCH (f)-[:DEFINES]->(sym:Entity)
                WHERE sym.structure_role = 'symbol' AND sym.structure_project = $p
                WITH sym ORDER BY sym.symbol_line
                RETURN collect(CASE WHEN sym IS NULL THEN null ELSE {
                    name: sym.name, kind: sym.symbol_kind, sig: sym.symbol_signature,
                    doc: sym.content, line: sym.symbol_line, parent: sym.symbol_parent,
                    decorator: sym.symbol_decorator
                } END) AS raw_symbols
            }
            CALL {
                WITH f
                OPTIONAL MATCH (f)-[:IMPORTS]->(imp:Entity {structure_project: $p})
                WITH imp ORDER BY imp.structure_path
                RETURN collect(imp.structure_path) AS raw_imports
            }
            CALL {
                WITH f
            // CF-224: every branch carries `structure_project`, not only the anchor. An
            // unqualified `MATCH (importer:Entity)` returns importers from EVERY project in the
            // database -- and because two projects routinely share file paths, the leaked value
            // looks exactly like one of the caller's own files.
                OPTIONAL MATCH (importer:Entity {structure_project: $p})-[:IMPORTS]->(f)
                WITH importer ORDER BY importer.structure_path
                RETURN collect(importer.structure_path) AS raw_imported_by
            }
            CALL {
                WITH f
                OPTIONAL MATCH (t:Entity {structure_project: $p})-[:TESTS]->(f)
                WITH t ORDER BY t.structure_path
                RETURN collect(t.structure_path) AS raw_tested_by
            }
            RETURN f.content AS summary,
                   coalesce(f.symbols_truncated, false) AS truncated,
                   [x IN raw_symbols WHERE x IS NOT NULL] AS symbols,
                   [x IN raw_imports WHERE x IS NOT NULL] AS imports,
                   [x IN raw_imported_by WHERE x IS NOT NULL] AS imported_by,
                   [x IN raw_tested_by WHERE x IS NOT NULL] AS tested_by
            """,
            {"p": project, "path": path},
        )
        if not rows:
            return {"error": f"File not found in structure graph: {path}"}

        row = rows[0]
        symbols = [
            {
                "name": str(r.get("name") or ""),
                "kind": str(r.get("kind") or ""),
                "sig": str(r.get("sig") or ""),
                "doc": str(r.get("doc") or ""),
                "line": int(r["line"]) if r.get("line") is not None else 0,
                "parent": str(r.get("parent") or ""),
                "decorator": str(r.get("decorator") or ""),
            }
            for r in (row.get("symbols") or [])
        ]
        return {
            "path": path,
            "summary": str(row.get("summary", "")),
            "truncated": bool(row.get("truncated", False)),
            "symbols": symbols,
            "imports": [str(x) for x in (row.get("imports") or [])],
            "imported_by": [str(x) for x in (row.get("imported_by") or [])],
            "tested_by": [str(x) for x in (row.get("tested_by") or [])],
        }

    # ------------------------------------------------------------------
    # Private — symbol writes
    # ------------------------------------------------------------------

    def _write_symbols(
        self,
        symbols: list[SymbolEntry],
        truncated_files: list[str],
        project_name: str,
        *,
        project_id: str | None,
        session_id: str,
        user_id: str,
        now: str,
        changed_paths: set[str] | None = None,
    ) -> tuple[int, int]:
        """Delete stale symbol nodes for a project and write fresh ones.

        If ``changed_paths`` is None (first scan / force), all symbols are replaced.
        If provided, only symbols belonging to those files are touched — symbols
        for unchanged files are left in place.

        Returns (entity_count, edge_count).
        """
        logger.debug(
            "write_symbols: project=%s symbols=%d truncated=%d incremental=%s",
            project_name,
            len(symbols),
            len(truncated_files),
            f"{len(changed_paths)} files" if changed_paths is not None else "full",
        )

        if changed_paths is None:
            # Full replace — delete all existing symbols for the project
            self.neo4j.execute(
                """
                MATCH (sym:Entity {structure_project: $project, structure_role: 'symbol'})
                DETACH DELETE sym
                """,
                {"project": project_name},
            )
            symbols_to_write = symbols
            truncated_to_mark = truncated_files
        else:
            # Incremental — only delete symbols for files that changed
            if changed_paths:
                self.neo4j.execute(
                    """
                    UNWIND $paths AS path
                    MATCH (f:Entity {structure_project: $project, structure_path: path})
                    MATCH (f)-[:DEFINES]->(sym:Entity {structure_role: 'symbol'})
                    DETACH DELETE sym
                    """,
                    {"project": project_name, "paths": list(changed_paths)},
                )
            symbols_to_write = [s for s in symbols if s.file_path in changed_paths]
            truncated_to_mark = [t for t in truncated_files if t in changed_paths]

        if not symbols_to_write:
            return 0, 0

        # Mark file entities that hit the per-file symbol cap
        if truncated_to_mark:
            self.neo4j.execute(
                """
                UNWIND $paths AS path
                MATCH (f:Entity {structure_project: $project, structure_path: path})
                SET f.symbols_truncated = true
                """,
                {"project": project_name, "paths": truncated_to_mark},
            )

        sym_rows = [
            {
                "uuid": str(uuid4()),
                "structure_project": project_name,
                "structure_project_id": project_id,
                "structure_path": _symbol_path(sym),
                "structure_role": "symbol",
                "name": sym.name,
                "content": sym.docstring[:120] if sym.docstring else "",
                "symbol_kind": sym.kind,
                "symbol_line": sym.line_no,
                "symbol_signature": sym.signature,
                "symbol_parent": sym.parent,
                "symbol_decorator": sym.decorator,
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
            }
            for sym in symbols_to_write
        ]
        entity_count = self._merge_symbols_batch(sym_rows)

        defines_edges = [
            {"source_path": sym.file_path, "target_path": _symbol_path(sym)}
            for sym in symbols_to_write
        ]
        edge_count = self._write_edges_batch("DEFINES", defines_edges, project_name)

        return entity_count, edge_count

    # ------------------------------------------------------------------
    # Private — entity writes
    # ------------------------------------------------------------------

    def _merge_entity(
        self,
        *,
        structure_project: str,
        structure_path: str,
        structure_role: str,
        name: str,
        content: str,
        session_id: str,
        user_id: str,
        now: str,
        extra: dict[str, Any] | None = None,
        structure_project_id: str | None = None,
    ) -> None:
        props = {
            **_ENTITY_DEFAULTS,
            "structure_project": structure_project,
            "structure_project_id": structure_project_id,
            "structure_path": structure_path,
            "structure_role": structure_role,
            "name": name,
            "content": content,
            "session_id": session_id,
            "user_id": user_id,
            "created_at": now,
            "last_accessed": now,
        }
        if extra:
            props.update(extra)

        self.neo4j.execute(
            """
            MERGE (n:Entity {structure_project: $sp, structure_path: $spath})
            ON CREATE SET n += $props, n.uuid = $uuid
            ON MATCH SET n += $extra, n.content = $content, n.last_accessed = $now,
                         n.structure_role = $role, n.name = $name,
                         n.structure_project_id = coalesce($spid, n.structure_project_id),
                         n.group_id = coalesce(n.group_id, ''),
                         n.summary = coalesce(n.summary, '')
            """,
            {
                "sp": structure_project,
                "spath": structure_path,
                "spid": structure_project_id,
                "uuid": str(uuid4()),
                "props": props,
                "extra": extra or {},
                "content": content,
                "now": now,
                "role": structure_role,
                "name": name,
            },
        )

    def _merge_entities_batch(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.neo4j.execute(
            """
            UNWIND $rows AS row
            MERGE (n:Entity {structure_project: row.structure_project, structure_path: row.structure_path})
            ON CREATE SET
                n.uuid = row.uuid,
                n.name = row.name,
                n.content = row.content,
                n.summary = '',
                n.type = 'SEMANTIC',
                n.scope = 'PERSISTENT',
                n.source = 'project-scan',
                n.source_confidence = $sc,
                n.user_flagged = false,
                n.group_id = '',
                n.structure_role = row.structure_role,
                n.structure_project = row.structure_project,
                n.structure_project_id = row.structure_project_id,
                n.structure_path = row.structure_path,
                n.file_mtime = coalesce(row.file_mtime, 0.0),
                n.hot_count = 0,
                n.session_id = row.session_id,
                n.user_id = row.user_id,
                n.created_at = row.now,
                n.last_accessed = row.now
            ON MATCH SET
                n.structure_project_id = coalesce(row.structure_project_id, n.structure_project_id),
                n.content = row.content,
                n.summary = coalesce(n.summary, ''),
                n.last_accessed = row.now,
                n.structure_role = row.structure_role,
                n.name = row.name,
                n.file_mtime = coalesce(row.file_mtime, n.file_mtime)
            """,
            {"rows": rows, "sc": STRUCTURE_SOURCE_CONFIDENCE},
        )
        return len(rows)

    def _merge_symbols_batch(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.neo4j.execute(
            """
            UNWIND $rows AS row
            MERGE (n:Entity {structure_project: row.structure_project, structure_path: row.structure_path})
            ON CREATE SET
                n.uuid = row.uuid,
                n.name = row.name,
                n.content = row.content,
                n.type = 'SEMANTIC',
                n.scope = 'PERSISTENT',
                n.source = 'project-scan',
                n.source_confidence = $sc,
                n.user_flagged = false,
                n.group_id = '',
                n.summary = '',
                n.structure_role = row.structure_role,
                n.structure_project = row.structure_project,
                n.structure_project_id = row.structure_project_id,
                n.structure_path = row.structure_path,
                n.symbol_kind = row.symbol_kind,
                n.symbol_line = row.symbol_line,
                n.symbol_signature = row.symbol_signature,
                n.symbol_parent = row.symbol_parent,
                n.symbol_decorator = row.symbol_decorator,
                n.session_id = row.session_id,
                n.user_id = row.user_id,
                n.created_at = row.now,
                n.last_accessed = row.now
            ON MATCH SET
                n.structure_project_id = coalesce(row.structure_project_id, n.structure_project_id),
                n.content = row.content,
                n.summary = coalesce(n.summary, ''),
                n.last_accessed = row.now,
                n.name = row.name,
                n.symbol_kind = row.symbol_kind,
                n.symbol_line = row.symbol_line,
                n.symbol_signature = row.symbol_signature,
                n.symbol_parent = row.symbol_parent,
                n.symbol_decorator = row.symbol_decorator
            """,
            {"rows": rows, "sc": STRUCTURE_SOURCE_CONFIDENCE},
        )
        return len(rows)

    # ------------------------------------------------------------------
    # Private — edge writes
    # ------------------------------------------------------------------

    def _build_contains_edges(self, scan: ProjectScanResult) -> list[dict[str, str]]:
        """Build CONTAINS edge list from project→dirs and dirs→files."""
        edges: list[dict[str, str]] = []

        # Project → top-level dirs
        for d in scan.directories:
            parent = "/".join(d.rel_path.rstrip("/").split("/")[:-1])
            source_path = parent if parent else "."
            edges.append({"source_path": source_path, "target_path": d.rel_path})

        # Dir → files
        for f in scan.files:
            parts = f.rel_path.replace("\\", "/").split("/")
            if len(parts) > 1:
                parent_dir = "/".join(parts[:-1])
            else:
                parent_dir = "."
            edges.append({"source_path": parent_dir, "target_path": f.rel_path})

        return edges

    def _write_edges_batch(
        self,
        rel_type: str,
        edges: list[dict[str, str]],
        project_name: str,
    ) -> int:
        if not edges:
            return 0
        # Neo4j doesn't support parameterized relationship types, so we use
        # separate queries per type (all are known at compile time).
        query = f"""
            UNWIND $edges AS edge
            MATCH (a:Entity {{structure_project: $project, structure_path: edge.source_path}})
            MATCH (b:Entity {{structure_project: $project, structure_path: edge.target_path}})
            MERGE (a)-[r:{rel_type}]->(b)
            ON CREATE SET r.source = 'project-scan', r.created_at = datetime()
            RETURN count(r) AS cnt
        """
        rows = self.neo4j.execute(query, {"edges": edges, "project": project_name})
        return int(rows[0].get("cnt", 0)) if rows else 0

    def _write_calls_edge(
        self,
        source_project: str,
        ref: Any,
        session_id: str,
        user_id: str,
        now: str,
    ) -> None:
        """Write a CALLS edge between two project entities.

        Creates the target project entity if it doesn't exist yet.
        """
        self.neo4j.execute(
            _INFERRED_PROJECT_TARGET_CYPHER
            + """
            MATCH (source:Entity {structure_project: $source_name, structure_path: '.', structure_role: 'project'})
            MERGE (source)-[r:CALLS]->(target)
            ON CREATE SET r.source = 'project-scan', r.mechanism = $mechanism, r.evidence = $evidence, r.created_at = datetime()
            ON MATCH SET r.mechanism = $mechanism, r.evidence = $evidence
            """,
            {
                "source_name": source_project,
                "target_name": ref.target_project,
                "uuid": str(uuid4()),
                "target_project_id": _inferred_project_id(ref.target_project),
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
                "mechanism": ref.mechanism,
                "evidence": ref.evidence,
                "sc_inferred": SOURCE_CONFIDENCE_AGENT,
            },
        )

    def _write_contains_repo_edge(
        self,
        umbrella_project: str,
        nested: Any,
        session_id: str,
        user_id: str,
        now: str,
    ) -> None:
        """Write a CONTAINS_REPO edge from an umbrella project to a nested repository.

        Deliberately NOT `CONTAINS`: that is the within-repo project→directory→file chain that
        `blast_radius` traverses, and reusing it would let impact analysis cross a repository
        boundary -- reintroducing in edge form the coupling the scan boundary removes. Also not
        `CALLS`, which means a runtime/import dependency; containing a repo implies no such
        thing.

        Like `_write_calls_edge`, the target project entity is MERGEd, so an umbrella may be
        scanned before its children and the edges still land; each child fills in its own
        detail when scanned directly.
        """
        self.neo4j.execute(
            _INFERRED_PROJECT_TARGET_CYPHER
            + """
            MATCH (source:Entity {structure_project: $source_name, structure_path: '.', structure_role: 'project'})
            MERGE (source)-[r:CONTAINS_REPO]->(target)
            ON CREATE SET r.source = 'project-scan', r.rel_path = $rel_path, r.created_at = datetime()
            ON MATCH SET r.rel_path = $rel_path
            """,
            {
                "source_name": umbrella_project,
                "target_name": nested.name,
                "rel_path": nested.rel_path,
                "uuid": str(uuid4()),
                "target_project_id": _inferred_project_id(nested.name),
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
                "sc_inferred": SOURCE_CONFIDENCE_AGENT,
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _symbol_path(sym: SymbolEntry) -> str:
    """Compute the fully qualified structure_path for a symbol node.

    Delegates to the shared `domain.utils.symbol_structure_path` (SSOT-12) so
    this and `project_scanner._sym_path` can never independently drift.
    """
    return symbol_structure_path(sym.file_path, sym.name, sym.parent)
