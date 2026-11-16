"""Deterministic project directory scanner for structural graph ingestion."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import logging
import os
import re
import stat
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from menhir.domain.utils import symbol_structure_path
from menhir.infrastructure.git_binding import read_git_binding as _read_git_binding
from menhir.infrastructure.path_containment import (
    check_contained_regular_file,
    is_contained_dir,
    is_host_system_path,
)
from menhir.infrastructure.text_io import read_text_utf8

logger = logging.getLogger(__name__)


class ScanReadError(RuntimeError):
    """A required source file could not be read, so absence cannot authorize pruning."""


class ScanPathRefused(OSError):
    """A path resolves outside the scan root or is not a regular file; treated as absent.

    An OSError so callers' existing absent-file handling applies. Raised outside the
    ScanReadError wrapping, which would otherwise abort the whole scan.
    """


# The root of the scan in progress. Every scanner read checks against it, including fixed-name
# reads (README.md, pyproject.toml) and files swapped after the walk.
_SCAN_ROOT: ContextVar[Path | None] = ContextVar("menhir_scan_root", default=None)


def _refuse_uncontained(path: str | Path) -> None:
    if not os.path.lexists(path):
        return  # a missing file keeps its ordinary read error
    root = _SCAN_ROOT.get()
    if root is not None:
        try:
            contained = check_contained_regular_file(path, root)
        except OSError as error:
            # Not proof of absence: refuse the scan rather than treat the file as missing.
            raise ScanReadError(f"Project scan could not examine {path}: {error}") from error
        if not contained:
            raise ScanPathRefused(f"{path} is not a regular file inside the scan root")
        return
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return
    if not regular:
        raise ScanPathRefused(f"{path} is not a regular file")


def _is_dir_link(path: str) -> bool:
    """True for a symlinked directory or a Windows junction (including volume mount points)."""
    return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def _read_text_for_scan(path: str | Path) -> str:
    _refuse_uncontained(path)
    try:
        return read_text_utf8(path)
    except OSError as error:
        raise ScanReadError(f"Project scan could not read {path}: {error}") from error


def _getsize_for_scan(path: str | Path) -> int:
    _refuse_uncontained(path)
    try:
        return os.path.getsize(path)
    except OSError as error:
        raise ScanReadError(f"Project scan could not stat {path}: {error}") from error

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
    stack_root: str = ""        # nearest stack root of the file's language ("." = repo root) (#681)
    test_runner: str = ""       # the test runner at that root (#681)


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
    #: Graph-issued ordering token obtained before traversal. A late scan may publish only while
    #: this is still the identity's latest issued scan generation.
    scan_generation: int | None = None
    symbols: list[SymbolEntry] = field(default_factory=list)
    truncated_symbol_files: list[str] = field(default_factory=list)
    call_edges: list[CallEdge] = field(default_factory=list)
    # .py files listed in `files` whose imports this scan could not read (vanished mid-scan,
    # refused as an out-of-root link). Their stored edges are not evidence-free to prune (#680).
    import_sources_unread: list[str] = field(default_factory=list)
    # Coverage accounting. Three counts, not one ratio: `files_indexed / files_discovered`
    # would mark every project permanently partial once documentation is intentionally
    # excluded. Truncation is `files_indexed < files_eligible`.
    files_discovered: int = 0
    files_eligible: int = 0
    files_indexed: int = 0
    nested_repos: list[NestedRepo] = field(default_factory=list)
    # The git commit this scan describes, for Beacon's evidence binding. `indexed_dirty` is True
    # when the working tree had uncommitted changes (or HEAD moved during the scan): then the
    # indexed content is not that commit and the binding must not be served. Empty/False when
    # the root is not a git repository.
    indexed_commit: str = ""
    indexed_repository: str = ""
    indexed_dirty: bool = False

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
SCANNER_SCHEMA_VERSION = 12  # v12: ast imports, per-directory stack, ranked test links (#681)
# (v11: text literals elided from stored signature defaults)
# (v9: the legacy `.agent/project-id` identity file is scan-invisible)

# Beacon publication and evidence scratch files live at the repository root. They are
# outputs/coordination state derived from a scan, never project source. Letting them back into
# discovery makes the scan fingerprint self-referential: every refresh changes an mtime, which
# forces another ingest and another refresh forever. The prefixes are deliberately root-only so
# a legitimate nested file with the same basename remains visible.
_BEACON_ROOT_FILES = {"beacon.generated.yaml", ".beacon.generated.lock"}
_BEACON_ROOT_PREFIXES = (".beacon.generated.", ".beacon-evidence-")


def _is_beacon_root_artifact(relative_path: str) -> bool:
    """Return whether *relative_path* is Beacon-owned generated/scratch state."""
    return "/" not in relative_path and (
        relative_path in _BEACON_ROOT_FILES
        or relative_path.startswith(_BEACON_ROOT_PREFIXES)
    )

#: CF-257's legacy per-checkout identity file. Identity lives in the graph now and Menhir never
#: writes it; files earlier versions left behind are Menhir state, not project content.
_LEGACY_IDENTITY_FILE = ".agent/project-id"

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

# Port -> project name mapping for cross-project ref detection.
#
# Empty by default: this shipped one operator's project names (and a service that no longer
# exists) as though they were general knowledge, so every other installation got wrong
# attributions for ports it happened to use. Populate it per deployment if that mapping is
# wanted; nothing here can be right for everyone.
_KNOWN_PORTS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class ProjectScanner:
    """Scans a project directory and produces a structured result."""

    def scan(self, root: str | Path, name: str | None = None) -> ProjectScanResult:
        root = Path(root).resolve()
        token = _SCAN_ROOT.set(root)
        try:
            return self._scan(root, name)
        finally:
            _SCAN_ROOT.reset(token)

    def _scan(self, root: Path, name: str | None) -> ProjectScanResult:
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        project_name = name or root.name
        gitignore_patterns = _load_gitignore(root)
        binding_before = _read_git_binding(root, ignore=_is_beacon_root_artifact)

        # Walk and collect
        all_rel_paths: list[str] = []
        dir_set: set[str] = set()
        file_entries: list[FileEntry] = []
        nested_repos: list[NestedRepo] = []

        def _walk_error(error: OSError) -> None:
            # os.walk silently skips unreadable subtrees without onerror. Their absence
            # would otherwise authorize the graph writer to prune previously indexed data.
            raise OSError(f"Project scan could not traverse {error.filename}: {error}") from error

        for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
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
                if _is_dir_link(full_d) or not is_contained_dir(full_d, root):
                    # os.walk does not follow symlinks but does enter Windows junctions; one
                    # pointing back into the root would loop or index files twice.
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
                if _is_beacon_root_artifact(rel) or rel == _LEGACY_IDENTITY_FILE:
                    continue
                if _matches_gitignore(full, root, gitignore_patterns):
                    continue
                try:
                    contained = check_contained_regular_file(full, root)
                except OSError as error:
                    # Absence must not authorize pruning: an unexaminable file refuses the scan.
                    raise OSError(f"Project scan could not stat {full}: {error}") from error
                if not contained:
                    # A symlink out of the root, or a FIFO/device: never index or read it.
                    logger.info("Project scan skipped %s: not a regular file inside the root", rel)
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
            except OSError as error:
                raise OSError(f"Project scan could not stat {root / rp}: {error}") from error
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

        # Detect stack: the root's for the project summary, each directory's for its files (#681)
        stack = _detect_stack(root)
        stack_roots = _detect_stack_roots(root, dir_set)
        _stamp_stacks(root, file_entries, stack_roots)

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
        result.imports, import_name_map = _analyze_python_imports(
            root, file_entries, stack_roots, result.import_sources_unread
        )
        result.test_edges = _detect_test_edges(file_entries, result.imports)
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
        if result.symbols:
            tl_lookup: dict[tuple[str, str], str] = {}
            method_lookup: dict[tuple[str, str, str], str] = {}
            for sym in result.symbols:
                path = _sym_path(sym)
                if sym.kind == "function":
                    tl_lookup[(sym.file_path, sym.name)] = path
                elif sym.kind == "method":
                    method_lookup[(sym.file_path, sym.parent, sym.name)] = path
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

        # A file disappearing after traversal is not evidence that it was deleted from the
        # project. Refuse this publication rather than prune from a torn filesystem view.
        for entry in result.files:
            if not (root / entry.rel_path).is_file():
                raise ScanReadError(
                    f"Project scan lost indexed file {root / entry.rel_path}"
                )

        # Read the binding on both sides of every filesystem read above: a commit or edit
        # landing mid-scan means the fingerprint and the commit may not describe one content.
        binding_after = _read_git_binding(root, ignore=_is_beacon_root_artifact)
        if binding_after is not None:
            commit, repository, dirty = binding_after
            result.indexed_commit = commit
            result.indexed_repository = repository
            result.indexed_dirty = dirty or binding_before != binding_after
        return result


# ---------------------------------------------------------------------------
# gitignore
# ---------------------------------------------------------------------------

def _load_gitignore(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not os.path.lexists(gi):
        return []
    try:
        regular = stat.S_ISREG(os.stat(gi).st_mode)
    except OSError as error:
        # A dangling or unreadable link to a shared .gitignore must not run the scan unfiltered.
        raise ScanReadError(f"Project scan could not examine {gi}: {error}") from error
    if not regular:
        raise ScanReadError(f"Project scan refused {gi}: not a regular file")
    # A shared .gitignore may live outside the root: only its patterns are used, never stored.
    # It is not optional, though: without it ignored files (local secrets) would be indexed.
    real = Path(os.path.realpath(gi))
    if is_host_system_path(real.parts) and not is_host_system_path(Path(os.path.realpath(root)).parts):
        raise ScanReadError(f"Project scan refused {gi}: it resolves to host system file {real}")
    try:
        text = read_text_utf8(gi)
    except OSError as error:
        raise ScanReadError(f"Project scan could not read {gi}: {error}") from error
    patterns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
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

    # A0: bounded agent-orientation docs (Beacon v2 design, §A0). Classified
    # before entrypoint/config checks so `.agent/README.md` is a document, not
    # an entrypoint, and before the tests rule so `.agent/*` is never a test.
    if len(parts) == 2 and parts[0] == ".agent" and name in _AGENT_ORIENTATION_DOCS:
        return "document"

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
    # pytest fixtures apply wherever the file sits; Go tests live beside their package.
    if name == "conftest.py" or re.match(r".*_test\.go$", name):
        return "test"
    if re.match(r".*\.test\.(ts|tsx|js|jsx)$", name):
        return "test"
    if re.match(r".*\.spec\.(ts|tsx|js|jsx)$", name):
        return "test"

    return "file"


def _is_root_level(parts: list[str]) -> bool:
    return len(parts) <= 2


#: A0 (Beacon v2 design): the only `.agent/` markdown files the scanner promotes
#: to document-role entities. Orientation for any agent; contributor process docs
#: (workflows, plans, file-index, maintenance) stay out of the document set.
_AGENT_ORIENTATION_DOCS: frozenset[str] = frozenset(
    {"README.md", "architecture.md", "data_models.md", "endpoints.md", "CHANGELOG.md"}
)


def _infer_description(rel_path: str, role: str) -> str:
    name = os.path.basename(rel_path)
    if role == "entrypoint":
        return f"Entry point: {name}"
    if role == "config":
        return f"Configuration: {name}"
    if role == "test":
        return f"Test file: {name}"
    if role == "document":
        return f"Agent orientation doc: {name}"
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
            text = _read_text_for_scan(agent_readme)[:1000]
            # Extract first meaningful paragraph
            return _first_paragraph(text)
        except OSError:
            pass

    # Fall back to CLAUDE.md, then the project's own README.md: a repository without the
    # agent-doc conventions still describes itself there (Beacon evidence refuses an empty one).
    for name in ("CLAUDE.md", "README.md"):
        path = root / name
        if path.is_file():
            try:
                description = _first_paragraph(_read_text_for_scan(path)[:1000])
            except OSError:
                continue
            if description:
                return description

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
            text = _read_text_for_scan(pyproject)
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
            for line in _read_text_for_scan(reqs).splitlines():
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
        data = json.loads(_read_text_for_scan(pkg))
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
            text = _read_text_for_scan(gf)
            deps: list[str] = []
            for m in re.finditer(r"""(?:implementation|api|compileOnly|runtimeOnly)\s*[\('"]\s*([^'")\s]+)""", text):
                deps.append(m.group(1).split(":")[0] if ":" in m.group(1) else m.group(1))
            return deps
        except OSError:
            pass
    return []


#: Language family per stack-signal file, and per source extension (#681). A test runs under
#: the nearest stack root of ITS OWN family: a `.test.ts` beside a root `pyproject.toml` is not
#: a pytest file.
_SIGNAL_FAMILY: dict[str, str] = {
    "pyproject.toml": "py",
    "setup.py": "py",
    "requirements.txt": "py",
    "package.json": "js",
    "build.gradle": "jvm",
    "build.gradle.kts": "jvm",
    "pom.xml": "jvm",
    "Cargo.toml": "rust",
    "go.mod": "go",
}
_EXTENSION_FAMILY: dict[str, str] = {
    ".py": "py",
    ".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".groovy": "jvm", ".scala": "jvm",
    ".rs": "rust",
    ".go": "go",
}
#: Runner per stack-signal file. A runner that takes file arguments is followed by the
#: selected files; the others run their stack root's suite (see structure_queries).
_SIGNAL_TEST_RUNNERS: dict[str, str] = {
    "pyproject.toml": "pytest",
    "setup.py": "pytest",
    "requirements.txt": "pytest",
    "build.gradle": "./gradlew test",
    "build.gradle.kts": "./gradlew test",
    "Cargo.toml": "cargo test",
    "go.mod": "go test",
    "pom.xml": "mvn test",
}
#: Runner when no stack root of the file's family exists: run from the repository root.
#: Only pytest runs without a manifest; `npm test` / `go test` / `cargo test` need theirs, so a
#: stray `.js` file in a repo with no `package.json` gets no command rather than a wrong one.
_FAMILY_FALLBACK_RUNNERS: dict[str, str] = {
    "py": "pytest",
}


#: Signals that name a project only outside a Python package (see `_detect_stack_roots`).
_WEAK_PACKAGE_SIGNALS = frozenset({"setup.py", "requirements.txt"})


@dataclass(frozen=True)
class StackRoot:
    """A directory carrying a stack signal: the nearest one above a file sets its stack."""

    rel_dir: str  # "." for the repository root
    stack: str
    signal: str  # the signal filename that decided it


def _detect_stack_roots(root: Path, rel_dirs: set[str] | list[str]) -> dict[str, list[StackRoot]]:
    """Stack per directory (#681): every scanned directory with signal files is a stack root.

    A directory can hold several (a Python service with a `package.json` for its tooling); each
    is kept, so a file finds the root of its own language.
    """
    roots: dict[str, list[StackRoot]] = {}
    for rel_dir in sorted({".", *rel_dirs}):
        base = root if rel_dir == "." else root / rel_dir
        in_package = rel_dir != "." and (base / "__init__.py").is_file()
        for filename, stack in _STACK_SIGNALS:
            # Inside a Python package, `setup.py` / `requirements.txt` are modules or data
            # (`menhir/cli/setup.py` is the setup COMMAND), not a project manifest.
            if in_package and filename in _WEAK_PACKAGE_SIGNALS:
                continue
            if (base / filename).is_file():
                found = roots.setdefault(rel_dir, [])
                if all(_SIGNAL_FAMILY[r.signal] != _SIGNAL_FAMILY[filename] for r in found):
                    found.append(StackRoot(rel_dir, stack, filename))
    return roots


def _file_family(rel_path: str) -> str | None:
    return _EXTENSION_FAMILY.get(os.path.splitext(rel_path)[1].lower())


def _stack_root_for(
    rel_path: str, roots: dict[str, list[StackRoot]], family: str | None = None
) -> StackRoot | None:
    """The nearest stack root above *rel_path* (of *family*, when given), or None."""
    parts = rel_path.split("/")[:-1]
    while True:
        key = "/".join(parts) if parts else "."
        for found in roots.get(key, ()):
            if family is None or _SIGNAL_FAMILY[found.signal] == family:
                return found
        if not parts:
            return None
        parts.pop()


def _js_test_runner(package_json: Path) -> str:
    """vitest or jest from the package's declared deps; `npm test` when it says neither.

    Read through the scan reader so the containment check applies: a `package.json` that is a
    link out of the project is not read, and the runner degrades rather than the scan failing.
    """
    try:
        data = json.loads(_read_text_for_scan(package_json))
    except (OSError, ScanReadError, ValueError):
        return "npm test --"
    deps: dict[str, object] = {}
    for key in ("devDependencies", "dependencies"):
        section = data.get(key) if isinstance(data, dict) else None
        if isinstance(section, dict):
            deps.update(section)
    if "vitest" in deps:
        return "npx vitest run"
    if "jest" in deps:
        return "npx jest"
    return "npm test --"


def _stamp_stacks(root: Path, files: list[FileEntry], roots: dict[str, list[StackRoot]]) -> None:
    """Record, per file, the stack root of its own language and the test runner there.

    Every file is stamped, not only tests: a change with no selected tests still needs its
    stack's suite command.
    """
    js_runners: dict[str, str] = {}
    for f in files:
        family = _file_family(f.rel_path)
        sr = _stack_root_for(f.rel_path, roots, family) if family else None
        if sr is None and family is None:
            sr = _stack_root_for(f.rel_path, roots)
        if sr is None:
            f.stack_root = "." if family in _FAMILY_FALLBACK_RUNNERS else ""
            f.test_runner = _FAMILY_FALLBACK_RUNNERS.get(family or "", "")
            continue
        f.stack_root = sr.rel_dir
        if sr.signal == "package.json":
            if sr.rel_dir not in js_runners:
                base = root if sr.rel_dir == "." else root / sr.rel_dir
                js_runners[sr.rel_dir] = _js_test_runner(base / "package.json")
            f.test_runner = js_runners[sr.rel_dir]
        elif sr.signal in ("build.gradle", "build.gradle.kts"):
            f.stack_root, f.test_runner = _gradle_command(root, sr.rel_dir)
        else:
            f.test_runner = _SIGNAL_TEST_RUNNERS.get(sr.signal, "")


def _gradle_command(root: Path, rel_dir: str) -> tuple[str, str]:
    """(directory to run from, task) for a Gradle (sub-)project.

    The wrapper lives at the build root, not in each sub-project: run `./gradlew :a:b:test`
    from the nearest ancestor holding `gradlew`, or plain `gradle test` where there is none.
    """
    parts = [] if rel_dir == "." else rel_dir.split("/")
    for i in range(len(parts), -1, -1):
        base = root.joinpath(*parts[:i]) if i else root
        if (base / "gradlew").is_file():
            wrapper_dir = "/".join(parts[:i]) or "."
            sub = parts[i:]
            return wrapper_dir, ("./gradlew " + (":" + ":".join(sub) + ":test" if sub else "test"))
    return rel_dir, "gradle test"


# ---------------------------------------------------------------------------
# Import parsing
# ---------------------------------------------------------------------------

#: One import statement: (module, [(name, asname)], level). `import a.b as c` is ("a.b", [], 0).
_PyImport = tuple[str, list[tuple[str, str]], int]

_FROM_RE = re.compile(r"^from\s+(\.*)([\w.]*)\s+import\s+(.+)$")
_IMPORT_RE = re.compile(r"^import\s+(.+)$")


def _python_imports(text: str) -> list[_PyImport]:
    """Every import statement in *text*, read with `ast` (#681).

    A file `ast` cannot parse (a syntax error, Python 2) falls back to the old line match, so a
    broken file still contributes what it can rather than nothing.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _python_imports_by_line(text)
    found: list[_PyImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, [], 0))
        elif isinstance(node, ast.ImportFrom):
            names = [(a.name, a.asname or a.name) for a in node.names]
            found.append((node.module or "", names, node.level or 0))
    return found


def _python_imports_by_line(text: str) -> list[_PyImport]:
    # One logical line per statement: a parenthesised `from . import (a, b)` spread over lines,
    # and backslash-continued lines, would otherwise show only their first physical line.
    text = text.replace("\\\n", " ")
    text = re.sub(r"\(([^()]*)\)", lambda m: "(" + m.group(1).replace("\n", " ") + ")", text)
    found: list[_PyImport] = []
    for line in text.splitlines():
        stripped = line.strip()
        m = _FROM_RE.match(stripped)
        if m:
            names: list[tuple[str, str]] = []
            for part in m.group(3).strip().rstrip("\\").strip("()").split(","):
                part = part.strip()
                original, _, local = part.partition(" as ")
                original, local = original.strip(), (local.strip() or original.strip())
                if original.isidentifier() and local.isidentifier():
                    names.append((original, local))
            found.append((m.group(2), names, len(m.group(1))))
            continue
        m = _IMPORT_RE.match(stripped)
        if m:
            for part in m.group(1).split(","):
                mod = part.strip().split(" as ")[0].strip()
                if re.fullmatch(r"[\w.]+", mod):
                    found.append((mod, [], 0))
    return found


def _module_names(rel_path: str, py_roots: list[str]) -> list[tuple[str, str]]:
    """(dotted name, scope) pairs a `.py` file can be imported as.

    Relative to the repository root and to its `src/` (scope "", visible everywhere, as
    before), and to every Python stack root above it and that root's `src/` -- so
    `packages/x/src/pkg/mod.py` is also `pkg.mod`. A name earned from a sub-project root is
    scoped to importers inside that root: from anywhere else, `import logging` is the stdlib,
    not `packages/x/src/logging.py`.
    """
    stem = rel_path[:-3]
    bases: dict[str, str] = {"": "", "src/": ""}
    for py_root in py_roots:
        if py_root == ".":
            continue
        prefix = py_root + "/"
        if not stem.startswith(prefix):
            continue
        bases.setdefault(prefix, py_root)
        bases.setdefault(prefix + "src/", py_root)
    names: list[tuple[str, str]] = []
    for base, scope in sorted(bases.items()):
        if not stem.startswith(base):
            continue
        dotted = stem[len(base):].replace("/", ".")
        if not dotted or dotted == "__init__":
            continue
        if dotted.endswith(".__init__"):
            names.append((dotted[: -len(".__init__")], scope))
        names.append((dotted, scope))
    return names


class _PyModuleIndex:
    """Dotted module name -> candidate files, plus a path set for relative resolution."""

    def __init__(self, files: list[FileEntry], roots: dict[str, list[StackRoot]]) -> None:
        py_roots = sorted(r.rel_dir for rs in roots.values() for r in rs if r.stack == "python")
        self.paths = {f.rel_path for f in files if f.rel_path.endswith(".py")}
        self.modules: dict[str, list[tuple[str, str]]] = {}
        for rel_path in sorted(self.paths):
            for name, scope in _module_names(rel_path, py_roots):
                bucket = self.modules.setdefault(name, [])
                if (rel_path, scope) not in bucket:
                    bucket.append((rel_path, scope))

    def exact(self, module: str, importer: str) -> str | None:
        candidates = sorted(
            {
                path
                for path, scope in self.modules.get(module, ())
                if not scope or importer.startswith(scope + "/")
            }
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # A name defined in two sub-projects: the copy nearest the importer is the one it gets.
        return max(candidates, key=lambda c: (_shared_dir_depth(c, importer), -len(c), c))

    def absolute(self, module: str, importer: str) -> str | None:
        """Exact match, else the longest importable prefix (an attribute of a package)."""
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            hit = self.exact(".".join(parts[:i]), importer)
            if hit:
                return hit
        return None

    def relative_base(self, importer: str, level: int, module: str) -> str | None:
        """Directory (no trailing slash, "" for root) a relative import's module names."""
        parts = importer.split("/")[:-1]
        up = level - 1
        if up >= len(parts):  # past the top-level package: Python refuses this import too
            return None
        base = parts[: len(parts) - up] if up else parts
        if module:
            base = base + module.split(".")
        return "/".join(base)

    def file_at(self, dotted_dir: str) -> str | None:
        """The module file for a slash path: `<p>.py`, else `<p>/__init__.py`."""
        if not dotted_dir:
            return "__init__.py" if "__init__.py" in self.paths else None
        for candidate in (f"{dotted_dir}.py", f"{dotted_dir}/__init__.py"):
            if candidate in self.paths:
                return candidate
        return None


def _shared_dir_depth(a: str, b: str) -> int:
    depth = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1], strict=False):
        if x != y:
            break
        depth += 1
    return depth


