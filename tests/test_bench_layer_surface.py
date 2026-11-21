"""Benchmark graph layer surfaces stay in step with the code (benchmarks/layer-surface.toml).

A stored benchmark graph layer is reused while the last commit that changed its declared files is
unchanged, so the declaration must follow code that is moved, renamed or split. Offline, AST only.
"""

from __future__ import annotations

import ast
import fnmatch
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECLARATION = "benchmarks/layer-surface.toml"


def load(root: Path) -> dict:
    return tomllib.loads((root / DECLARATION).read_text(encoding="utf-8"))


def _module_path(src: Path, module: str) -> Path | None:
    base = src / Path(*module.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def direct_imports(root: Path, rel: str) -> set[str]:
    """Repo-relative paths of the menhir modules ``rel`` imports (absolute and relative imports)."""
    src = root / "src"
    path = root / rel
    parts = list(path.relative_to(src).with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    module = parts[:-1] if is_pkg else parts
    package = module if is_pkg else module[:-1]
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    found: set[str] = set()
    for name in names:
        if name != "menhir" and not name.startswith("menhir."):
            continue
        bits = name.split(".")
        for end in range(len(bits), 0, -1):
            target = _module_path(src, ".".join(bits[:end]))
            if target is None:
                continue
            # ``from pkg import name`` where name is not a submodule: the import is the package.
            if target.name != "__init__.py" or end == len(bits):
                found.add(target.relative_to(root).as_posix())
            break
    return found


def _matches(root: Path, pattern: str) -> list[str]:
    return [p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file()]


def layer_files(root: Path, decl: dict, name: str) -> set[str]:
    layer = decl["layers"][name]
    files = {f for g in layer.get("globs", []) for f in _matches(root, g)}
    return files | set(layer.get("entries", [])) | set(layer.get("files", []))


def _outside_for(decl: dict, layer: str) -> list[str]:
    return [p for group in decl.get("outside", []) if layer not in group.get("not_for", [])
            for p in group.get("patterns", [])]


def problems(root: Path, decl: dict) -> list[str]:
    out: list[str] = []
    if decl.get("version") != 1:
        out.append(f"{DECLARATION}: version must be 1")
    layers = decl.get("layers", {})
    for name, layer in layers.items():
        if layer.get("parent") and layer["parent"] not in layers:
            out.append(f"layer {name}: parent {layer['parent']!r} is not a layer")
        for entry in layer.get("entries", []):
            if not (root / entry).is_file():
                out.append(f"layer {name}: entry {entry} does not exist")
        for pattern in layer.get("globs", []):
            if not _matches(root, pattern):
                out.append(f"layer {name}: glob {pattern} matches no file")
        for listed in [*layer.get("files", []), *layer.get("paths", [])]:
            if not (root / listed).is_file():
                out.append(f"layer {name}: {listed} does not exist")
    for i, group in enumerate(decl.get("outside", [])):
        if not str(group.get("reason") or "").strip():
            out.append(f"outside group {i}: no reason")
        for pattern in group.get("patterns", []):
            if not _matches(root, pattern):
                out.append(f"outside group {i}: pattern {pattern} matches no file")
        for layer in group.get("not_for", []):
            if layer not in layers:
                out.append(f"outside group {i}: not_for names unknown layer {layer!r}")
    for name in layers:
        files = layer_files(root, decl, name)
        outside = _outside_for(decl, name)
        for rel in sorted(f for f in files if f.endswith(".py") and (root / f).is_file()):
            for dep in sorted(direct_imports(root, rel)):
                if dep in files or any(fnmatch.fnmatch(dep, p) for p in outside):
                    continue
                out.append(f"layer {name}: {rel} imports {dep}, which is neither in the layer nor outside; "
                           f"add it to [layers.{name}].files or to an [[outside]] group with a reason")
    return out


def test_layer_surface_matches_code():
    assert problems(ROOT, load(ROOT)) == []


# --------------------------------------------------------------- the check catches bad moves
FAKE = {
    "src/menhir/__init__.py": "",
    "src/menhir/services/__init__.py": "",
    "src/menhir/services/ingest_worker.py": "from menhir.services import ingest_steps\nfrom ..infra import store\n",
    "src/menhir/services/ingest_steps.py": "from menhir.infra.store import write\n",
    "src/menhir/infra/__init__.py": "",
    "src/menhir/infra/store.py": "def write():\n    pass\n",
    "src/menhir/infra/telemetry.py": "",
    "uv.lock": "",
}
FAKE_DECL = """
version = 1
[layers.ingest]
entries = ["src/menhir/services/ingest_worker.py"]
globs = ["src/menhir/services/ingest_*.py"]
files = ["src/menhir/infra/store.py", "src/menhir/__init__.py", "src/menhir/services/__init__.py",
         "src/menhir/infra/__init__.py"]
paths = ["uv.lock"]
[[outside]]
reason = "telemetry"
patterns = ["src/menhir/infra/telemetry.py"]
"""


def _fake(tmp_path: Path) -> Path:
    for rel, text in FAKE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    (tmp_path / DECLARATION).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / DECLARATION).write_text(FAKE_DECL, encoding="utf-8")
    return tmp_path


def test_fake_package_is_clean(tmp_path):
    root = _fake(tmp_path)
    assert problems(root, load(root)) == []


def test_moving_covered_code_to_an_uncovered_module_fails(tmp_path):
    root = _fake(tmp_path)
    shutil.move(root / "src/menhir/infra/store.py", root / "src/menhir/infra/graph_store.py")
    (root / "src/menhir/services/ingest_steps.py").write_text("from menhir.infra.graph_store import write\n")
    (root / "src/menhir/services/ingest_worker.py").write_text("from menhir.services import ingest_steps\n")
    found = problems(root, load(root))
    assert any("store.py does not exist" in p for p in found)
    assert any("imports src/menhir/infra/graph_store.py" in p for p in found)


def test_renaming_a_glob_target_fails(tmp_path):
    root = _fake(tmp_path)
    for name in ("ingest_worker.py", "ingest_steps.py"):
        shutil.move(root / "src/menhir/services" / name, root / "src/menhir/services" / name.replace("ingest", "intake"))
    found = problems(root, load(root))
    assert any("glob src/menhir/services/ingest_*.py matches no file" in p for p in found)
    assert any("entry src/menhir/services/ingest_worker.py does not exist" in p for p in found)


def test_new_import_of_an_unlisted_module_fails_and_outside_allows_it(tmp_path):
    root = _fake(tmp_path)
    (root / "src/menhir/infra/cache.py").write_text("")
    (root / "src/menhir/services/ingest_steps.py").write_text(
        "from menhir.infra.store import write\nfrom menhir.infra import cache, telemetry\n")
    found = problems(root, load(root))
    assert found == [p for p in found if "imports src/menhir/infra/cache.py" in p] and len(found) == 1


def test_outside_group_needs_a_reason_and_live_patterns(tmp_path):
    root = _fake(tmp_path)
    (root / DECLARATION).write_text(FAKE_DECL.replace('reason = "telemetry"', 'reason = ""')
                                    .replace("infra/telemetry.py", "infra/gone.py"))
    found = problems(root, load(root))
    assert any("no reason" in p for p in found)
    assert any("pattern src/menhir/infra/gone.py matches no file" in p for p in found)
