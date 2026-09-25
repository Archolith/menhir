"""Public write API for the structure graph: full-scan merge and document ingest."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from menhir.infrastructure.project_scanner import ProjectScanResult
from menhir.infrastructure.structure_queries_common import STRUCTURE_SOURCE

logger = logging.getLogger(__name__)


class StructureGraphWritesMixin:
    """Mixin part of ``StructureGraphWriter`` carrying the public write methods."""

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
        # #99. Read once and thread it through every read-back and every prune below, so the rows
        # this scan deletes are the rows the identity gate authorised it to write -- rather than
        # whatever currently answers to the same display name.
        project_id = getattr(scan, "project_id", None)

        # 0. Incremental diff — compare stored file mtimes to determine which
        #    files actually changed so we can skip symbol rewrites for the rest.
        stored_mtimes = self.get_file_mtimes(scan.name, project_id)
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
            # Same capacity-vs-destruction rule as the stale-role prune below: on a
            # truncated scan, absence from scan.files is the cap's doing, not deletion.
            if deleted_paths and not scan.partial_index:
                self._delete_file_entities(scan.name, list(deleted_paths), project_id)
            elif deleted_paths and scan.partial_index:
                logger.info(
                    "Skipping incremental file prune for project=%s: scan truncated, "
                    "%d stored paths absent from the scan map are not evidence of deletion",
                    scan.name,
                    len(deleted_paths),
                )
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
                # The description exactly as the scanner read it from .agent/README.md or
                # CLAUDE.md -- empty when neither exists. `content` above substitutes a
                # display placeholder ("python project") for readability; consumers that must
                # not present an invented purpose as indexed fact (Beacon generation) read
                # this property instead (PR #125 F4).
                "indexed_description": scan.description or "",
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
        #     identified purely by absence from the current scan. Same capacity rule as
        #     every other prune: a truncated scan is not evidence of absence.
        if not scan.partial_index:
            stale_dirs = self._delete_stale_directories(
                scan.name, [d.rel_path for d in scan.directories], project_id
            )
            if stale_dirs:
                logger.info(
                    "Pruned %d stale directory entities for project=%s",
                    stale_dirs,
                    scan.name,
                )
        else:
            logger.info(
                "Skipping stale-directory pruning for project=%s: scan truncated, "
                "absence from this scan is not evidence of deletion",
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
                project_id,
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

        # 3c. Prune scanner-written `document` entities the scan no longer sees (PR #125 F5).
        #
        # The A0 orientation docs (`.agent/README.md`, `.agent/architecture.md`, ...) are
        # written through the same batch MERGE as files but carry the `document` role, which
        # the multi-role file prune above deliberately excludes: `ingest_document` writes the
        # same role with absolute-path keys and the `document-ingest` source label, and those
        # are not the scan's to delete. Restricting the prune to STRUCTURE_SOURCE (passed as a
        # parameter, never a literal) makes this the scanner pruning only what the scanner wrote. Same capacity rule: a truncated scan is
        # not evidence of absence. An empty keep-list on a complete scan means the project has
        # no orientation docs any more, and every stale one goes -- the single-role prune's
        # documented semantics, shared with endpoints and dependencies.
        if not scan.partial_index:
            stale_docs = self._delete_stale_role_entities(
                scan.name,
                "document",
                [f.rel_path for f in scan.files if f.role == "document"],
                project_id,
                source=STRUCTURE_SOURCE,
            )
            if stale_docs:
                logger.info(
                    "Pruned %d stale scanner document entities for project=%s",
                    stale_docs,
                    scan.name,
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
                scan.name, "dependency", [f"dep:{dep}" for dep in scan.dependencies],
                project_id,
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
                scan.name, "endpoint", [f"endpoint:{ep.name}" for ep in scan.endpoints],
                project_id,
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
                scan.name, [n.rel_path for n in nested_repos], project_id
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
