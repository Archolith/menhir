"""Result formatters for the query_structure MCP tool.

Extracted from ``query_structure``: the pure dict-to-text renderers the tool's dispatch
hands query results to -- blast radius, affected tests, symbols, context, overview, and
unknown project -- plus the coverage caveats and unindexed-path refusals they embed.
"""

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
