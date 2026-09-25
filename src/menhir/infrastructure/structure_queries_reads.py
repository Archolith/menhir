"""Read queries over the structure graph used by the query_structure MCP tool."""

from __future__ import annotations

from typing import Any


class StructureGraphReadsMixin:
    """Mixin part of ``StructureGraphWriter`` carrying aggregate read queries."""

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
                RETURN n.content AS description, n.stack AS stack,
                       n.indexed_description AS indexed_description
                LIMIT 1
            }
            RETURN raw_entities, raw_edges, description, stack, indexed_description
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

        indexed_description = row.get("indexed_description")
        return {
            "project": project,
            "description": row.get("description") or "",
            # None when the project node predates the property (or is absent): the reader
            # cannot tell an empty scanner result from an unrecorded one, so it says so.
            "indexed_description": (
                None if indexed_description is None else str(indexed_description)
            ),
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
