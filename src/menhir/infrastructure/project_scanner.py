"""Deterministic project directory scanner for structural graph ingestion."""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from menhir.domain.utils import symbol_structure_path
from menhir.infrastructure.text_io import read_text_utf8

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DirEntry:
    rel_path: str
    purpose: str = ""


@dataclass
class FileEntry:
    rel_path: str
    role: str = "file"          # file | entrypoint | config | test | endpoint
    description: str = ""
    symbols_truncated: bool = False
    file_mtime: float = 0.0     # mtime at scan time — used for incremental write diff


@dataclass
class ImportEdge:
    source_path: str            # importing file (relative)
    target_path: str            # imported file (relative)


@dataclass
class TestEdge:
    test_path: str
    source_path: str


@dataclass
class EndpointEntry:
    name: str                   # e.g. "add_memory", "/api/cards"
    file_path: str
    kind: str = "unknown"       # mcp_tool | http_route | cli_command


@dataclass
class CrossProjectRef:
    target_project: str
    mechanism: str              # http | shared_db | import
    evidence: str


@dataclass
class NestedRepo:
    """A git repository nested inside the scanned one — a boundary, not a subtree.

    Recorded rather than silently dropped: declining to index another repo's files is correct,
    but the containment itself is a true structural fact and the only interesting thing about
    an umbrella directory.
    """
    rel_path: str               # umbrella-relative, e.g. "menhir"
    name: str                   # directory basename == the child's project name on ingest


@dataclass
class SymbolEntry:
    file_path: str              # relative to project root
    name: str                   # unqualified function/class name
    kind: str                   # "function" | "class" | "method"
    line_no: int
    signature: str              # "def foo(x: int) -> bool"
    docstring: str              # first line of docstring, or ""
    parent: str                 # class name if method, else ""
    decorator: str = ""         # "property" | "classmethod" | "staticmethod" | ""


@dataclass
class CallEdge:
    caller_path: str            # fully qualified symbol path: file::Class.method or file::func
    callee_path: str            # fully qualified symbol path of callee


@dataclass
class ProjectScanResult:
    name: str
    root_path: str
    stack: str
    description: str
    directories: list[DirEntry] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    imports: list[ImportEdge] = field(default_factory=list)
    test_edges: list[TestEdge] = field(default_factory=list)
    endpoints: list[EndpointEntry] = field(default_factory=list)
    cross_project_refs: list[CrossProjectRef] = field(default_factory=list)
    scan_fingerprint: str = ""
    #: CF-257. The identity this scan writes under, settled before the write and carried here so
    #: every writer under `write_project` stamps it without threading a parameter through four
    #: batch helpers. Declared rather than attached dynamically so it survives `asdict` across the
    #: transport boundary -- dropping it there broke the deprecated compat writer entirely.
    project_id: str | None = None
    #: The claim generation the identity was settled under (CF-257). Carried on the scan so the
    #: write boundary can prove the binding has not changed hands since; an id alone cannot.
    identity_generation: int | None = None
    symbols: list[SymbolEntry] = field(default_factory=list)
    truncated_symbol_files: list[str] = field(default_factory=list)
    call_edges: list[CallEdge] = field(default_factory=list)
    # Coverage accounting. Three counts, not one ratio: `files_indexed / files_discovered`
    # would mark every project permanently partial once documentation is intentionally
    # excluded. Truncation is `files_indexed < files_eligible`.
    files_discovered: int = 0
    files_eligible: int = 0
    files_indexed: int = 0
    nested_repos: list[NestedRepo] = field(default_factory=list)

    @property
    def partial_index(self) -> bool:
        """True when the cap dropped eligible files, so the index is incomplete.

        Derived, not stored: `dataclasses.asdict` serializes fields only, so this does NOT
        appear in the JSON payload crossing the backend boundary. That is intentional -- the
        three counts are the source of truth and this is recomputed on reconstruction. Read it
        off the object or recompute from the counts; never expect `payload["partial_index"]`.
        """
        return self.files_indexed < self.files_eligible


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALWAYS_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs", "egg-info", ".idea",
    ".vscode", ".gradle", ".next", "target", ".ruff_cache", "htmlcov",
}

# Artifact directories pruned ONLY at the repository root. These names are too generic to
# prune by basename at any depth: `_ALWAYS_SKIP_DIRS` is matched against the bare directory
# name during the walk, so putting "logs" there would delete a legitimate `src/<pkg>/logs/`
# package from the index of every project that has one.
_ROOT_ONLY_SKIP_DIRS = {"results", "logs", "coverage"}

