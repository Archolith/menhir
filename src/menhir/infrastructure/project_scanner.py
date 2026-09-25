"""Deterministic project directory scanner for structural graph ingestion."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from menhir.infrastructure.project_scanner_edges import (
    _detect_cross_project_refs,
    _detect_endpoints,
    _detect_test_edges,
    _find_sibling_projects,
    _parse_imports,
    _resolve_module,
)
from menhir.infrastructure.project_scanner_metadata import (
    _first_paragraph,
    _parse_dependencies,
    _parse_gradle_deps,
    _parse_node_deps,
    _parse_python_deps,
    _read_project_description,
)
from menhir.infrastructure.project_scanner_models import (
    CallEdge,
    CrossProjectRef,
    DirEntry,
    EndpointEntry,
    FileEntry,
    ImportEdge,
    NestedRepo,
    ProjectScanResult,
    SymbolEntry,
    TestEdge,
)
from menhir.infrastructure.project_scanner_rules import (
    SCANNER_SCHEMA_VERSION,
    _AGENT_ORIENTATION_DOCS,
    _ALWAYS_SKIP_DIRS,
    _BEACON_ROOT_FILES,
    _BEACON_ROOT_PREFIXES,
    _BINARY_EXTENSIONS,
    _CODE_EXTENSIONS,
    _CONFIG_EXTENSIONS,
    _CONFIG_NAMES,
    _DOC_EXTENSIONS,
    _ENTRYPOINT_NAMES,
    _KNOWN_PORTS,
    _MANIFEST_PATTERN,
    _MAX_FILE_BYTES,
    _MAX_KEY_FILES,
    _ROOT_ONLY_SKIP_DIRS,
    _STACK_SIGNALS,
    _WINDOWS_RESERVED,
    _cap_files,
    _classify_file_role,
    _detect_stack,
    _infer_description,
    _infer_dir_purpose,
    _is_beacon_root_artifact,
    _is_independent_clone,
    _is_nested_repo,
    _is_root_level,
    _load_gitignore,
    _matches_gitignore,
    is_eligible_file,
)
from menhir.infrastructure.project_scanner_symbols import (
    _MAX_CALLS_PER_SYMBOL,
    _SYMBOL_PER_FILE_CAP,
    _build_import_name_map,
    _build_signature,
    _detect_decorator,
    _extract_call_edges,
    _extract_module_docstring,
    _extract_symbols,
    _make_symbol,
    _sym_path,
    _walk_no_inner_defs,
)

# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class ProjectScanner:
    """Scans a project directory and produces a structured result."""

    def scan(self, root: str | Path, name: str | None = None) -> ProjectScanResult:
        root = Path(root).resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        project_name = name or root.name
        gitignore_patterns = _load_gitignore(root)

        # Walk and collect
        all_rel_paths: list[str] = []
        dir_set: set[str] = set()
        file_entries: list[FileEntry] = []
        nested_repos: list[NestedRepo] = []

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip dirs in-place. Root-only artifact names are pruned solely at the
            # repository root -- see _ROOT_ONLY_SKIP_DIRS for why they must not apply at depth.
            at_root = os.path.abspath(dirpath) == os.path.abspath(str(root))
            kept_dirs: list[str] = []
            for d in dirnames:
                full_d = os.path.join(dirpath, d)
                if d in _ALWAYS_SKIP_DIRS or (at_root and d in _ROOT_ONLY_SKIP_DIRS):
                    continue
                if _is_nested_repo(full_d):
                    # Boundary, not a subtree: record the containment, do not descend.
                    #
                    # THIS MUST PRECEDE THE GITIGNORE CHECK. An umbrella repo gitignores its
                    # sub-repositories -- that is how an umbrella is set up -- so testing
                    # gitignore first prunes the directory as merely ignored and the boundary is
                    # never recorded. That silently disabled containment for 47 of 49 nested
                    # repos across the four umbrellas once root-anchored patterns began matching.
                    # Being gitignored by the parent is evidence FOR separate-repo status, not
                    # against it. Cache/artifact directories are already gone via the skip-dirs
                    # check above.
                    #
                    # Descent stops for every boundary, but only an independent clone is
                    # recorded as containment -- a worktree is another checkout of a repo the
                    # graph already holds.
                    if _is_independent_clone(full_d):
                        rel = os.path.relpath(full_d, root).replace("\\", "/")
                        nested_repos.append(
                            NestedRepo(rel_path=rel, name=os.path.basename(rel))
                        )
                    continue
                if _matches_gitignore(full_d, root, gitignore_patterns):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs

            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            if rel_dir != ".":
                dir_set.add(rel_dir)

            for fname in filenames:
                # Skip Windows reserved device names (nul, con, etc.)
                if os.name == "nt" and fname.split(".")[0].lower() in _WINDOWS_RESERVED:
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if _is_beacon_root_artifact(rel):
                    continue
                if _matches_gitignore(full, root, gitignore_patterns):
                    continue
                all_rel_paths.append(rel)

        # Build per-file mtime map + project fingerprint (single walk)
        file_mtimes: dict[str, float] = {}
        fingerprint_lines: list[str] = []
        for rp in sorted(all_rel_paths):
            try:
                mtime = os.path.getmtime(root / rp)
                file_mtimes[rp] = mtime
                fingerprint_lines.append(f"{rp}:{mtime}")
            except OSError:
                fingerprint_lines.append(rp)
        # Schema version participates in the fingerprint so a change to eligibility, priority,
        # or cap semantics invalidates stored fingerprints and forces a re-scan. Without it the
        # path+mtime hash is identical and ingest skips as "unchanged", leaving every existing
        # graph permanently truncated under the old rules.
        fingerprint_lines.append(f"__scanner_schema__:{SCANNER_SCHEMA_VERSION}")
        scan_fingerprint = hashlib.sha256("\n".join(fingerprint_lines).encode()).hexdigest()

        # Classify files into roles
        for rp in all_rel_paths:
            role = _classify_file_role(rp)
            desc = _infer_description(rp, role)
            # For plain Python source files, populate description from module docstring
            if role == "file" and rp.endswith(".py"):
                doc = _extract_module_docstring(str(root / rp))
                if doc:
                    desc = doc
            file_entries.append(FileEntry(
                rel_path=rp,
                role=role,
                description=desc,
                file_mtime=file_mtimes.get(rp, 0.0),
            ))

        # Eligibility (step 2/3 of the deny-list; step 1 ran during the walk), then cap.
        files_discovered = len(file_entries)
        file_entries = [f for f in file_entries if is_eligible_file(f.rel_path, f.role)]
        files_eligible = len(file_entries)
        file_entries = _cap_files(file_entries, _MAX_KEY_FILES, project=project_name)
        files_indexed = len(file_entries)

        # Build directory entries
        directories = [DirEntry(rel_path=d, purpose=_infer_dir_purpose(d)) for d in sorted(dir_set)]

        # Detect stack
        stack = _detect_stack(root)

        # Read description
        description = _read_project_description(root)

        # Parse dependencies
        dependencies = _parse_dependencies(root, stack)

        result = ProjectScanResult(
            name=project_name,
            root_path=str(root),
            stack=stack,
            description=description,
            directories=directories,
            files=file_entries,
            dependencies=dependencies,
            scan_fingerprint=scan_fingerprint,
            files_discovered=files_discovered,
            files_eligible=files_eligible,
            files_indexed=files_indexed,
            nested_repos=sorted(nested_repos, key=lambda r: r.rel_path),
        )

        # Parse imports, test edges, endpoints, cross-project refs
        result.imports = _parse_imports(root, file_entries, stack)
        result.test_edges = _detect_test_edges(file_entries)
        result.endpoints = _detect_endpoints(root, file_entries, stack)
        result.cross_project_refs = _detect_cross_project_refs(root, file_entries, project_name)

        # Extract symbols from Python files (only for Python projects or any file with .py extension)
        for fe in result.files:
            if not fe.rel_path.endswith(".py"):
                continue
            syms, truncated = _extract_symbols(str(root / fe.rel_path), fe.rel_path)
            if truncated:
                fe.symbols_truncated = True
                result.truncated_symbol_files.append(fe.rel_path)
            result.symbols.extend(syms)

        # Build function-level call graph from extracted symbols
        if result.symbols and stack == "python":
            tl_lookup: dict[tuple[str, str], str] = {}
            method_lookup: dict[tuple[str, str, str], str] = {}
            for sym in result.symbols:
                path = _sym_path(sym)
                if sym.kind == "function":
                    tl_lookup[(sym.file_path, sym.name)] = path
                elif sym.kind == "method":
                    method_lookup[(sym.file_path, sym.parent, sym.name)] = path
            import_name_map = _build_import_name_map(root, file_entries, stack)
            for fe in result.files:
                if not fe.rel_path.endswith(".py"):
                    continue
                file_import_names = import_name_map.get(fe.rel_path, {})
                result.call_edges.extend(
                    _extract_call_edges(
                        str(root / fe.rel_path), fe.rel_path,
                        tl_lookup, method_lookup, file_import_names,
                    )
                )

        return result