def _resolve_python_import(
    imp: _PyImport, importer: str, index: _PyModuleIndex
) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """Files one import statement links to, and the local names it binds to a module's members.

    `from pkg import sub` links to `pkg/sub.py` when `sub` is a module and to `pkg` otherwise;
    a relative import resolves against the importing file's own package.
    """
    module, names, level = imp
    targets: set[str] = set()
    bound: dict[str, tuple[str, str]] = {}
    if level:
        base = index.relative_base(importer, level, module)
        if base is None:
            return targets, bound
        owner = index.file_at(base)
        for original, local in names:
            sub = index.file_at(f"{base}/{original}" if base else original)
            if sub and original != "*":
                targets.add(sub)
                # `from .sub import mod` runs sub/__init__.py first; `from . import mod` is
                # inside a package that is already initialised, so it adds nothing.
                if module and owner and owner.endswith("__init__.py"):
                    targets.add(owner)
            elif owner:
                targets.add(owner)
                bound[local] = (owner, original)
        if not names and owner:
            targets.add(owner)
    elif names:
        owner = index.absolute(module, importer)
        package_init = index.exact(module, importer)
        for original, local in names:
            sub = index.exact(f"{module}.{original}", importer) if original != "*" else None
            if sub:
                targets.add(sub)
                # Importing a submodule executes its package's __init__.py first.
                if package_init and package_init.endswith("__init__.py"):
                    targets.add(package_init)
            elif owner:
                targets.add(owner)
                bound[local] = (owner, original)
    else:
        target = index.absolute(module, importer)
        if target:
            targets.add(target)
    targets.discard(importer)
    bound = {k: v for k, v in bound.items() if v[0] != importer}
    return targets, bound