_CONFIG_EXTENSIONS = {".toml", ".yaml", ".yml", ".json", ".cfg", ".ini"}
_CONFIG_NAMES = {
    "CLAUDE.md", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile", "Taskfile.yml", "Procfile",
}

_ENTRYPOINT_NAMES = {"__main__.py", "main.py", "app.py", "bot.py", "server.py", "manage.py", "cli.py"}

# Runaway guard, not a routine constraint. Sized to sit well above a large real project's
# eligible-file count (menhir: 845) so it does not bind in normal operation. When it DOES bind,
# `partial_index` is set and consumers refuse to answer completeness-sensitive queries rather
# than reporting absence as fact.
_MAX_KEY_FILES = 2000
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB — skip files larger than this to avoid memory bloat

# Bumped whenever eligibility, priority, or cap semantics change. Folded into the scan
# fingerprint so a rules change invalidates every stored fingerprint and forces a re-scan --
# otherwise the path+mtime fingerprint is unchanged, ingest skips as "unchanged", and existing
# graphs keep their truncated state forever.
SCANNER_SCHEMA_VERSION = 5

# --- Eligibility: an ordered deny-list. First match wins. ---------------------------------
# Step 2 (preserve) runs BEFORE step 3 (extension exclusions) so that structural manifests are
# not lost to a blanket extension rule -- `CLAUDE.md` to `.md`, `uv.lock` to `.lock`, and most
# importantly `requirements.txt` to `.txt`, which `_parse_dependencies` reads line by line.
_MANIFEST_PATTERN = re.compile(
    r"^(requirements.*\.txt|constraints.*\.txt|.*\.lock|package-lock\.json)$", re.IGNORECASE
)
_DOC_EXTENSIONS = {".md", ".rst", ".txt", ".log"}
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".so", ".dll", ".dylib", ".pyc", ".pyo", ".bin",
    ".db", ".sqlite", ".sqlite3", ".pma",
}  # NOTE: `.lock` is deliberately absent -- lockfiles are preserved as manifests above.

