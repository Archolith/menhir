"""AST evidence for the feature registry's development-time consistency checks."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# Exact reviewed dynamic boundaries. Unknown expressions/functions fail closed for review.
DYNAMIC_READS = {
    ("config/settings_helpers.py", "_getenv", "key"): "Canonical primary/alias wrapper; inspect its callers.",
    ("config/oauth.py", "_get_setting", "key"): "Legacy OAuth snapshot wrapper; inspect its callers.",
    ("infrastructure/audit_trail.py", "__init__", "env_var"): "AuditChannel constructor; inspect its callers.",
    ("infrastructure/paths.py", "_env_path", "name"): "Path wrapper; inspect its callers.",
    ("services/verifier_sync.py", "env_key_executor", "key"): "Runtime verifier parameters; not a finite product setting.",
}
WRAPPERS = {"_getenv": (0, True), "_env_path": (0, False),
            "_get_setting": (2, True), "AuditChannel": (1, False)}


@dataclass
class SourceEvidence:
    reads: dict[str, set[str]] = field(default_factory=dict)
    raw_defaults: dict[str, set[str]] = field(default_factory=dict)
    unresolved: set[tuple[str, str, str]] = field(default_factory=set)
    dynamic: set[tuple[str, str, str]] = field(default_factory=set)
    consumers: set[str] = field(default_factory=set)
    bridges: dict[str, str] = field(default_factory=dict)
    bindings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    legacy_bindings: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)


def inspect_source(sources: dict[str, str]) -> SourceEvidence:
    evidence = SourceEvidence()
    for path, source in sources.items():
        tree = ast.parse(source)
        constants = {}
        imports = {"os": "os"}
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.update({a.asname or a.name: a.name for a in node.names})
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                imports.update({a.asname or a.name: f"os.{a.name}" for a in node.names})
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if isinstance(node.value, ast.Constant):
                    constants[node.targets[0].id] = node.value.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if isinstance(node.value, ast.Constant):
                    constants[node.target.id] = node.value.value

        def spelling(node: ast.AST) -> str:
            if isinstance(node, ast.Name):
                return imports.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return f"{spelling(node.value)}.{node.attr}"
            return ast.unparse(node)

        def value(node: ast.AST):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return constants.get(node.id)
            return None

        class Visitor(ast.NodeVisitor):
            function = "<module>"

            def visit_Import(self, node: ast.Import) -> None:
                imports.update({a.asname or a.name: a.name for a in node.names})

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if node.module == "os":
                    imports.update({a.asname or a.name: f"os.{a.name}" for a in node.names})

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                previous = self.function
                self.function = node.name
                self.generic_visit(node)
                self.function = previous

            visit_AsyncFunctionDef = visit_FunctionDef

            def record(self, key: ast.AST, default: ast.AST | None = None) -> None:
                resolved = value(key)
                if isinstance(resolved, str):
                    if resolved.startswith("MENHIR_"):
                        evidence.reads.setdefault(resolved, set()).add(path)
                        evidence.raw_defaults.setdefault(resolved, set()).add(
                            ast.unparse(default) if default is not None else "None")
                else:
                    boundary = (path, self.function, ast.unparse(key))
                    if boundary in DYNAMIC_READS:
                        evidence.dynamic.add(boundary)
                    else:
                        evidence.unresolved.add(boundary)

            def visit_Call(self, node: ast.Call) -> None:
                name = spelling(node.func)
                keywords = {k.arg: k.value for k in node.keywords}
                if name in {"os.getenv", "os.environ.get", "os.environ.setdefault"}:
                    key = node.args[0] if node.args else keywords.get("key")
                    default = node.args[1] if len(node.args) > 1 else keywords.get("default")
                    if key is not None:
                        self.record(key, default)
                    else:
                        self.record(ast.Name(id="<unresolved-key>"))
                elif name in WRAPPERS:
                    index, aliases = WRAPPERS[name]
                    keys = node.args[index:index + 1]
                    parameter = {"_getenv": "primary", "_env_path": "name",
                                 "_get_setting": "env_var", "AuditChannel": "env_var"}[name]
                    if not keys and parameter in keywords:
                        keys = [keywords[parameter]]
                    if aliases:
                        keys += node.args[4:] if name == "_get_setting" else node.args[index + 1:]
                    for key in keys:
                        self.record(key, ast.Constant("") if name == "_env_path" else None)
                    if name == "_get_setting":
                        attr = node.args[1] if len(node.args) > 1 else keywords.get("attr")
                        if isinstance(attr, ast.Constant) and all(isinstance(value(k), str) for k in keys):
                            evidence.legacy_bindings.setdefault(str(attr.value), set()).add(
                                tuple(value(k) for k in keys))
                if path != "config/settings_model.py":
                    if name == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        evidence.consumers.add(str(node.args[1].value))
                    if name == "_get_setting" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        evidence.consumers.add(str(node.args[1].value))
                self.generic_visit(node)

            def visit_Subscript(self, node: ast.Subscript) -> None:
                if spelling(node.value) == "os.environ" and isinstance(node.ctx, ast.Load):
                    self.record(node.slice)
                self.generic_visit(node)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if path != "config/settings_model.py" and isinstance(node.ctx, ast.Load):
                    evidence.consumers.add(node.attr)
                self.generic_visit(node)

        Visitor().visit(tree)
        if path == "config/settings_model.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword):
                    calls = [c for c in ast.walk(node.value) if isinstance(c, ast.Call)
                             and isinstance(c.func, ast.Name) and c.func.id == "_getenv"]
                    if calls:
                        evidence.bindings[node.arg] = tuple(a.value for a in calls[0].args
                            if isinstance(a, ast.Constant) and isinstance(a.value, str))
                    if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name):
                        if node.value.value.id == "self":
                            evidence.bridges[node.value.attr] = str(node.arg)
    return evidence


def read_sources(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in root.rglob("*.py") if p.relative_to(root).as_posix() != "config/feature_flags.py"}
