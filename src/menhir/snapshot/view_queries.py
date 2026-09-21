"""Read structural data from the published canonical snapshot view.

Snapshot nodes are intentionally separate from the local structure graph. Every match in this
module is constrained to the current published root and its project id; a relationship whose
other endpoint belongs to another root is therefore invisible even if such a corrupt edge exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CANONICAL_VIEW_KEY = "canonical"
SNAPSHOT_STATUS_KEY = "__snapshot_status__"
DEGRADED_WARNING = "Canonical snapshot view is degraded; results may be unreliable."


@dataclass(frozen=True)
class ResolvedSnapshotView:
    project_id: str
    view_key: str
    snapshot_id: str
    generation: int
    current_root: str
    degraded: bool


class SnapshotStructureViewReader:
    """Resolve and query one published snapshot structure view."""

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j

    def list_projects(self) -> list[dict[str, Any]]:
        """List published snapshot projects in the legacy project-list shape."""
        rows = list(
            self.neo4j.execute(
                "MATCH (v:CanonicalView {view_key: $view_key}) "
                "MATCH (r:ViewRoot {root_id: v.current_root, project_id: v.project_id, "
                "view_key: v.view_key}) "
                "OPTIONAL MATCH (p:SnapshotEntity {view_root: r.root_id, "
                "project_id: v.project_id, structure_role: 'project'}) "
                "RETURN coalesce(v.display_name, v.project_id) AS name, "
                "v.project_id AS project_id, coalesce(p.stack, '') AS stack, "
                "coalesce(p.content, '') AS description, "
                "coalesce(v.degraded, false) AS degraded ORDER BY name, project_id",
                {"view_key": CANONICAL_VIEW_KEY},
            )
        )
        return [
            {
                "name": str(row.get("name") or row.get("project_id") or ""),
                "project_id": str(row.get("project_id") or ""),
                "stack": str(row.get("stack") or ""),
                "description": str(row.get("description") or ""),
                "snapshot": True,
                "degraded": bool(row.get("degraded") or False),
            }
            for row in rows
        ]

    def resolve(
        self, project: str, *, allow_display_name: bool = True
    ) -> ResolvedSnapshotView | None:
        """Resolve an exact project id, optionally followed by an unambiguous display name."""
        exact = list(
            self.neo4j.execute(
                "MATCH (v:CanonicalView {project_id: $project, view_key: $view_key}) "
                "OPTIONAL MATCH (r:ViewRoot {root_id: v.current_root}) "
                "WHERE r.project_id = v.project_id AND r.view_key = v.view_key "
                "RETURN v.project_id AS project_id, v.view_key AS view_key, "
                "v.current_root AS current_root, coalesce(v.generation, 0) AS generation, "
                "coalesce(v.degraded, false) AS degraded, "
                "coalesce(r.snapshot_id, '') AS snapshot_id, true AS exact_match "
                "LIMIT 1",
                {"project": project, "view_key": CANONICAL_VIEW_KEY},
            )
        )
        if exact:
            return self._resolved(exact[0])
        if not allow_display_name:
            return None

        named = list(
            self.neo4j.execute(
                "MATCH (v:CanonicalView {display_name: $project, view_key: $view_key}) "
                "OPTIONAL MATCH (r:ViewRoot {root_id: v.current_root}) "
                "WHERE r.project_id = v.project_id AND r.view_key = v.view_key "
                "RETURN v.project_id AS project_id, v.view_key AS view_key, "
                "v.current_root AS current_root, coalesce(v.generation, 0) AS generation, "
                "coalesce(v.degraded, false) AS degraded, "
                "coalesce(r.snapshot_id, '') AS snapshot_id, false AS exact_match "
                "ORDER BY project_id",
                {"project": project, "view_key": CANONICAL_VIEW_KEY},
            )
        )
        if len(named) > 1:
            raise ValueError(f"Ambiguous canonical snapshot display name: {project}")
        return self._resolved(named[0]) if named else None

    @staticmethod
    def _resolved(row: dict[str, Any]) -> ResolvedSnapshotView | None:
        current_root = str(row.get("current_root") or "")
        if not current_root:
            return None
        return ResolvedSnapshotView(
            project_id=str(row.get("project_id") or ""),
            view_key=str(row.get("view_key") or CANONICAL_VIEW_KEY),
            snapshot_id=str(row.get("snapshot_id") or ""),
            generation=int(row.get("generation") or 0),
            current_root=current_root,
            degraded=bool(row.get("degraded") or False),
        )

    def query(
        self,
        project: str,
        query_type: str,
        *,
        allow_display_name: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Return a snapshot envelope, or None when no published view exists."""
        view = self.resolve(project, allow_display_name=allow_display_name)
        if view is None:
            return None

        if query_type == "overview":
            data = self._overview(view, project)
        elif query_type == "files":
            data = self._files(view, str(kwargs.get("path_filter") or ""))
        elif query_type == "imports":
            data = self._imports(view, str(kwargs.get("file_path") or ""))
        elif query_type == "tests":
            data = self._tests(view, str(kwargs.get("file_path") or ""))
        elif query_type == "endpoints":
            data = self._endpoints(view)
        elif query_type == "dependencies":
            data = self._dependencies(view)
        elif query_type == "cross_refs":
            data = []
        elif query_type == "blast_radius":
            data = self._blast_radius(
                view,
                list(kwargs.get("file_paths") or []),
                int(kwargs.get("max_depth") or 5),
            )
        elif query_type == "affected_tests":
            data = self._affected_tests(
                view,
                list(kwargs.get("file_paths") or []),
                int(kwargs.get("max_depth") or 5),
            )
        elif query_type == "symbols":
            data = self._symbols(view, str(kwargs.get("path") or ""))
        elif query_type == "context":
            data = self._context(view, str(kwargs.get("path") or ""))
        else:
            raise ValueError(f"Unknown structure query type: {query_type}")

        return {
            SNAPSHOT_STATUS_KEY: {
                "project_id": view.project_id,
                "view_key": view.view_key,
                "snapshot_id": view.snapshot_id,
                "generation": view.generation,
                "degraded": view.degraded,
                "warning": DEGRADED_WARNING if view.degraded else "",
            },
            "data": data,
        }

    @staticmethod
    def _params(view: ResolvedSnapshotView, **extra: Any) -> dict[str, Any]:
        return {"root": view.current_root, "pid": view.project_id, **extra}

    def _coverage(self, view: ResolvedSnapshotView) -> dict[str, Any]:
        rows = list(
            self.neo4j.execute(
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'project'}) "
                "RETURN n.files_discovered AS discovered, n.files_eligible AS eligible, "
                "n.files_indexed AS indexed, n.partial_index AS partial LIMIT 1",
                self._params(view),
            )
        )
        if not rows:
            return {"known": False, "partial_index": False}
        row = rows[0]
        return {
            "known": row.get("indexed") is not None,
            "files_discovered": row.get("discovered"),
            "files_eligible": row.get("eligible"),
            "files_indexed": row.get("indexed"),
            "partial_index": bool(row.get("partial")),
        }

    def _overview(self, view: ResolvedSnapshotView, project: str) -> dict[str, Any]:
        entity_rows = list(
            self.neo4j.execute(
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid}) "
                "RETURN n.structure_role AS role, count(n) AS count ORDER BY role",
                self._params(view),
            )
        )
        edge_rows = list(
            self.neo4j.execute(
                "MATCH (a:SnapshotEntity {view_root: $root, project_id: $pid})-[rel]->"
                "(b:SnapshotEntity {view_root: $root, project_id: $pid}) "
                "RETURN type(rel) AS rel, count(rel) AS count ORDER BY rel",
                self._params(view),
            )
        )
        project_rows = list(
            self.neo4j.execute(
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'project'}) "
                "RETURN n.content AS description, n.stack AS stack LIMIT 1",
                self._params(view),
            )
        )
        meta = project_rows[0] if project_rows else {}
        return {
            "project": project,
            "description": str(meta.get("description") or ""),
            "stack": str(meta.get("stack") or ""),
            "entities": {
                str(row["role"]): int(row.get("count") or 0)
                for row in entity_rows
                if row.get("role") is not None
            },
            "edges": {
                str(row["rel"]): int(row.get("count") or 0)
                for row in edge_rows
                if row.get("rel") is not None
            },
            "coverage": self._coverage(view),
            "contains_repos": [],
        }

    def _files(self, view: ResolvedSnapshotView, path_filter: str) -> list[dict[str, Any]]:
        rows = list(
            self.neo4j.execute(
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid}) "
                "WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test'] "
                "AND ($prefix = '' OR n.structure_path STARTS WITH $prefix) "
                "RETURN n.structure_path AS path, n.structure_role AS role, "
                "n.content AS description, coalesce(n.hot_count, 0) AS hot_count "
                "ORDER BY path",
                self._params(view, prefix=path_filter),
            )
        )
        return [
            {
                "path": str(row.get("path") or ""),
                "role": str(row.get("role") or ""),
                "description": str(row.get("description") or ""),
                **(
                    {"hot_count": int(row.get("hot_count") or 0)}
                    if int(row.get("hot_count") or 0) > 0
                    else {}
                ),
            }
            for row in rows
        ]

    def _imports(self, view: ResolvedSnapshotView, file_path: str) -> dict[str, list[str]]:
        outgoing = list(
            self.neo4j.execute(
                "MATCH (a:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: $path})-[:IMPORTS]->"
                "(b:SnapshotEntity {view_root: $root, project_id: $pid}) "
                "RETURN b.structure_path AS path ORDER BY path",
                self._params(view, path=file_path),
            )
        )
        incoming = list(
            self.neo4j.execute(
                "MATCH (a:SnapshotEntity {view_root: $root, project_id: $pid})-[:IMPORTS]->"
                "(b:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: $path}) "
                "RETURN a.structure_path AS path ORDER BY path",
                self._params(view, path=file_path),
            )
        )
        return {
            "imports": [str(row["path"]) for row in outgoing],
            "imported_by": [str(row["path"]) for row in incoming],
        }

    def _tests(self, view: ResolvedSnapshotView, file_path: str) -> list[dict[str, str]]:
        rows = list(
            self.neo4j.execute(
                "MATCH (t:SnapshotEntity {view_root: $root, project_id: $pid})-[:TESTS]->"
                "(s:SnapshotEntity {view_root: $root, project_id: $pid}) "
                "WHERE $path = '' OR s.structure_path = $path "
                "RETURN t.structure_path AS test_file, s.structure_path AS source_file "
                "ORDER BY test_file",
                self._params(view, path=file_path),
            )
        )
        return [
            {"test": str(row["test_file"]), "source": str(row["source_file"])}
            for row in rows
        ]

    def _endpoints(self, view: ResolvedSnapshotView) -> list[dict[str, str]]:
        rows = list(
            self.neo4j.execute(
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'endpoint'}) "
                "RETURN n.name AS name, n.content AS description, "
                "n.structure_path AS path ORDER BY name",
                self._params(view),
            )
        )
        return [
            {
                "name": str(row.get("name") or ""),
                "description": str(row.get("description") or ""),
                "path": str(row.get("path") or ""),
            }
            for row in rows
        ]

    def _dependencies(self, view: ResolvedSnapshotView) -> list[str]:
        rows = list(
            self.neo4j.execute(
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'dependency'}) RETURN n.name AS name ORDER BY name",
                self._params(view),
            )
        )
        return [str(row["name"]) for row in rows]

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _indexed_paths(
        self, view: ResolvedSnapshotView, file_paths: list[str]
    ) -> set[str]:
        variants: dict[str, str] = {}
        for path in file_paths:
            variants.setdefault(self._normalize_path(path), path)
        if not variants:
            return set()
        rows = list(
            self.neo4j.execute(
                "UNWIND $paths AS path "
                "MATCH (n:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: path}) RETURN DISTINCT n.structure_path AS path",
                self._params(view, paths=list(variants)),
            )
        )
        return {
            variants[str(row["path"])]
            for row in rows
            if str(row.get("path") or "") in variants
        }

    def _blast_radius(
        self, view: ResolvedSnapshotView, file_paths: list[str], max_depth: int
    ) -> dict[str, Any]:
        rows = list(
            self.neo4j.execute(
                "UNWIND $paths AS changed_path "
                "MATCH path = (importer:SnapshotEntity {view_root: $root, project_id: $pid})"
                "-[:IMPORTS*1..]->(changed:SnapshotEntity {view_root: $root, "
                "project_id: $pid, structure_path: changed_path}) "
                "WHERE length(path) <= $depth AND ALL(n IN nodes(path) WHERE "
                "n.view_root = $root AND n.project_id = $pid) "
                "RETURN DISTINCT importer.structure_path AS importer_path, "
                "length(path) AS hop_distance",
                self._params(view, paths=file_paths, depth=max_depth),
            )
        )
        changed = set(file_paths)
        direct: set[str] = set()
        transitive: set[str] = set()
        for row in rows:
            importer = row.get("importer_path")
            if not importer or importer in changed:
                continue
            if int(row.get("hop_distance") or 1) == 1:
                direct.add(str(importer))
            else:
                transitive.add(str(importer))
        transitive -= direct
        impacted = sorted(changed | direct | transitive)

        test_rows = list(
            self.neo4j.execute(
                "UNWIND $paths AS source_path "
                "MATCH (t:SnapshotEntity {view_root: $root, project_id: $pid})-[:TESTS]->"
                "(s:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: source_path}) "
                "RETURN DISTINCT t.structure_path AS test_file, s.structure_path AS covers "
                "ORDER BY test_file",
                self._params(view, paths=impacted),
            )
        )
        caller_rows = list(
            self.neo4j.execute(
                "UNWIND $paths AS changed_path "
                "MATCH (f:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: changed_path})-[:DEFINES]->"
                "(callee:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'symbol'}) "
                "MATCH (caller:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'symbol'})-[:CALLS]->(callee) "
                "MATCH (caller_file:SnapshotEntity {view_root: $root, project_id: $pid})"
                "-[:DEFINES]->(caller) "
                "WHERE caller_file.structure_path <> changed_path "
                "AND NOT caller_file.structure_path IN $paths "
                "RETURN DISTINCT caller.name AS caller_name, caller.symbol_parent AS caller_class, "
                "caller_file.structure_path AS caller_file, callee.name AS callee_name, "
                "callee.symbol_parent AS callee_class ORDER BY caller_file, caller_name LIMIT 30",
                self._params(view, paths=file_paths),
            )
        )
        indexed = self._indexed_paths(view, file_paths)
        return {
            "changed": sorted(changed),
            "directly_affected": sorted(direct),
            "transitively_affected": sorted(transitive),
            "affected_tests": [
                {"test": str(row["test_file"]), "covers": str(row["covers"])}
                for row in test_rows
                if row.get("test_file")
            ],
            "cross_project_refs": [],
            "related_memories": [],
            "open_todos": [],
            "function_callers": [
                {
                    "caller": (
                        f"{row['caller_class']}.{row['caller_name']}"
                        if row.get("caller_class")
                        else str(row.get("caller_name") or "")
                    ),
                    "caller_file": str(row.get("caller_file") or ""),
                    "callee": (
                        f"{row['callee_class']}.{row['callee_name']}"
                        if row.get("callee_class")
                        else str(row.get("callee_name") or "")
                    ),
                }
                for row in caller_rows
                if row.get("caller_file")
            ],
            "unindexed_paths": sorted(path for path in file_paths if path not in indexed),
            "coverage": self._coverage(view),
        }

    def _affected_tests(
        self, view: ResolvedSnapshotView, file_paths: list[str], max_depth: int
    ) -> dict[str, Any]:
        if not file_paths:
            return {"changed_files": [], "test_files": [], "test_command": "pytest"}
        radius = self._blast_radius(view, file_paths, max_depth)
        test_files = sorted({row["test"] for row in radius["affected_tests"]})
        command = (
            "pytest " + " ".join(test_files)
            if test_files
            else "pytest  # no specific tests found \u2014 run full suite"
        )
        return {
            "changed_files": file_paths,
            "affected_source_files": sorted(
                set(radius["directly_affected"]) | set(radius["transitively_affected"])
            ),
            "test_files": test_files,
            "test_command": command,
            "unindexed_paths": radius.get("unindexed_paths", []),
            "coverage": radius.get("coverage", {}),
        }

    def _symbols(self, view: ResolvedSnapshotView, path: str) -> dict[str, Any]:
        if not path:
            where = ""
            params = self._params(view)
        elif path.endswith("/"):
            where = "AND sym.structure_path STARTS WITH $prefix "
            params = self._params(view, prefix=path)
        else:
            where = "AND sym.structure_path STARTS WITH $prefix "
            params = self._params(view, prefix=path + "::")
        rows = list(
            self.neo4j.execute(
                "MATCH (sym:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_role: 'symbol'}) "
                + where
                + "RETURN sym.name AS name, sym.symbol_kind AS kind, "
                "sym.symbol_signature AS sig, sym.content AS doc, sym.symbol_line AS line, "
                "sym.symbol_parent AS parent, sym.symbol_decorator AS decorator "
                "ORDER BY sym.structure_path, sym.symbol_line",
                params,
            )
        )
        if not path or path.endswith("/"):
            trunc_where = "AND ($prefix = '' OR f.structure_path STARTS WITH $prefix) "
            trunc_params = self._params(view, prefix=path)
        else:
            trunc_where = "AND f.structure_path = $prefix "
            trunc_params = self._params(view, prefix=path)
        truncated_rows = list(
            self.neo4j.execute(
                "MATCH (f:SnapshotEntity {view_root: $root, project_id: $pid}) "
                + trunc_where
                + "RETURN count(CASE WHEN coalesce(f.symbols_truncated, false) "
                "THEN 1 END) > 0 AS any_truncated",
                trunc_params,
            )
        )
        return {
            "symbols": [self._symbol(row) for row in rows],
            "truncated": bool(
                truncated_rows[0].get("any_truncated", False) if truncated_rows else False
            ),
        }

    @staticmethod
    def _symbol(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(row.get("name") or ""),
            "kind": str(row.get("kind") or ""),
            "sig": str(row.get("sig") or ""),
            "doc": str(row.get("doc") or ""),
            "line": int(row.get("line") or 0),
            "parent": str(row.get("parent") or ""),
            "decorator": str(row.get("decorator") or ""),
        }

    def _context(self, view: ResolvedSnapshotView, path: str) -> dict[str, Any]:
        if not path:
            raise ValueError("path is required for context query")
        file_rows = list(
            self.neo4j.execute(
                "MATCH (f:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: $path}) "
                "RETURN f.content AS summary, "
                "coalesce(f.symbols_truncated, false) AS truncated LIMIT 1",
                self._params(view, path=path),
            )
        )
        if not file_rows:
            return {"error": f"File not found in structure graph: {path}"}
        symbols = self._symbols(view, path)
        imports = self._imports(view, path)
        tested_rows = list(
            self.neo4j.execute(
                "MATCH (t:SnapshotEntity {view_root: $root, project_id: $pid})-[:TESTS]->"
                "(f:SnapshotEntity {view_root: $root, project_id: $pid, "
                "structure_path: $path}) RETURN t.structure_path AS path ORDER BY path",
                self._params(view, path=path),
            )
        )
        row = file_rows[0]
        return {
            "path": path,
            "summary": str(row.get("summary") or ""),
            "truncated": bool(row.get("truncated") or symbols["truncated"]),
            "symbols": symbols["symbols"],
            "imports": imports["imports"],
            "imported_by": imports["imported_by"],
            "tested_by": [str(item["path"]) for item in tested_rows],
        }
