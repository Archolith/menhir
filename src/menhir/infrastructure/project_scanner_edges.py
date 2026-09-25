"""Import, test-edge, endpoint, and cross-project reference detection for the scanner.

Moved verbatim from ``project_scanner.py``; the facade module re-exports every name here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from menhir.infrastructure.project_scanner_models import (
    CrossProjectRef,
    EndpointEntry,
    FileEntry,
    ImportEdge,
    TestEdge,
)
from menhir.infrastructure.project_scanner_rules import _KNOWN_PORTS
from menhir.infrastructure.text_io import read_text_utf8

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
