"""MCP tool: query_structure — query the structural project graph."""

from __future__ import annotations

import json
import os

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope

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


def _coverage_caveat(data: dict) -> str | None:
    """Incompleteness warning for a project whose index is truncated or unverified.

    Three states, not two. `known=False` means the project was scanned before coverage was
    recorded, so completeness is UNVERIFIED -- it must never be reported as complete. Every
    project scanned under the old rules is in exactly that state and is in fact truncated, so
    treating unknown as complete would turn this tool's output into a false all-clear.
    """
    cov = data.get("coverage") or {}
    if not cov.get("known"):
        return (
            "COVERAGE UNVERIFIED: this project was indexed before coverage tracking existed, "
            "so completeness is unknown and it is probably truncated. A negative result below "
            "is not evidence of absence. Re-scan with ingest_project(path=..., force=True)."
        )
    if not cov.get("partial_index"):
        return None
    indexed, eligible = cov.get("files_indexed"), cov.get("files_eligible")
    ratio = f" ({indexed}/{eligible} files indexed)" if indexed and eligible else ""
    return (
        f"INCOMPLETE: this project is only partially indexed{ratio}, so results below may be "
        "missing entries. A negative result here is not evidence of absence."
    )


def _negative_qualifier(coverage: dict | None) -> str:
    """Suffix appended to any 'nothing found' message so it never reads as proven absence.

    Completeness-sensitive queries (files, imports, tests, endpoints, symbols, cross-refs) all
    answer from the same index. An empty result means "not in the index", which equals "does
    not exist" only when the index is known complete.
    """
    cov = coverage or {}
    if not cov.get("known"):
        return (
            " — NOTE: index completeness is unverified for this project (scanned before "
            "coverage tracking), so this is not evidence of absence. Re-scan with "
            "ingest_project(force=True)."
        )
    if cov.get("partial_index"):
        return (
            f" — NOTE: this project is only partially indexed "
            f"({cov.get('files_indexed')}/{cov.get('files_eligible')} files), so this is not "
            "evidence of absence."
        )
    return " (project fully indexed)"


def _unindexed_refusal(data: dict, project: str, what: str) -> str | None:
    """Refusal text when the requested paths are not in the index at all, else None."""
    missing = data.get("unindexed_paths") or []
    if not missing:
        return None
    cov = data.get("coverage") or {}
    indexed, eligible = cov.get("files_indexed"), cov.get("files_eligible")
    ratio = f" Project index holds {indexed}/{eligible} eligible files." if indexed and eligible else ""
    return (
        f"Cannot answer {what} for {project}: not indexed.\n\n"
        + "\n".join(f"  ? {p}" for p in missing)
        + f"\n\nThese paths are absent from the structure graph, so no dependency, test, or "
        f"impact conclusion can be drawn about them -- an empty result would be misleading "
        f"rather than reassuring.{ratio}\n"
        "Re-scan with ingest_project(path=..., force=True), then retry."
    )


def _format_blast_radius(data: dict, project: str) -> str:
    refusal = _unindexed_refusal(data, project, "blast radius")
    if refusal:
        return refusal

    lines = [f"Blast radius for {project}:"]
    caveat = _coverage_caveat(data)
    if caveat:
        lines.append(f"\n{caveat}")

    lines.append(f"\nChanged files ({len(data['changed'])}):")
    for f in data["changed"]:
        lines.append(f"  * {f}")

    if data["directly_affected"]:
        lines.append(f"\nDirectly affected ({len(data['directly_affected'])}):")
        for f in data["directly_affected"]:
            lines.append(f"  <- {f}")

    if data["transitively_affected"]:
        lines.append(f"\nTransitively affected ({len(data['transitively_affected'])}):")
        for f in data["transitively_affected"]:
            lines.append(f"  <<- {f}")

    if data["affected_tests"]:
        lines.append(f"\nAffected tests ({len(data['affected_tests'])}):")
        for t in data["affected_tests"]:
            lines.append(f"  {t['test']} (covers {t['covers']})")
    else:
        lines.append(
            "\nAffected tests: none found" + _negative_qualifier(data.get("coverage"))
        )

    if data["cross_project_refs"]:
        lines.append(f"\nCross-project impact:")
        for r in data["cross_project_refs"]:
            lines.append(f"  -> {r['target']} via {r['mechanism']}")

    fn_callers = data.get("function_callers", [])
    if fn_callers:
        lines.append(f"\nFunction-level callers ({len(fn_callers)}):")
        for fc in fn_callers[:20]:
            lines.append(
                f"  {fc['caller_file']}::{fc['caller']} → calls {fc['callee']}"
            )

    memories = data.get("related_memories", [])
    if memories:
        lines.append(f"\nRelated memories ({len(memories)}):")
        for m in memories:
            prov = f" [{m['anchor_source']}]" if m.get("anchor_source") else ""
            lines.append(
                f"  [{m['name']}] {m.get('preview', '')}{prov}  (via {m['linked_file']})"
            )

    total = (
        len(data["changed"])
        + len(data["directly_affected"])
        + len(data["transitively_affected"])
    )
    lines.append(f"\nTotal impact: {total} files, {len(data['affected_tests'])} tests")
    return "\n".join(lines)