def _analyze_python_imports(
    root: Path,
    files: list[FileEntry],
    roots: dict[str, list[StackRoot]] | None = None,
    unread: list[str] | None = None,
) -> tuple[list[ImportEdge], dict[str, dict[str, tuple[str, str]]]]:
    """IMPORTS edges and the CALLS name map from one `ast` read of every `.py` file.

    Runs on every Python file whatever the root stack (#681): a monorepo with no root signal
    still has Python in it.
    """
    if roots is None:
        roots = _detect_stack_roots(root, {os.path.dirname(f.rel_path) or "." for f in files})
    index = _PyModuleIndex(files, roots)
    edges: list[ImportEdge] = []
    seen: set[tuple[str, str]] = set()
    name_maps: dict[str, dict[str, tuple[str, str]]] = {}
    for f in files:
        if not f.rel_path.endswith(".py") or f.role == "config":
            continue
        full_path = root / f.rel_path
        try:
            if not full_path.is_file():
                raise FileNotFoundError(full_path)
            text = _read_text_for_scan(full_path)
        except OSError:
            # An editor's atomic save can make the file vanish for an instant. Absence of its
            # edges then says nothing; record it so the writer leaves its stored edges alone.
            if unread is not None:
                unread.append(f.rel_path)
            continue
        name_map: dict[str, tuple[str, str]] = {}
        for imp in _python_imports(text):
            targets, bound = _resolve_python_import(imp, f.rel_path, index)
            for target in sorted(targets):
                if (f.rel_path, target) not in seen:
                    seen.add((f.rel_path, target))
                    edges.append(ImportEdge(source_path=f.rel_path, target_path=target))
            name_map.update(bound)
        if name_map:
            name_maps[f.rel_path] = name_map
    return edges, name_maps