# Windows reserved device names — cause os.path.relpath / Path() to fail
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Stack detection by config file presence
_STACK_SIGNALS: list[tuple[str, str]] = [
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("package.json", "typescript"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "kotlin"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pom.xml", "java"),
]

# Port → project name mapping for cross-project ref detection
_KNOWN_PORTS: dict[str, str] = {
    "8080": "yawn.rip",
    "8082": "cth.mcp.scheduler",
    "5173": "yawn.dashboard",
    "8787": "menhir",
}

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


# ---------------------------------------------------------------------------
# gitignore
# ---------------------------------------------------------------------------

def _load_gitignore(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return []
    patterns: list[str] = []
    try:
        for line in read_text_utf8(gi).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    except OSError:
        pass
    return patterns


def _matches_gitignore(path: str | os.PathLike[str], root: Path, patterns: list[str]) -> bool:
    rel = os.path.relpath(path, root).replace("\\", "/")
    name = os.path.basename(path)
    for pat in patterns:
        # Directory-only patterns
        clean = pat.rstrip("/")

        # Root-anchored patterns (`/build/`, `/Local State`). Only the trailing slash was
        # stripped before, so a leading one survived into fnmatch and could never match a
        # repo-relative path -- silently discarding a whole class of standard gitignore
        # syntax. Anchored patterns match the repo-relative path ONLY, never a basename at
        # depth: `/Default/` must not swallow `src/pkg/Default/`.
        if clean.startswith("/"):
            anchored = clean.lstrip("/")
            if fnmatch(rel, anchored) or fnmatch(rel, anchored + "/*"):
                return True
            continue

        if fnmatch(name, clean) or fnmatch(rel, clean) or fnmatch(rel, clean + "/*"):
            return True
        if "/" in clean and fnmatch(rel, clean):
            return True
    return False


# ---------------------------------------------------------------------------
# File role classification
# ---------------------------------------------------------------------------

def _classify_file_role(rel_path: str) -> str:
    name = os.path.basename(rel_path)
    parts = rel_path.replace("\\", "/").split("/")

    # Entrypoints
    if name in _ENTRYPOINT_NAMES:
        return "entrypoint"

    # Config files
    _, ext = os.path.splitext(name)
    if name in _CONFIG_NAMES or name.startswith(".env"):
        return "config"
    if ext in _CONFIG_EXTENSIONS and _is_root_level(parts):
        return "config"

    # Test files
    if any(p in ("tests", "test", "__tests__") for p in parts):
        return "test"
    if re.match(r"^test_.*\.py$", name) or re.match(r".*_test\.py$", name):
        return "test"
    if re.match(r".*\.test\.(ts|tsx|js|jsx)$", name):
        return "test"
    if re.match(r".*\.spec\.(ts|tsx|js|jsx)$", name):
        return "test"

    return "file"


def _is_root_level(parts: list[str]) -> bool:
    return len(parts) <= 2


def _infer_description(rel_path: str, role: str) -> str:
    name = os.path.basename(rel_path)
    if role == "entrypoint":
        return f"Entry point: {name}"
    if role == "config":
        return f"Configuration: {name}"
    if role == "test":
        return f"Test file: {name}"
    return ""


def _infer_dir_purpose(rel_dir: str) -> str:
    last = rel_dir.rstrip("/").split("/")[-1]
    purposes = {
        "src": "source code",
        "tests": "test suite",
        "test": "test suite",
        "services": "business logic / services",
        "infrastructure": "adapters and external integrations",
        "domain": "domain models and types",
        "mcp": "MCP server and tools",
        "config": "configuration",
        "api": "API layer",
        "web": "web frontend",
        "scripts": "utility scripts",
        "docs": "documentation",
        "migrations": "database migrations",
        "templates": "templates",
        "static": "static assets",
        "explorer": "explorer UI",
    }
    return purposes.get(last, "")


# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------

def _detect_stack(root: Path) -> str:
    for filename, stack in _STACK_SIGNALS:
        if (root / filename).exists():
            return stack
    return "unknown"


# ---------------------------------------------------------------------------
# Description reading
# ---------------------------------------------------------------------------

def _read_project_description(root: Path) -> str:
    # Try .agent/README.md first
    agent_readme = root / ".agent" / "README.md"
    if agent_readme.is_file():
        try:
            text = read_text_utf8(agent_readme)[:1000]
            # Extract first meaningful paragraph
            return _first_paragraph(text)
        except OSError:
            pass

    # Fall back to CLAUDE.md
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file():
        try:
            text = read_text_utf8(claude_md)[:1000]
            return _first_paragraph(text)
        except OSError:
            pass

    return ""


def _first_paragraph(text: str) -> str:
    lines: list[str] = []
    in_content = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if in_content:
                break
            continue
        if stripped.startswith("#"):
            if in_content:
                break
            continue
        in_content = True
        lines.append(stripped)
    result = " ".join(lines)
    return result[:500] if len(result) > 500 else result


# ---------------------------------------------------------------------------
# Dependency parsing
# ---------------------------------------------------------------------------

def _parse_dependencies(root: Path, stack: str) -> list[str]:
    if stack == "python":
        return _parse_python_deps(root)
    if stack in ("typescript", "javascript"):
        return _parse_node_deps(root)
    if stack in ("java", "kotlin"):
        return _parse_gradle_deps(root)
    return []


def _parse_python_deps(root: Path) -> list[str]:
    # Try pyproject.toml first
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = read_text_utf8(pyproject)
            deps: list[str] = []

            # Strategy 1: PEP 621 inline list — dependencies = ["pkg>=1.0", ...]
            # Collect everything between `dependencies = [` and the closing `]`
            in_inline_deps = False
            for line in text.splitlines():
                stripped = line.strip()
                if re.match(r"^dependencies\s*=\s*\[", stripped):
                    in_inline_deps = True
                    # May have items on the same line
                    for m in re.finditer(r'"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped):
                        deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])
                    if "]" in stripped.split("[", 1)[-1]:
                        in_inline_deps = False
                    continue
                if in_inline_deps:
                    if "]" in stripped:
                        # Last line of the list — parse items before ]
                        for m in re.finditer(r'"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped):
                            deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])
                        in_inline_deps = False
                        continue
                    m = re.match(r'^"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped)
                    if m:
                        deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])

            # Strategy 2: section-based — [project.dependencies] or [dependency-groups]
            if not deps:
                in_section = False
                for line in text.splitlines():
                    if re.match(r"^\[.*dependencies.*\]", line, re.IGNORECASE):
                        in_section = True
                        continue
                    if in_section and line.startswith("["):
                        break
                    if in_section:
                        m = re.match(r'^"?([a-zA-Z0-9_-]+)', line.strip())
                        if m:
                            deps.append(m.group(1))

            if deps:
                return deps
        except OSError:
            pass

    # Fall back to requirements.txt
    reqs = root / "requirements.txt"
    if reqs.is_file():
        try:
            deps = []
            for line in read_text_utf8(reqs).splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                m = re.match(r"^([a-zA-Z0-9_-]+)", line)
                if m:
                    deps.append(m.group(1))
            return deps
        except OSError:
            pass
    return []


