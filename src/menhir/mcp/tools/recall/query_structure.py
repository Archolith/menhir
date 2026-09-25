"""MCP tool: query_structure — query the structural project graph."""

from __future__ import annotations

import json
import os

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope
from menhir.mcp.tools.recall.query_structure_formatting import (
    _coverage_caveat,
    _format_affected_tests,
    _format_blast_radius,
    _format_context,
    _format_overview,
    _format_symbols,
    _format_unknown_project,
    _negative_qualifier,
    _unindexed_refusal,
)

# Read-time existence check on a project's root_path — the structural-graph analogue of
# stale_labeling.py's file-anchor advisory. The structural graph reflects the last
# ingest_project scan, not live filesystem state: a project whose root_path was deleted
# or moved keeps returning its last-known files/symbols/imports with no signal that the
# checkout is gone (e.g. after a branch merge that removed the worktree). Label-only —
# does not filter, delete, or refuse the query; the caller decides whether to trust it.
STRUCT_STALE_REASON = "project_root_missing"
STRUCT_STALE_ACTION = "verify_project_path_or_reingest"
STRUCT_STALE_ADVISORY = (
    "root_path no longer exists on disk. This reflects the last ingest_project scan, not "
    "live filesystem state — files/symbols/imports below may be gone, moved, or merged "
    "elsewhere (e.g. a branch merged into another checkout). Verify the path before "
    "relying on this, or re-run ingest_project if the project moved."
)


def _root_status(entry: dict) -> str:
    """Classify a project by whether its recorded root still exists on disk.

    Three states, not two. A project node with NO `root_path` is not evidence that the root is
    fine -- it is a project that was never scanned. Those nodes are created as MERGE targets by
    the CONTAINS_REPO and CALLS writers, which record a name and nothing else, and they also
    predate root_path being persisted. Collapsing "unrecorded" into "ok" is what let
    `menhir` keep 3,199 entities while passing every staleness check.

    Returns "ok", "missing" (root recorded, directory gone), or "unscanned" (no root recorded).
    """
    root = str(entry.get("root_path", ""))
    if not root:
        return "unscanned"
    return "ok" if os.path.isdir(root) else "missing"


def _root_missing(projects: list[dict], project: str) -> str:
    """Return the stale project's root_path if its directory no longer exists, else ""."""
    for p in projects:
        if str(p.get("name", "")) == project:
            root = str(p.get("root_path", ""))
            return root if _root_status(p) == "missing" else ""
    return ""


async def query_structure(
    query_type: str,
    project: str = "",
    path: str = "",
    namespace: str = "",
) -> str:
    """Query the structural code graph for project layout, files, imports, tests, and endpoints.

    Use this tool to understand project structure, find files, trace imports,
    identify test coverage, list endpoints, or see cross-project dependencies.

    Args:
        query_type: What to query. One of:
            - "projects" — list all ingested projects
            - "overview" — project summary with entity/edge counts
            - "files" — list files (optionally filtered by path prefix)
            - "imports" — what a file imports and what imports it (requires path)
            - "tests" — test→source mappings (optionally filtered to a source file via path)
            - "endpoints" — all MCP tools and HTTP routes exposed by the project
            - "dependencies" — external packages the project depends on
            - "cross_refs" — cross-project references (CALLS edges)
            - "blast_radius" — trace impact of changed files (path = comma-separated file paths)
            - "affected_tests" — minimal test set for changed files (path = comma-separated file paths)
            - "symbols" — list functions/classes/methods (path = exact file, dir prefix with /, or empty for all)
            - "context" — full picture of one file: summary + symbols + imports + tests (requires path)
        project: Project name (e.g. "menhir"). Required for all query types except "projects".
        path: File path filter. Used by "files" (prefix filter), "imports" (exact file), "tests" (source file),
            "blast_radius" and "affected_tests" (comma-separated list of changed file paths),
            "symbols" (exact file, dir prefix with trailing /, or empty), "context" (exact file).

    Returns:
        Structured text result with the requested information.
    """
    return await QueryStructureTool().execute(
        query_type=query_type, project=project, path=path, namespace=namespace
    )