def _parse_imports(root: Path, files: list[FileEntry], stack: str = "") -> list[ImportEdge]:
    """Python IMPORTS edges. *stack* is ignored: the stack is decided per directory (#681)."""
    return _analyze_python_imports(root, files)[0]


# ---------------------------------------------------------------------------
# Test edge detection
# ---------------------------------------------------------------------------

def _detect_test_edges(
    files: list[FileEntry], imports: list[ImportEdge] | None = None
) -> list[TestEdge]:
    """Link each test to the source its name points at.

    Several files can share a basename (`utils.py`, `models.py`), so the candidate is ranked,
    not taken first (#681): one the test imports, then the one sharing the most trailing
    directory names with the test (`tests/cli/test_up.py` -> `src/pkg/cli/up.py`), then the
    shortest path.
    """
    test_files = [f for f in files if f.role == "test"]
    source_files = {f.rel_path: f for f in files if f.role not in ("test", "config")}
    imported: dict[str, set[str]] = {}
    for e in imports or []:
        imported.setdefault(e.source_path, set()).add(e.target_path)

    basename_index: dict[str, list[str]] = {}
    for rp in source_files:
        basename_index.setdefault(os.path.basename(rp), []).append(rp)

    def best(test_path: str, candidates: list[str]) -> str:
        test_dirs = [d for d in test_path.split("/")[:-1] if d not in ("tests", "test", "__tests__")]
        used = imported.get(test_path, set())

        def rank(c: str) -> tuple[int, int, int, str]:
            src_dirs = c.split("/")[:-1]
            shared = 0
            for x, y in zip(reversed(test_dirs), reversed(src_dirs), strict=False):
                if x != y:
                    break
                shared += 1
            return (1 if c in used else 0, shared, -len(c), c)

        return max(candidates, key=rank)

    edges: list[TestEdge] = []
    for tf in test_files:
        name = os.path.basename(tf.rel_path)
        targets: list[str] = []
        m = re.match(r"^test_(.+\.py)$", name)
        if m:
            targets = [m.group(1)]
        else:
            m = re.match(r"^(.+)_test\.py$", name)
            if m:
                targets = [m.group(1) + ".py"]
            else:
                m = re.match(r"^(.+)\.(test|spec)\.(ts|tsx|js|jsx)$", name)
                if m:
                    targets = [f"{m.group(1)}.{ext}" for ext in (m.group(3), "tsx", "ts", "js", "jsx")]
        for target_name in targets:
            candidates = basename_index.get(target_name, [])
            if candidates:
                edges.append(TestEdge(test_path=tf.rel_path, source_path=best(tf.rel_path, candidates)))
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
            text = _read_text_for_scan(full)
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
            text = _read_text_for_scan(full)
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
            text = _read_text_for_scan(full)
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
            text = _read_text_for_scan(full)
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

    # Step 2b -- A0: the bounded agent-orientation doc set is structural evidence
    # (Beacon v2 design); other markdown remains excluded documentation.
    if role == "document":
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
    stack: str = "",
) -> dict[str, dict[str, tuple[str, str]]]:
    """Per-file map of imported names to their source, for cross-file call resolution.

    Returns {file_rel_path: {local_name: (target_rel_path, original_name)}}. Shares the `ast`
    import reader with IMPORTS edges; *stack* is ignored (#681).
    """
    return _analyze_python_imports(root, files)[1]


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
        if _getsize_for_scan(abs_path) > _MAX_FILE_BYTES:
            return []
        source = _read_text_for_scan(abs_path)
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
        # The cap bounds distinct callees, not call sites: 131 calls to one helper are one edge.
        callees: set[str] = set()
        for node in _walk_no_inner_defs(body):
            if len(callees) >= _MAX_CALLS_PER_SYMBOL:
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
                callees.add(callee)

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
        if _getsize_for_scan(abs_path) > _MAX_FILE_BYTES:
            return ""
        source = _read_text_for_scan(abs_path)
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
        if _getsize_for_scan(abs_path) > _MAX_FILE_BYTES:
            logger.debug("Skipping symbol extraction for oversized file: %s", rel_path)
            return [], False
        source = _read_text_for_scan(abs_path)
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


class _ElideTextLiterals(ast.NodeTransformer):
    """Replace string, bytes and f-string literals with ``...``."""

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, (str, bytes)):
            return ast.copy_location(ast.Constant(value=...), node)
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return ast.copy_location(ast.Constant(value=...), node)


def _signature_args(args: ast.arguments) -> ast.arguments:
    """Copy *args* with text literals in default values elided.

    Signatures are stored in the structure graph and returned to every caller, and a default
    such as ``password: str = "..."`` is a common place for a hard-coded credential that no
    detector recognises. Annotations are left alone, so forward-reference types still read.
    """
    copied = copy.deepcopy(args)
    elide = _ElideTextLiterals()
    copied.defaults = [elide.visit(d) for d in copied.defaults]
    copied.kw_defaults = [None if d is None else elide.visit(d) for d in copied.kw_defaults]
    return copied


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(_signature_args(node.args))
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
