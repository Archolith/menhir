"""Benchmark: query_structure (Neo4j) vs file-system equivalents.

Run with: python -m tests.bench_structure_queries
Requires live Neo4j with ingested cth.mcp.memory project.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

PROJECT = "cth.mcp.memory"
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # cth.mcp.memory/


def _time_it(label: str, fn, iterations: int = 5):
    """Run fn N times, report avg/min/max ms."""
    times = []
    result = None
    for _ in range(iterations):
        start = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    avg = sum(times) / len(times)
    print(f"  {label:40s}  avg={avg:7.1f}ms  min={min(times):7.1f}ms  max={max(times):7.1f}ms")
    return result


# ---------------------------------------------------------------------------
# Neo4j queries (via structure_queries directly)
# ---------------------------------------------------------------------------

def _get_writer():
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.structure_queries import StructureGraphWriter
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    neo4j = Neo4jRepository(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "password"),
    )
    return StructureGraphWriter(neo4j)


def neo4j_overview(writer):
    return writer.query_overview(PROJECT)


def neo4j_files(writer):
    return writer.query_files(PROJECT)


def neo4j_files_filtered(writer):
    return writer.query_files(PROJECT, path_filter="src/menhir/services/")


def neo4j_imports(writer):
    return writer.query_imports(PROJECT, file_path="src/menhir/services/ingest_service.py")


def neo4j_tests(writer):
    return writer.query_tests(PROJECT)


def neo4j_endpoints(writer):
    return writer.query_endpoints(PROJECT)


def neo4j_dependencies(writer):
    return writer.query_dependencies(PROJECT)


def neo4j_cross_refs(writer):
    return writer.query_cross_refs(PROJECT)


# ---------------------------------------------------------------------------
# File-system equivalents (what Claude would do without the tool)
# ---------------------------------------------------------------------------

SRC = PROJECT_ROOT / "src"
TESTS = PROJECT_ROOT / "tests"


def fs_list_files():
    """Equivalent of query_structure("files") — walk entire project."""
    files = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"}]
        for f in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, f), PROJECT_ROOT))
    return files


def fs_list_files_filtered():
    """Equivalent of query_structure("files", path="src/menhir/services/")."""
    target = SRC / "menhir" / "services"
    files = []
    for dirpath, _, filenames in os.walk(target):
        for f in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, f), PROJECT_ROOT))
    return files


def fs_imports():
    """Equivalent of query_structure("imports", file="...ingest_service.py") — read file + regex."""
    target = SRC / "menhir" / "services" / "ingest_service.py"
    text = target.read_text()
    imports = []
    for line in text.splitlines():
        m = re.match(r"^from\s+([\w.]+)\s+import", line.strip())
        if m:
            imports.append(m.group(1))
        m = re.match(r"^import\s+([\w.]+)", line.strip())
        if m:
            imports.append(m.group(1))
    # For "imported_by" we'd need to grep ALL files
    imported_by = []
    for dirpath, _, filenames in os.walk(SRC):
        for f in filenames:
            if not f.endswith(".py"):
                continue
            full = os.path.join(dirpath, f)
            try:
                content = open(full).read()
                if "ingest_service" in content and full != str(target):
                    imported_by.append(os.path.relpath(full, PROJECT_ROOT))
            except Exception:
                pass
    return {"imports": imports, "imported_by": imported_by}


def fs_tests():
    """Equivalent of query_structure("tests") — match test_X.py to X.py by convention."""
    test_files = []
    for dirpath, _, filenames in os.walk(TESTS):
        for f in filenames:
            if f.startswith("test_") and f.endswith(".py"):
                test_files.append(f)
    # Now find source files
    src_files = {}
    for dirpath, _, filenames in os.walk(SRC):
        for f in filenames:
            if f.endswith(".py"):
                src_files[f] = os.path.relpath(os.path.join(dirpath, f), PROJECT_ROOT)
    pairs = []
    for tf in test_files:
        source_name = tf.replace("test_", "", 1)
        if source_name in src_files:
            pairs.append((tf, src_files[source_name]))
    return pairs


def fs_endpoints():
    """Equivalent of query_structure("endpoints") — grep for BaseTextTool/BaseJsonTool + route decorators."""
    endpoints = []
    for dirpath, _, filenames in os.walk(SRC):
        for f in filenames:
            if not f.endswith(".py"):
                continue
            full = os.path.join(dirpath, f)
            try:
                text = open(full).read()
                for m in re.finditer(r'class\s+\w+\(Base(?:Text|Json)Tool\)', text):
                    block = text[m.start():m.start()+500]
                    nm = re.search(r'name\s*=\s*["\']([^"\']+)', block)
                    if nm:
                        endpoints.append(nm.group(1))
                for m in re.finditer(r'@(?:app|router)\.(get|post|put|delete)\(\s*["\']([^"\']+)', text):
                    endpoints.append(f"{m.group(1).upper()} {m.group(2)}")
            except Exception:
                pass
    return endpoints


def fs_dependencies():
    """Equivalent of query_structure("dependencies") — parse pyproject.toml."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    text = pyproject.read_text()
    deps = []
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^dependencies\s*=\s*\[", stripped):
            in_deps = True
            continue
        if in_deps:
            if "]" in stripped:
                break
            m = re.match(r'^"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped)
            if m:
                deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])
    return deps


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 75)
    print("Benchmark: query_structure (Neo4j) vs file-system equivalents")
    print(f"Project: {PROJECT} at {PROJECT_ROOT}")
    print("=" * 75)

    writer = _get_writer()

    # Warm up Neo4j connection
    writer.neo4j.execute("RETURN 1")

    print("\n--- Neo4j structural queries ---")
    overview = _time_it("overview", lambda: neo4j_overview(writer))
    neo4j_f = _time_it("files (all)", lambda: neo4j_files(writer))
    _time_it("files (services/)", lambda: neo4j_files_filtered(writer))
    neo4j_imp = _time_it("imports (ingest_service)", lambda: neo4j_imports(writer))
    neo4j_t = _time_it("tests", lambda: neo4j_tests(writer))
    neo4j_ep = _time_it("endpoints", lambda: neo4j_endpoints(writer))
    neo4j_dep = _time_it("dependencies", lambda: neo4j_dependencies(writer))
    _time_it("cross_refs", lambda: neo4j_cross_refs(writer))

    print("\n--- File-system equivalents ---")
    fs_f = _time_it("files (walk all)", lambda: fs_list_files())
    _time_it("files (walk services/)", lambda: fs_list_files_filtered())
    fs_imp = _time_it("imports (read+grep all .py)", lambda: fs_imports())
    fs_t = _time_it("tests (convention match)", lambda: fs_tests())
    fs_ep = _time_it("endpoints (grep all .py)", lambda: fs_endpoints())
    fs_dep = _time_it("dependencies (parse toml)", lambda: fs_dependencies())

    print("\n--- Result counts ---")
    print(f"  Neo4j files: {len(neo4j_f)}, FS files: {len(fs_f)}")
    print(f"  Neo4j imports out: {len(neo4j_imp['imports'])}, imported_by: {len(neo4j_imp['imported_by'])}")
    print(f"  FS imports out: {len(fs_imp['imports'])}, imported_by: {len(fs_imp['imported_by'])}")
    print(f"  Neo4j tests: {len(neo4j_t)}, FS tests: {len(fs_t)}")
    print(f"  Neo4j endpoints: {len(neo4j_ep)}, FS endpoints: {len(fs_ep)}")
    print(f"  Neo4j deps: {len(neo4j_dep)}, FS deps: {len(fs_dep)}")

    print("\n--- Context window cost (approximate chars) ---")
    # Estimate how many chars Claude would consume
    neo4j_chars = sum(len(str(f)) for f in neo4j_f)
    fs_chars = sum(len(str(f)) for f in fs_f)
    print(f"  Neo4j files response: ~{neo4j_chars:,} chars")
    print(f"  FS file list response: ~{fs_chars:,} chars")
    imp_chars = len(str(fs_imp))
    print(f"  FS imports (full grep): ~{imp_chars:,} chars (reads every .py file)")
    print(f"  Neo4j imports response: ~{len(str(neo4j_imp)):,} chars (instant lookup)")

    writer.neo4j.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