def _parse_node_deps(root: Path) -> list[str]:
    pkg = root / "package.json"
    if not pkg.is_file():
        return []
    try:
        import json
        data = json.loads(read_text_utf8(pkg))
        deps: list[str] = []
        for key in ("dependencies", "devDependencies"):
            section = data.get(key, {})
            if isinstance(section, dict):
                deps.extend(section.keys())
        return deps
    except (OSError, json.JSONDecodeError):
        return []


def _parse_gradle_deps(root: Path) -> list[str]:
    for name in ("build.gradle", "build.gradle.kts"):
        gf = root / name
        if not gf.is_file():
            continue
        try:
            text = read_text_utf8(gf)
            deps: list[str] = []
            for m in re.finditer(r"""(?:implementation|api|compileOnly|runtimeOnly)\s*[\('"]\s*([^'")\s]+)""", text):
                deps.append(m.group(1).split(":")[0] if ":" in m.group(1) else m.group(1))
            return deps
        except OSError:
            pass
    return []


# ---------------------------------------------------------------------------
# Import parsing
# ---------------------------------------------------------------------------

def _parse_imports(root: Path, files: list[FileEntry], stack: str) -> list[ImportEdge]:
    if stack != "python":
        return []

    # Build module → rel_path lookup
    module_map: dict[str, str] = {}
    for f in files:
        if f.rel_path.endswith(".py"):
            # Convert path to module: src/menhir/services/foo.py → menhir.services.foo
            mod = f.rel_path.replace("/", ".").replace("\\", ".")
            if mod.endswith(".py"):
                mod = mod[:-3]
            # Strip leading src. if present
            if mod.startswith("src."):
                mod = mod[4:]
            # Also register without __init__
            if mod.endswith(".__init__"):
                module_map[mod[:-9]] = f.rel_path
            module_map[mod] = f.rel_path

    edges: list[ImportEdge] = []
    source_files = [f for f in files if f.rel_path.endswith(".py") and f.role != "config"]

    for f in source_files:
        full_path = root / f.rel_path
        if not full_path.is_file():
            continue
        try:
            text = read_text_utf8(full_path)
        except OSError:
            continue

        for line in text.splitlines():
            line = line.strip()
            # from X.Y import Z
            m = re.match(r"^from\s+([\w.]+)\s+import\s+", line)
            if m:
                mod_name = m.group(1)
                target = _resolve_module(mod_name, module_map)
                if target and target != f.rel_path:
                    edges.append(ImportEdge(source_path=f.rel_path, target_path=target))
                continue
            # import X.Y
            m = re.match(r"^import\s+([\w.]+)", line)
            if m:
                mod_name = m.group(1)
                target = _resolve_module(mod_name, module_map)
                if target and target != f.rel_path:
                    edges.append(ImportEdge(source_path=f.rel_path, target_path=target))

    # Deduplicate
    seen: set[tuple[str, str]] = set()
    deduped: list[ImportEdge] = []
    for e in edges:
        key = (e.source_path, e.target_path)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    return deduped


def _resolve_module(mod_name: str, module_map: dict[str, str]) -> str | None:
    """Try to resolve a dotted module name to a project-relative file path."""
    if mod_name in module_map:
        return module_map[mod_name]
    # Try progressively shorter prefixes
    parts = mod_name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in module_map:
            return module_map[prefix]
    return None


# ---------------------------------------------------------------------------
# Test edge detection
# ---------------------------------------------------------------------------

