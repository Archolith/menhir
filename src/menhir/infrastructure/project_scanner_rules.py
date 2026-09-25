"""Skip rules, eligibility, gitignore, classification, and walk helpers for the scanner.

Moved verbatim from ``project_scanner.py``; the facade module re-exports every name here.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path

from menhir.infrastructure.project_scanner_models import FileEntry
from menhir.infrastructure.text_io import read_text_utf8

logger = logging.getLogger(__name__)

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
SCANNER_SCHEMA_VERSION = 7  # v7: Beacon-generated root artifacts are scan-invisible

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
