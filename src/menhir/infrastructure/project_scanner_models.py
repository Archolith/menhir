"""Data models for the deterministic project directory scanner.

Moved verbatim from ``project_scanner.py``; the facade module re-exports every name here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
