"""Document- and symbol-level content reads for the structure graph."""

from __future__ import annotations

from typing import Any

def _set_properties(record: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    """Return the optional node properties that are actually set, as strings.

    Neo4j returns an unset property as a PRESENT key whose value is ``None``, so
    ``str(record.get(key, default))`` yields the truthy string ``"None"`` and defeats every
    downstream ``or default`` fallback. Scanner-indexed documents carry no ``document_type``,
    which is how generated Beacon manifests came to publish ``role: None`` (PR #125 F1). An unset
    property is therefore omitted, never stringified; readers apply their own documented default.
    """
    return {key: str(record[key]) for key in keys if record.get(key) is not None}


class StructureGraphContentMixin:
    """Mixin part of ``StructureGraphWriter`` carrying document/symbol/context reads."""

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
                **_set_properties(r, ("description", "root_path", "doc_type")),
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
                **_set_properties(r, ("root_path", "doc_type")),
            }
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