def _detect_test_edges(files: list[FileEntry]) -> list[TestEdge]:
    test_files = [f for f in files if f.role == "test"]
    source_files = {f.rel_path: f for f in files if f.role not in ("test", "config")}

    # Build basename → rel_path index for source files
    basename_index: dict[str, list[str]] = {}
    for rp in source_files:
        bn = os.path.basename(rp)
        basename_index.setdefault(bn, []).append(rp)

    edges: list[TestEdge] = []
    for tf in test_files:
        name = os.path.basename(tf.rel_path)
        # test_foo.py → foo.py
        m = re.match(r"^test_(.+\.py)$", name)
        if m:
            target_name = m.group(1)
            candidates = basename_index.get(target_name, [])
            if candidates:
                edges.append(TestEdge(test_path=tf.rel_path, source_path=candidates[0]))
                continue
        # foo_test.py → foo.py
        m = re.match(r"^(.+)_test\.py$", name)
        if m:
            target_name = m.group(1) + ".py"
            candidates = basename_index.get(target_name, [])
            if candidates:
                edges.append(TestEdge(test_path=tf.rel_path, source_path=candidates[0]))
                continue
        # foo.test.ts → foo.ts (or .tsx)
        m = re.match(r"^(.+)\.(test|spec)\.(ts|tsx|js|jsx)$", name)
        if m:
            for ext in (m.group(3), "tsx", "ts", "js", "jsx"):
                target_name = f"{m.group(1)}.{ext}"
                candidates = basename_index.get(target_name, [])
                if candidates:
                    edges.append(TestEdge(test_path=tf.rel_path, source_path=candidates[0]))
                    break

    return edges


# ---------------------------------------------------------------------------
# Endpoint detection
# ---------------------------------------------------------------------------

def _detect_endpoints(root: Path, files: list[FileEntry], stack: str) -> list[EndpointEntry]:
    endpoints: list[EndpointEntry] = []

    for f in files:
        if f.role == "test" or f.role == "config":
            continue
        full = root / f.rel_path
        if not full.is_file():
            continue
        try:
            text = read_text_utf8(full)
        except OSError:
            continue

        # Python MCP tools: class Foo(BaseTextTool) or class Foo(BaseJsonTool) with name = "..."
        if f.rel_path.endswith(".py"):
            for m in re.finditer(r'class\s+\w+\(Base(?:Text|Json)Tool\)', text):
                # Find the name attribute
                block = text[m.start():m.start() + 500]
                nm = re.search(r'name\s*=\s*["\']([^"\']+)["\']', block)
                if nm:
                    endpoints.append(EndpointEntry(
                        name=nm.group(1), file_path=f.rel_path, kind="mcp_tool",
                    ))

            # FastAPI routes
            for m in re.finditer(r'@(?:app|router)\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)', text):
                endpoints.append(EndpointEntry(
                    name=f"{m.group(1).upper()} {m.group(2)}", file_path=f.rel_path, kind="http_route",
                ))

        # Java Spring routes
        if f.rel_path.endswith(".java"):
            for m in re.finditer(r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*)?["\']([^"\']+)', text):
                endpoints.append(EndpointEntry(
                    name=m.group(1), file_path=f.rel_path, kind="http_route",
                ))

        # TypeScript/JS routes (Express-style)
        if f.rel_path.endswith((".ts", ".tsx", ".js", ".jsx")):
            for m in re.finditer(r'(?:app|router)\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)', text):
                endpoints.append(EndpointEntry(
                    name=f"{m.group(1).upper()} {m.group(2)}", file_path=f.rel_path, kind="http_route",
                ))

    return endpoints


# ---------------------------------------------------------------------------
# Cross-project reference detection
# ---------------------------------------------------------------------------

