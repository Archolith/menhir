"""Private write helpers for the structure graph: symbol, entity and edge merges."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from menhir.domain.utils import symbol_structure_path
from menhir.infrastructure.project_scanner import ProjectScanResult, SymbolEntry
from menhir.infrastructure.structure_queries_common import (
    _ENTITY_DEFAULTS,
    STRUCTURE_SOURCE_CONFIDENCE,
)

logger = logging.getLogger(__name__)


def _symbol_path(sym: SymbolEntry) -> str:
    """Compute the fully qualified structure_path for a symbol node.

    Delegates to the shared `domain.utils.symbol_structure_path` (SSOT-12) so
    this and `project_scanner._sym_path` can never independently drift.
    """
    return symbol_structure_path(sym.file_path, sym.name, sym.parent)


class StructureGraphInternalsMixin:
    """Mixin part of ``StructureGraphWriter`` carrying batch merge internals."""

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
            # Full replace — delete all existing symbols for the project.
            #
            # #99 applies here too, and this is the sharpest instance of it in the file: the
            # statement carries no path filter, so keyed on the display name alone a first scan
            # (or any `force`) deleted every symbol row answering to that name, whichever identity
            # owned it. The issue enumerated six helpers and missed this one and its incremental
            # sibling below; the invariant is what binds, not the list.
            for owner in self._owner_arms("sym", project_id):
                self.neo4j.execute(
                    f"""
                    MATCH (sym:Entity {{structure_role: 'symbol'}})
                    WHERE {owner}
                    DETACH DELETE sym
                    """,
                    self._owner_params(project_name, project_id),
                )
            symbols_to_write = symbols
            truncated_to_mark = truncated_files
        else:
            # Incremental — only delete symbols for files that changed
            if changed_paths:
                for owner in self._owner_arms("f", project_id):
                    self.neo4j.execute(
                        f"""
                        UNWIND $paths AS path
                        MATCH (f:Entity {{structure_path: path}})
                        WHERE {owner}
                        MATCH (f)-[:DEFINES]->(sym:Entity {{structure_role: 'symbol'}})
                        DETACH DELETE sym
                        """,
                        {
                            **self._owner_params(project_name, project_id),
                            "paths": list(changed_paths),
                        },
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