class QueryStructureTool(BaseTextTool):
    name = "query_structure"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Query Structure"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Query the structural code graph for project layout, files, imports, tests, endpoints, "
        "and cross-project dependencies."
    )

    async def endpoint(
        self,
        query_type: str,
        project: str = "",
        path: str = "",
        namespace: str = "",
    ) -> str:
        """Query the structural code graph for project layout, files, imports, tests, and endpoints.

        Args:
            query_type: One of: projects, overview, files, imports, tests, endpoints, dependencies,
                cross_refs, blast_radius, affected_tests, symbols, context.
            project: Project name (required except for "projects").
            path: File path filter. For symbols: exact file, dir prefix (trailing /), or empty.
                For context: exact file path.

        Returns:
            Structured text result with the requested information.
        """
        backend = self.get_backend()

        if query_type == "projects":
            projects = await backend.query_structure("", "projects")
            if not projects:
                return "No projects ingested yet. Use ingest_project to scan a project first."
            lines = ["Ingested projects:"]
            stale_names: list[str] = []
            unscanned_names: list[str] = []
            partial_names: list[str] = []
            for p in projects:
                status = _root_status(p)
                if status == "missing":
                    stale_names.append(str(p["name"]))
                elif status == "unscanned":
                    unscanned_names.append(str(p["name"]))
                tag = {
                    "missing": " [STALE: root_path missing]",
                    "unscanned": " [NEVER SCANNED: no root_path]",
                }.get(status, "")
                if p.get("partial_index"):
                    partial_names.append(str(p["name"]))
                    tag += (
                        f" [PARTIAL INDEX: {p.get('files_indexed')}/"
                        f"{p.get('files_eligible')} files]"
                    )
                lines.append(
                    f"  {p['name']} ({p.get('stack', '?')}){tag} — {p.get('description', '')[:80]}"
                )
            if partial_names:
                lines.append("")
                lines.append(f"PARTIAL INDEX ({len(partial_names)}): {', '.join(partial_names)}")
                lines.append(
                    "  The file cap dropped eligible files in these projects. A negative "
                    "structural answer (no dependents, no tests) is NOT evidence of absence "
                    "for them. Re-scan with ingest_project(force=True) after raising the cap."
                )
            if stale_names:
                lines.append("")
                lines.append(f"STALE ({len(stale_names)}): {', '.join(stale_names)}")
                lines.append(f"  {STRUCT_STALE_ADVISORY}")
            if unscanned_names:
                lines.append("")
                lines.append(
                    f"NEVER SCANNED ({len(unscanned_names)}): {', '.join(unscanned_names)}"
                )
                lines.append(
                    "  These project nodes record no root_path, so whether the project still "
                    "exists on disk cannot be checked. Most are name-only stubs created by the "
                    "CONTAINS_REPO/CALLS writers. Run ingest_project against the real path to "
                    "resolve, or treat their entities as unverified."
                )
            # Rows must carry an `entities` count to be orphan rows. A backend that does not
            # implement this query type can echo back some other payload, and a diagnostic
            # banner must never be the thing that breaks the listing it is appended to.
            orphans = [
                o
                for o in (await backend.query_structure("", "orphan_structure_projects") or [])
                if isinstance(o, dict) and "entities" in o and o.get("name")
            ]
            if orphans:
                lines.append("")
                lines.append(
                    f"NO PROJECT NODE ({len(orphans)}): "
                    + ", ".join(f"{o['name']} ({o['entities']} entities)" for o in orphans)
                )
                lines.append(
                    "  Entities carry these structure_project values but no project entity "
                    "exists, so they appear in no listing and no staleness check reaches them. "
                    "Re-ingest to restore the project node, or delete the orphaned entities."
                )
            return "\n".join(lines)

        if not project:
            return "Error: project name is required for this query type."

        projects = await backend.query_structure("", "projects")
        known_projects = {str(p.get("name", "")) for p in projects}
        if project not in known_projects:
            return _format_unknown_project(project, projects)

        # Coverage for THIS project, derived from the listing already fetched above rather
        # than a second round trip. Every completeness-sensitive answer below is qualified
        # with it, because "not in the index" only means "does not exist" when the index is
        # known complete.
        _meta = next((p for p in projects if str(p.get("name", "")) == project), {})
        coverage = {
            "known": _meta.get("files_indexed") is not None,
            "files_eligible": _meta.get("files_eligible"),
            "files_indexed": _meta.get("files_indexed"),
            "partial_index": bool(_meta.get("partial_index")),
        }
        neg = _negative_qualifier(coverage)

        stale_root = _root_missing(projects, project)
        stale_banner = (
            f"[STALE] {project} {STRUCT_STALE_ADVISORY} (root_path={stale_root})\n\n"
            if stale_root
            else ""
        )

        try:
            text = await self._dispatch(
                query_type, project, path, backend, namespace, neg=neg
            )
        except ValueError as e:
            return f"Error: {e}"
        return stale_banner + text if stale_banner else text

    async def _dispatch(
        self,
        query_type: str,
        project: str,
        path: str,
        backend,
        namespace: str = "",
        *,
        neg: str = "",
    ) -> str:
            """`neg` is the coverage qualifier appended to every 'nothing found' message.

            It is passed explicitly rather than read from the enclosing scope: `_dispatch` is
            a separate method, and referencing a bare name defined only in `endpoint` raises
            NameError on every call down that branch -- the exact defect that shipped silently
            in the `blast_radius`/`namespace` path (see CHANGELOG 2026-08-07).
            """
            if query_type == "overview":
                result = await backend.query_structure(project, "overview")
                return _format_overview(result)

            if query_type == "files":
                kwargs = {"path_filter": path} if path else {}
                files = await backend.query_structure(project, "files", kwargs)
                if not files:
                    return (
                        f"No files found in {project}{neg}"
                        + (f" matching '{path}'" if path else "")
                        + "."
                    )
                lines = [
                    f"Files in {project}"
                    + (f" matching '{path}'" if path else "")
                    + f" ({len(files)}):"
                ]
                for f in files:
                    role_tag = f" [{f['role']}]" if f["role"] != "file" else ""
                    desc = f.get("description", "")
                    desc_tag = f"  — {desc}" if desc and desc != f["path"] else ""
                    heat = f.get("hot_count", 0)
                    heat_tag = f" [hot:{heat}]" if heat else ""
                    lines.append(f"  {f['path']}{role_tag}{heat_tag}{desc_tag}")
                return "\n".join(lines)

            if query_type == "imports":
                if not path:
                    return "Error: path is required for imports query (the file to inspect)."
                result = await backend.query_structure(
                    project, "imports", {"file_path": path}
                )
                lines = [f"Import graph for {path}:"]
                if result["imports"]:
                    lines.append(f"  Imports ({len(result['imports'])}):")
                    for p in result["imports"]:
                        lines.append(f"    → {p}")
                else:
                    lines.append(f"  Imports: (none found){neg}")
                if result["imported_by"]:
                    lines.append(f"  Imported by ({len(result['imported_by'])}):")
                    for p in result["imported_by"]:
                        lines.append(f"    ← {p}")
                else:
                    lines.append(f"  Imported by: (none found){neg}")
                return "\n".join(lines)

            if query_type == "tests":
                kwargs = {"file_path": path} if path else {}
                tests = await backend.query_structure(project, "tests", kwargs)
                if not tests:
                    return (
                        f"No test mappings found{neg}"
                        + (f" for {path}" if path else f" in {project}")
                        + "."
                    )
                lines = [
                    f"Test coverage"
                    + (f" for {path}" if path else f" in {project}")
                    + f" ({len(tests)}):"
                ]
                for t in tests:
                    lines.append(f"  {t['test']} → {t['source']}")
                return "\n".join(lines)

            if query_type == "endpoints":
                eps = await backend.query_structure(project, "endpoints")
                if not eps:
                    return f"No endpoints found in {project}.{neg}"
                lines = [f"Endpoints in {project} ({len(eps)}):"]
                for ep in eps:
                    lines.append(f"  {ep['name']} — {ep.get('description', '')}")
                return "\n".join(lines)

            if query_type == "dependencies":
                deps = await backend.query_structure(project, "dependencies")
                if not deps:
                    return f"No dependencies found in {project}."
                return f"Dependencies for {project} ({len(deps)}): {', '.join(deps)}"

            if query_type == "cross_refs":
                refs = await backend.query_structure(project, "cross_refs")
                if not refs:
                    return f"No cross-project references found for {project}.{neg}"
                lines = [f"Cross-project references from {project} ({len(refs)}):"]
                for r in refs:
                    lines.append(
                        f"  → {r['target']} via {r.get('mechanism', '?')} ({r.get('evidence', '')})"
                    )
                return "\n".join(lines)

            if query_type == "blast_radius":
                if not path:
                    return "Error: path is required for blast_radius query (comma-separated file paths)."
                file_paths = [p.strip() for p in path.split(",") if p.strip()]
                # namespace scopes only the open-TODO section of the result, which
                # reads :Todo through :TodoLocation. Omitted keeps the historical
                # cross-silo behavior, matching list_todos/get_todo.
                result = await backend.query_structure(
                    project,
                    "blast_radius",
                    {"file_paths": file_paths, "namespace": namespace or None},
                )
                return _format_blast_radius(result, project)

            if query_type == "affected_tests":
                if not path:
                    return "Error: path is required for affected_tests query (comma-separated file paths)."
                file_paths = [p.strip() for p in path.split(",") if p.strip()]
                result = await backend.query_structure(
                    project, "affected_tests", {"file_paths": file_paths}
                )
                return _format_affected_tests(result)

            if query_type == "symbols":
                result = await backend.query_structure(
                    project, "symbols", {"path": path}
                )
                return _format_symbols(result, path, project)

            if query_type == "context":
                if not path:
                    return (
                        "Error: path is required for context query (exact file path)."
                    )
                result = await backend.query_structure(
                    project, "context", {"path": path}
                )
                return _format_context(result)

            if query_type == "documents":
                # path = optional path_filter, doc_type = optional document_type filter
                kwargs = {"path": path} if path else {}
                result = await backend.query_structure(project, "documents", kwargs)
                if not result:
                    return f"No documents found for {project}."
                lines = [f"Documents for {project} ({len(result)}):"]
                for d in result:
                    tag = (
                        f" [{d.get('doc_type', 'generic')}]"
                        if d.get("doc_type", "generic") != "generic"
                        else ""
                    )
                    lines.append(f"  {d['path']}{tag}")
                return "\n".join(lines)

            return (
                f"Unknown query_type: {query_type}. Use one of: projects, overview, files, imports, "
                f"tests, endpoints, dependencies, cross_refs, blast_radius, affected_tests, "
                f"symbols, context, documents."
            )