def _detect_cross_project_refs(
    root: Path, files: list[FileEntry], self_name: str,
) -> list[CrossProjectRef]:
    refs: list[CrossProjectRef] = []
    seen: set[tuple[str, str]] = set()

    def _add(target: str, mechanism: str, evidence: str) -> None:
        if target == self_name:
            return
        key = (target, mechanism)
        if key not in seen:
            seen.add(key)
            refs.append(CrossProjectRef(target_project=target, mechanism=mechanism, evidence=evidence))

    # Check .env files for port references
    for f in files:
        if not os.path.basename(f.rel_path).startswith(".env"):
            continue
        full = root / f.rel_path
        if not full.is_file():
            continue
        try:
            text = read_text_utf8(full)
        except OSError:
            continue
        for port, proj in _KNOWN_PORTS.items():
            if port in text:
                _add(proj, "http", f"port {port} referenced in {f.rel_path}")

    # Check Python imports for sibling project references
    sibling_projects = _find_sibling_projects(root)
    for f in files:
        if not f.rel_path.endswith(".py"):
            continue
        full = root / f.rel_path
        if not full.is_file():
            continue
        try:
            text = read_text_utf8(full)
        except OSError:
            continue
        for sibling in sibling_projects:
            mod_prefix = sibling.replace(".", "_").replace("-", "_")
            if re.search(rf"\b(?:from|import)\s+{re.escape(mod_prefix)}\b", text):
                _add(sibling, "import", f"import {mod_prefix} in {f.rel_path}")

    # Check for shared DB references (NEO4J_*, POSTGRES_* in env)
    for f in files:
        if not os.path.basename(f.rel_path).startswith(".env"):
            continue
        full = root / f.rel_path
        if not full.is_file():
            continue
        try:
            text = read_text_utf8(full)
        except OSError:
            continue
        if "NEO4J_" in text or "POSTGRES_" in text:
            # This project uses a shared DB — mark as shared_db with unknown targets
            # (other projects with the same env vars will also be detected)
            pass  # Cross-project DB sharing is detected per-pair in Phase 1

    return refs


