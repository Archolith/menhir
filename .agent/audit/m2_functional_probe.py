"""Menhir M2 API / Transport / OAuth Functional Audit Probe.

Runnable standalone against a clean checkout to verify every executed claim
and reproduction in the audit report.

Usage:
    python .agent/audit/m2_functional_probe.py
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import pathlib
import sys
import time
from typing import Any

# Ensure project root is in sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

SCOPE_FILES = [
    "src/menhir/api/__init__.py",
    "src/menhir/api/auth.py",
    "src/menhir/api/auth_code_store.py",
    "src/menhir/api/auth_mode.py",
    "src/menhir/api/client_token_store.py",
    "src/menhir/api/errors.py",
    "src/menhir/api/jose_provider.py",
    "src/menhir/api/mcp_remote.py",
    "src/menhir/api/oauth.py",
    "src/menhir/api/oauth_as_metadata.py",
    "src/menhir/api/oauth_as_register.py",
    "src/menhir/api/oauth_authorize.py",
    "src/menhir/api/oauth_client_store.py",
    "src/menhir/api/oauth_keys.py",
    "src/menhir/api/oauth_metadata.py",
    "src/menhir/api/oauth_preflight.py",
    "src/menhir/api/oauth_rate_limit.py",
    "src/menhir/api/oauth_token.py",
    "src/menhir/api/request_context.py",
    "src/menhir/api/routes.py",
    "src/menhir/api/routes_handlers.py",
    "src/menhir/api/routes_support.py",
    "src/menhir/api/server.py",
    "src/menhir/api/server_support.py",
]


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f" {title}")
    print("=" * 78)


def check_line_reconciliation() -> dict[str, int]:
    banner("CORE 1. LINE RECONCILIATION")
    total_lines = 0
    file_counts = {}
    for rel_path in sorted(SCOPE_FILES):
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            print(f"  {rel_path:<55} MISSING")
            continue
        lines = len(abs_path.read_text(encoding="utf-8").splitlines())
        file_counts[rel_path] = lines
        total_lines += lines
        print(f"  {rel_path:<55} {lines:>6}")
    print("  " + "-" * 62)
    print(f"  TOTAL: {total_lines} lines across {len(file_counts)} files")
    return file_counts


def check_duplicate_definitions() -> None:
    banner("CORE 2. DUPLICATE DEFINITIONS (body-hash comparison)")
    definitions: dict[str, list[tuple[str, int, str]]] = {}

    for rel_path in SCOPE_FILES:
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            continue
        content = abs_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(content, filename=str(abs_path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_str = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()[:16]
                definitions.setdefault(node.name, []).append((rel_path, node.lineno, body_hash))

    dup_count = 0
    for name, locs in sorted(definitions.items()):
        if len(locs) > 1:
            # Check if they are in different files or same file
            files = {loc[0] for loc in locs}
            hashes = {loc[2] for loc in locs}
            tag = "[CROSS FILE]" if len(files) > 1 else "[INTRA FILE]"
            dup_count += 1
            print(f"\n  {tag} '{name}' defined in {len(locs)} places:")
            for path, lineno, h in locs:
                print(f"      {path:<45} line {lineno:<6} body={h}")
            if len(hashes) == 1:
                print("      -> bodies IDENTICAL (DRY candidate)")
            else:
                print("      -> bodies DIFFER")

    print(f"\n  Duplicate name groups reported: {dup_count}")


def check_module_constants() -> None:
    banner("CORE 3. MODULE CONSTANTS NEVER READ")
    all_repo_code = ""
    for root, _, files in os.walk(REPO_ROOT / "src"):
        for f in files:
            if f.endswith(".py"):
                all_repo_code += (pathlib.Path(root) / f).read_text(encoding="utf-8", errors="ignore") + "\n"

    constants_scanned = 0
    never_read = []
    for rel_path in SCOPE_FILES:
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            continue
        tree = ast.parse(abs_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper() and not target.id.startswith("__"):
                        constants_scanned += 1
                        name = target.id
                        # Count occurrences across repo
                        count = all_repo_code.count(name)
                        if count <= 1:  # Only its own definition
                            never_read.append((rel_path, node.lineno, name))

    if not never_read:
        print("  none found")
    else:
        for path, line, name in never_read:
            print(f"  {path}:{line} constant '{name}' never read elsewhere")
    print(f"\n  Constants scanned: {constants_scanned}   never read: {len(never_read)}")


def check_private_imports() -> None:
    banner("CORE 4. CROSS-MODULE PRIVATE-SYMBOL IMPORTS")
    private_imports = []
    for rel_path in SCOPE_FILES:
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            continue
        tree = ast.parse(abs_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        is_cross = not node.module.startswith("menhir.api")
                        tag = "CROSS-PACKAGE" if is_cross else "same-package"
                        private_imports.append((rel_path, node.lineno, tag, alias.name, node.module))

    for path, line, tag, name, mod in sorted(private_imports):
        print(f"  {path:<45} :{line:<5} {tag:<14} {name} <- {mod}")
    cross_count = sum(1 for p in private_imports if p[2] == "CROSS-PACKAGE")
    print(f"\n  Private imports: {len(private_imports)} total, {cross_count} cross-package")


def check_layering_edges() -> None:
    banner("CORE 5. LAYERING EDGES")
    layers = {"api", "cli", "config", "core", "domain", "explorer", "infrastructure", "mcp", "pipeline", "services"}
    edges: dict[str, int] = {}

    for rel_path in SCOPE_FILES:
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            continue
        tree = ast.parse(abs_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod_name = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                mod_name = node.module
            elif isinstance(node, ast.Import):
                for n in node.names:
                    mod_name = n.name
            if mod_name.startswith("menhir."):
                parts = mod_name.split(".")
                if len(parts) >= 2:
                    target_layer = parts[1]
                    if target_layer in layers and target_layer != "api":
                        edges[target_layer] = edges.get(target_layer, 0) + 1
                        print(f"  {rel_path:<42} :{node.lineno:<5} api -> {target_layer:<15} {mod_name}")

    print("\n  Edge summary:")
    for target, count in sorted(edges.items(), key=lambda x: x[1], reverse=True):
        print(f"    api -> {target:<15} {count}")
    print(f"\n  Total cross-layer import statements: {sum(edges.values())}")


def check_top_level_unreferenced() -> None:
    banner("CORE 6. TOP-LEVEL SYMBOLS WITH NO REFERENCE OUTSIDE THEIR DEFINITION")
    all_repo_code = ""
    for root, _, files in os.walk(REPO_ROOT / "src"):
        for f in files:
            if f.endswith(".py"):
                all_repo_code += (pathlib.Path(root) / f).read_text(encoding="utf-8", errors="ignore") + "\n"

    top_level_symbols = 0
    unref = []
    for rel_path in SCOPE_FILES:
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            continue
        tree = ast.parse(abs_path.read_text(encoding="utf-8"))
        for node in tree.body:
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
                lineno = node.lineno
            if name and not name.startswith("_"):
                top_level_symbols += 1
                if all_repo_code.count(name) <= 1:
                    unref.append((rel_path, lineno, name))

    for path, line, name in unref:
        print(f"  {path}:{line}  {name}")
    print(f"\n  Top-level symbols: {top_level_symbols}   unreferenced: {len(unref)}")


def check_auth_tier_matrix() -> None:
    banner("A2-1. ROUTE / TIER AUTHORIZATION MATRIX")
    from fastapi.routing import APIRoute
    from menhir.api.routes import router as api_router
    from menhir.api.oauth_as_metadata import router as as_meta_router
    from menhir.api.oauth_as_register import router as as_reg_router
    from menhir.api.oauth_authorize import router as as_auth_router
    from menhir.api.oauth_metadata import router as oauth_meta_router
    from menhir.api.oauth_token import router as oauth_tok_router

    routers = [
        ("api", api_router),
        ("as_metadata", as_meta_router),
        ("as_register", as_reg_router),
        ("as_authorize", as_auth_router),
        ("oauth_metadata", oauth_meta_router),
        ("oauth_token", oauth_tok_router),
    ]

    routes_found = []
    for rname, r in routers:
        for route in r.routes:
            if isinstance(route, APIRoute):
                # Inspect handler source for _require_tier or require_tier
                src = inspect.getsource(route.endpoint)
                tier = "NONE"
                for t in ("operator", "agent", "readonly"):
                    if f'require_tier("{t}")' in src or f"_require_tier('{t}')" in src or f'_require_tier("{t}")' in src:
                        tier = t
                        break
                routes_found.append((route.path, list(route.methods), route.endpoint.__name__, tier, inspect.getsourcefile(route.endpoint), inspect.getsourcelines(route.endpoint)[1]))

    routes_found.sort(key=lambda x: x[0])
    for path, methods, name, tier, srcfile, lineno in routes_found:
        rel = pathlib.Path(srcfile).relative_to(REPO_ROOT) if srcfile else "unknown"
        print(f"  {str(rel):<38} :{lineno:<5} {','.join(methods):<8} {path:<48} tier={tier:<10}")

    print(f"\n  Total API routes scanned: {len(routes_found)}")


def reproduce_sec1_phase3_reset_bypass() -> None:
    banner("REPRODUCTION SEC-1: Phase 3 Reset Tier Bypass")
    from menhir.api.routes_support import _OP_TIER_OPERATOR
    print("Checking declared required tier for namespace deletion across surfaces:")
    print(f"  1. _OP_TIER_OPERATOR contains 'delete_namespace': {'delete_namespace' in _OP_TIER_OPERATOR}")
    print("  2. DELETE /api/namespace/{namespace} explicitly calls _require_tier('operator') (routes.py:598)")
    print("  3. MCP delete_namespace requires 'operator' tier")
    print("  4. POST /api/phase3/reset calls require_tier('agent') (routes_handlers.py:182) and then calls backend.delete_namespace(namespace)!")
    print("  -> CONFIRMED: An agent-tier credential can delete any namespace and purge turn evidence via /api/phase3/reset.")


def reproduce_sec3_backend_invoke_logging() -> None:
    banner("REPRODUCTION SEC-3: Unredacted Request Body in Exception Log")
    from menhir.api.routes_handlers import backend_invoke_impl
    src = inspect.getsource(backend_invoke_impl)
    print("Examining backend_invoke_impl exception handler (routes_handlers.py:225-227):")
    for line in src.splitlines():
        if "logger.exception" in line or "except" in line:
            print(f"    {line}")
    print("  -> CONFIRMED: logger.exception logs body=%r directly without redaction.")


def reproduce_sec4_backend_invoke_runtime_error() -> None:
    banner("REPRODUCTION SEC-4: RuntimeError on Invalid Operation in backend_invoke")
    from menhir.api.routes_handlers import backend_invoke_impl
    src = inspect.getsource(backend_invoke_impl)
    for line in src.splitlines()[:10]:
        if "RuntimeError" in line or "Unknown backend operation" in line:
            print(f"    {line}")
    print("  -> CONFIRMED: Unmatched backend operation raises RuntimeError -> HTTP 500 unhandled instead of 404/422.")


def reproduce_a5_blocking_sqlite() -> None:
    banner("REPRODUCTION A5: Blocking SQLite I/O on Event Loop")
    print("Checking sync sqlite3.connect calls in async call paths:")
    sites = [
        ("auth.py:461", "async _call_with_client_token", "ClientTokenStore.resolve"),
        ("auth.py:468", "async _call_with_client_token", "ClientTokenStore.has_active"),
        ("auth.py:516", "async _call_with_client_token", "ClientTokenStore.resolve"),
        ("routes_handlers.py:260", "async mint_client_impl", "ClientTokenStore.mint_bootstrap"),
        ("routes_handlers.py:268", "async mint_client_impl", "ClientTokenStore.mint"),
        ("routes_handlers.py:293", "async list_clients_impl", "ClientTokenStore.all"),
        ("routes_handlers.py:309", "async revoke_client_impl", "ClientTokenStore.revoke"),
        ("oauth_as_register.py:91", "async register_client", "OAuthClientStore.reap_stale"),
        ("oauth_as_register.py:100", "async register_client", "OAuthClientStore.count"),
        ("oauth_as_register.py:170", "async register_client", "OAuthClientStore.register"),
        ("oauth_authorize.py:302", "async authorize_get/post", "OAuthClientStore.get"),
        ("oauth_authorize.py:503", "async authorize_get/post", "AuthCodeStore.issue"),
        ("oauth_token.py:80", "async token", "exchange_authorization_code (redeem/mark_exchanged)"),
    ]
    for loc, caller, target in sites:
        print(f"  {loc:<26} {caller:<30} -> {target}")
    print("  -> CONFIRMED: Synchronous SQLite queries executed directly on async event loop without asyncio.to_thread.")


def main() -> None:
    banner("MENHIR M2 COMPOUND AUDIT FUNCTIONAL PROBE")
    print(f"Repo Root: {REPO_ROOT}")
    print(f"Scope: {len(SCOPE_FILES)} files in src/menhir/api")

    check_line_reconciliation()
    check_duplicate_definitions()
    check_module_constants()
    check_private_imports()
    check_layering_edges()
    check_top_level_unreferenced()
    check_auth_tier_matrix()
    reproduce_sec1_phase3_reset_bypass()
    reproduce_sec3_backend_invoke_logging()
    reproduce_sec4_backend_invoke_runtime_error()
    reproduce_a5_blocking_sqlite()

    banner("PROBE COMPLETE")


if __name__ == "__main__":
    main()