def _format_affected_tests(data: dict) -> str:
    lines = []

    if not data["test_files"]:
        missing = data.get("unindexed_paths") or []
        partial = (data.get("coverage") or {}).get("partial_index")
        if missing:
            lines.append(
                "Cannot determine the affected tests: the changed files are not in the "
                "structure graph."
            )
            lines.extend(f"  ? {p}" for p in missing)
            lines.append(
                "\nAn empty test set here reflects a missing index, not untested code. "
                "Re-scan with ingest_project(path=..., force=True), then retry."
            )
        elif partial:
            cov = data.get("coverage") or {}
            lines.append(
                "Cannot determine the affected tests: this project is only partially "
                f"indexed ({cov.get('files_indexed')}/{cov.get('files_eligible')} files), so "
                "test mappings may be missing."
            )
        else:
            lines.append("No specific tests found for the changed files.")
        lines.append("Recommendation: run full test suite.")
        lines.append(f"\n  {data['test_command']}")
        return "\n".join(lines)

    lines.append(f"Test selector for {len(data['changed_files'])} changed file(s):")

    if data["affected_source_files"]:
        lines.append(
            f"\nAlso affected ({len(data['affected_source_files'])} downstream files):"
        )
        for f in data["affected_source_files"][:20]:
            lines.append(f"  <- {f}")
        if len(data["affected_source_files"]) > 20:
            lines.append(f"  ... and {len(data['affected_source_files']) - 20} more")

    lines.append(f"\nTests to run ({len(data['test_files'])}):")
    for t in data["test_files"]:
        lines.append(f"  {t}")

    lines.append(f"\nCommand:\n  {data['test_command']}")
    return "\n".join(lines)


def _format_symbols(data: dict, path: str, project: str) -> str:
    symbols = data.get("symbols", [])
    truncated = data.get("truncated", False)
    scope = path if path else project
    if not symbols:
        return f"No symbols found for {scope}."
    lines = [
        f"Symbols in {scope} ({len(symbols)})"
        + (" [TRUNCATED — per-file cap hit]" if truncated else "")
        + ":"
    ]
    for s in symbols:
        parent = s.get("parent", "")
        prefix = f"  {parent}." if parent else "  "
        kind_tag = f" [{s['kind']}]" if s.get("kind") != "function" else ""
        dec = f" @{s['decorator']}" if s.get("decorator") else ""
        doc = f"  # {s['doc']}" if s.get("doc") else ""
        lines.append(
            f"{prefix}{s['sig']}{kind_tag}{dec}{doc}  (line {s.get('line', '?')})"
        )
    return "\n".join(lines)


def _format_context(data: dict) -> str:
    if "error" in data:
        return f"Error: {data['error']}"
    lines = [f"Context for {data.get('path', '')}:"]
    summary = data.get("summary", "")
    if summary:
        lines.append(f"\nSummary: {summary}")
    symbols = data.get("symbols", [])
    truncated = data.get("truncated", False)
    lines.append(
        f"\nSymbols ({len(symbols)})" + (" [TRUNCATED]" if truncated else "") + ":"
    )
    if symbols:
        for s in symbols:
            parent = s.get("parent", "")
            prefix = f"  {parent}." if parent else "  "
            dec = f" @{s['decorator']}" if s.get("decorator") else ""
            doc = f"  # {s['doc']}" if s.get("doc") else ""
            lines.append(f"{prefix}{s['sig']}{dec}{doc}  (line {s.get('line', '?')})")
    else:
        lines.append("  (none)")
    imports = data.get("imports", [])
    if imports:
        lines.append(f"\nImports ({len(imports)}):")
        for p in imports:
            lines.append(f"  → {p}")
    imported_by = data.get("imported_by", [])
    if imported_by:
        lines.append(f"\nImported by ({len(imported_by)}):")
        for p in imported_by:
            lines.append(f"  ← {p}")
    tested_by = data.get("tested_by", [])
    if tested_by:
        lines.append(f"\nTests ({len(tested_by)}):")
        for p in tested_by:
            lines.append(f"  ✓ {p}")
    return "\n".join(lines)


def _format_overview(data: dict) -> str:
    lines = [
        f"Project: {data['project']}",
        f"Stack: {data.get('stack', 'unknown')}",
        f"Description: {data.get('description', '')}",
        "",
        "Entities:",
    ]
    for role, count in sorted(data.get("entities", {}).items()):
        lines.append(f"  {role}: {count}")
    lines.append("")
    lines.append("Edges:")
    for rel, count in sorted(data.get("edges", {}).items()):
        lines.append(f"  {rel}: {count}")

    contained = data.get("contains_repos") or []
    if contained:
        lines.append("")
        lines.append(f"Contains repositories ({len(contained)}):")
        for c in contained:
            loc = f" at {c['rel_path']}" if c.get("rel_path") else ""
            lines.append(f"  {c['name']}{loc}")
        lines.append(
            "  (nested repos are scanned as their own projects; query them by name)"
        )

    cov = data.get("coverage") or {}
    if cov.get("known"):
        lines.append("")
        lines.append("Coverage:")
        lines.append(f"  discovered: {cov.get('files_discovered')}")
        lines.append(f"  eligible:   {cov.get('files_eligible')}")
        lines.append(f"  indexed:    {cov.get('files_indexed')}")
        if cov.get("partial_index"):
            lines.append(
                "  PARTIAL — the cap dropped eligible files. Negative structural answers "
                "for this project are not evidence of absence."
            )
        else:
            lines.append("  complete (every eligible file indexed)")
    return "\n".join(lines)


def _format_unknown_project(project: str, projects: list[dict]) -> str:
    lines = [
        f"Project '{project}' is not ingested in the structural graph.",
        "Run ingest_project with the repo's absolute path before relying on query_structure results.",
    ]
    known = [str(p.get("name", "")) for p in projects if p.get("name")]
    if known:
        sample = ", ".join(sorted(known)[:8])
        more = len(known) - min(len(known), 8)
        suffix = f", ... (+{more} more)" if more > 0 else ""
        lines.append(f"Known projects: {sample}{suffix}")
    lines.append("Example: ingest_project(path=\"/path/to/your-project\", name=\"your-project\")")
    return "\n".join(lines)