def _find_sibling_projects(root: Path) -> list[str]:
    """Find sibling project directories (same parent as root)."""
    parent = root.parent
    siblings: list[str] = []
    if not parent.is_dir():
        return siblings
    try:
        for entry in parent.iterdir():
            if entry.is_dir() and entry != root and not entry.name.startswith("."):
                siblings.append(entry.name)
    except OSError:
        pass
    return siblings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CODE_EXTENSIONS = {".py", ".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".cpp", ".h"}


def _is_nested_repo(dir_path: str) -> bool:
    """True when *dir_path* is its own git repository, and therefore a scan boundary.

    A repository is the unit of ingestion: each one is its own project with its own name,
    fingerprint, and graph entities. Descending into a nested repo indexes another project's
    files under the parent's name, duplicating every entity and letting an umbrella directory
    dwarf the real projects inside it -- the `archolith` umbrella walked 10,367 files across
    11 nested repos and truncated at the cap, while each of those repos is ingested properly
    on its own.

    `.git` may be a directory or, in worktrees and submodules, a file -- `exists` covers both.
    Both are boundaries: a worktree holds a second checkout of a repo already ingested under
    its own name, so descending into one duplicates that repo's files under the parent. See
    `_is_independent_clone` for which boundaries additionally count as containment.

    The scan root itself is never tested here, so scanning a repo directly still works.
    """
    return os.path.exists(os.path.join(dir_path, ".git"))


def _is_independent_clone(dir_path: str) -> bool:
    """True when *dir_path* is a repository in its own right, not a worktree of another.

    An independently cloned repo has `.git` as a DIRECTORY. A git worktree (and a submodule)
    carries a `.git` FILE holding a `gitdir:` pointer back into another repo. Both are scan
    boundaries, but only the clone is a distinct project worth a CONTAINS_REPO edge: a worktree
    is a transient, branch-scoped checkout of a repo the graph already has, and recording it
    would attach short-lived directories to the umbrella that disappear without warning.

    Live check across the four umbrellas: 48 independent clones, one worktree
    (`projects/yawn/yawn.rip-market-confidence`).
    """
    return os.path.isdir(os.path.join(dir_path, ".git"))


def is_eligible_file(rel_path: str, role: str) -> bool:
    """Decide whether a discovered file belongs in the structure graph.

    An ordered precedence, first match wins. A flat extension list cannot express this,
    because a file's extension does not determine its structural meaning: `CLAUDE.md` is a
    config, `requirements.txt` is a dependency manifest the scanner parses, and `uv.lock` is a
    lockfile -- all three would be lost to a naive `.md`/`.txt`/`.lock` rule.

    Step 1 (cache/artifact directories) is handled during the walk, not here.
    """
    name = rel_path.rsplit("/", 1)[-1]

    # Step 2 -- preserve structural manifests and anything already classified structural.
    if role in ("config", "entrypoint"):
        return True
    if _MANIFEST_PATTERN.match(name):
        return True

    # Step 3 -- exclude documentation and binaries by extension.
    ext = os.path.splitext(name)[1].lower()
    if ext in _DOC_EXTENSIONS or ext in _BINARY_EXTENSIONS:
        return False

    return True


def _cap_files(files: list[FileEntry], limit: int, *, project: str = "") -> list[FileEntry]:
    """Cap the file list, retaining structural files ahead of tests.

    Eligibility filtering has already removed documentation and binaries, so every file
    reaching here is structural -- there is no `is_code` term, which previously demoted any
    language absent from `_CODE_EXTENSIONS` (SQL, Vue, Svelte, C#, Ruby, PHP) below tests.

    When the cap binds, tests are dropped before source and TESTS edges degrade. That is
    deliberate and still strictly better than the inverse: retaining a test whose target was
    dropped yields an orphan that consumes budget and can never be linked. A binding cap is
    reported via `partial_index` rather than hidden.
    """
    if len(files) <= limit:
        return files

    def sort_key(f: FileEntry) -> tuple[int, int, str]:
        role_priority = {"entrypoint": 0, "file": 1, "config": 2, "test": 3}
        depth = f.rel_path.count("/")
        return (role_priority.get(f.role, 4), depth, f.rel_path)

    files.sort(key=sort_key)
    kept, dropped = files[:limit], files[limit:]
    logger.warning(
        "Structure scan truncated for project=%s: %d eligible, %d indexed, %d dropped "
        "(first dropped: %s). Consumers will report partial_index.",
        project or "<unknown>", len(files), len(kept), len(dropped),
        dropped[0].rel_path if dropped else "-",
    )
    return kept


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

_SYMBOL_PER_FILE_CAP = 200
_MAX_CALLS_PER_SYMBOL = 50  # max resolved calls tracked per function/method


def _sym_path(sym: SymbolEntry) -> str:
    """Compute the fully qualified structure_path for a symbol.

    Delegates to the shared `domain.utils.symbol_structure_path` (SSOT-12) so
    this and `structure_queries._symbol_path` can never independently drift.
    """
    return symbol_structure_path(sym.file_path, sym.name, sym.parent)


def _build_import_name_map(
    root: Path,
    files: list[FileEntry],
    stack: str,
) -> dict[str, dict[str, tuple[str, str]]]:
    """Build per-file map of imported names to their source for cross-file call resolution.

    Returns {file_rel_path: {local_name: (target_rel_path, original_name)}}.
    Only populated for Python projects.
    """
    if stack != "python":
        return {}

    module_map: dict[str, str] = {}
    for f in files:
        if not f.rel_path.endswith(".py"):
            continue
        mod = f.rel_path.replace("/", ".").replace("\\", ".")
        if mod.endswith(".py"):
            mod = mod[:-3]
        if mod.startswith("src."):
            mod = mod[4:]
        if mod.endswith(".__init__"):
            module_map[mod[:-9]] = f.rel_path
        module_map[mod] = f.rel_path

    result: dict[str, dict[str, tuple[str, str]]] = {}
    for f in files:
        if not f.rel_path.endswith(".py"):
            continue
        full_path = root / f.rel_path
        if not full_path.is_file():
            continue
        try:
            text = read_text_utf8(full_path)
        except OSError:
            continue

        name_map: dict[str, tuple[str, str]] = {}
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r"^from\s+([\w.]+)\s+import\s+(.+)$", stripped)
            if not m:
                continue
            mod_name = m.group(1)
            target = _resolve_module(mod_name, module_map)
            if not target or target == f.rel_path:
                continue
            names_str = m.group(2).strip().rstrip("\\").strip("()")
            for part in names_str.split(","):
                part = part.strip()
                if " as " in part:
                    original, local = [p.strip() for p in part.split(" as ", 1)]
                else:
                    original = local = part
                if original and local and original.isidentifier() and local.isidentifier():
                    name_map[local] = (target, original)

        if name_map:
            result[f.rel_path] = name_map

    return result


def _walk_no_inner_defs(stmts: list) -> list:
    """Yield AST nodes from stmts without descending into nested function/class definitions."""
    stack: list = list(stmts)
    out: list = []
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append(child)
    return out


def _extract_call_edges(
    abs_path: str,
    rel_path: str,
    tl_lookup: dict[tuple[str, str], str],
    method_lookup: dict[tuple[str, str, str], str],
    import_names: dict[str, tuple[str, str]],
) -> list[CallEdge]:
    """Extract function-level CALLS edges from a Python file.

    Resolves:
    - bare calls: foo() → same-file top-level function or imported function
    - self/cls calls: self.method() → same-class method

    Returns deduplicated CallEdge list.
    """
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            return []
        source = read_text_utf8(abs_path)
    except OSError:
        return []

    if source.count("\n") > 10_000:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    edges: list[CallEdge] = []
    seen: set[tuple[str, str]] = set()

    def add_edge(caller: str, callee: str) -> None:
        key = (caller, callee)
        if key not in seen and caller != callee:
            seen.add(key)
            edges.append(CallEdge(caller_path=caller, callee_path=callee))

    def resolve_name(name: str) -> str | None:
        if (rel_path, name) in tl_lookup:
            return tl_lookup[(rel_path, name)]
        if name in import_names:
            tgt_path, orig_name = import_names[name]
            if (tgt_path, orig_name) in tl_lookup:
                return tl_lookup[(tgt_path, orig_name)]
        return None

    def resolve_self(method_name: str, class_name: str) -> str | None:
        return method_lookup.get((rel_path, class_name, method_name))

    def walk_body(body: list, caller_sym_path: str, class_ctx: str | None) -> None:
        count = 0
        for node in _walk_no_inner_defs(body):
            if count >= _MAX_CALLS_PER_SYMBOL:
                break
            if not isinstance(node, ast.Call):
                continue
            callee = None
            func = node.func
            if isinstance(func, ast.Name):
                callee = resolve_name(func.id)
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in ("self", "cls")
                and class_ctx
            ):
                callee = resolve_self(func.attr, class_ctx)
            if callee:
                add_edge(caller_sym_path, callee)
                count += 1

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            caller = tl_lookup.get((rel_path, node.name))
            if caller:
                walk_body(node.body, caller, None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    caller = method_lookup.get((rel_path, node.name, item.name))
                    if caller:
                        walk_body(item.body, caller, node.name)

    return edges


def _extract_module_docstring(abs_path: str) -> str:
    """Extract the module-level docstring or first block comment from a Python file."""
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            return ""
        source = read_text_utf8(abs_path)
    except OSError:
        return ""
    try:
        tree = ast.parse(source)
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            doc = tree.body[0].value.value
            first_line = doc.splitlines()[0].strip() if doc else ""
            return first_line[:200] if first_line else ""
    except SyntaxError:
        pass
    # Fall back to leading # comment
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:200]
        break
    return ""


def _extract_symbols(abs_path: str, rel_path: str) -> tuple[list[SymbolEntry], bool]:
    """Extract function/class/method symbols from a Python file via stdlib ast.

    Returns (symbols, truncated) where truncated is True if the per-file cap was hit.
    Skips files >2 MB or >10k lines (generated code, data files) and files with syntax errors.
    """
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            logger.debug("Skipping symbol extraction for oversized file: %s", rel_path)
            return [], False
        source = read_text_utf8(abs_path)
    except OSError:
        return [], False

    if source.count("\n") > 10_000:
        logger.debug("Skipping symbol extraction for large file: %s", rel_path)
        return [], False

    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.debug("Skipping symbol extraction for unparseable file: %s", rel_path)
        return [], False

    symbols: list[SymbolEntry] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_make_symbol(node, rel_path, parent=""))
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            symbols.append(SymbolEntry(
                file_path=rel_path,
                name=node.name,
                kind="class",
                line_no=node.lineno,
                signature=f"class {node.name}",
                docstring=doc.splitlines()[0][:120] if doc else "",
                parent="",
            ))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_make_symbol(item, rel_path, parent=node.name))
        if len(symbols) >= _SYMBOL_PER_FILE_CAP:
            break

    truncated = len(symbols) >= _SYMBOL_PER_FILE_CAP
    return symbols, truncated


def _make_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    parent: str,
) -> SymbolEntry:
    sig = _build_signature(node)
    doc = ast.get_docstring(node) or ""
    decorator = _detect_decorator(node)
    return SymbolEntry(
        file_path=rel_path,
        name=node.name,
        kind="method" if parent else "function",
        line_no=node.lineno,
        signature=sig,
        docstring=doc.splitlines()[0][:120] if doc else "",
        parent=parent,
        decorator=decorator,
    )


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({args}){ret}"
    except Exception:
        return f"def {node.name}(...)"


def _detect_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for dec in node.decorator_list:
        name = ast.unparse(dec).split("(")[0]
        if name in ("property", "classmethod", "staticmethod"):
            return name
    return ""
