#!/usr/bin/env python3
"""Rerunnable evidence probe for the Menhir M2 API/OAuth compound audit.

Run from the audit branch (or a checkout whose scoped tree is identical):

    python .agent/audit/m2_functional_probe.py
    python .agent/audit/m2_functional_probe.py --strict

The probe is read-only except for a temporary SQLite database used by the
optional pinned ``archolith_oauth`` authorization-code reproduction.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

AUDITED_COMMIT = "eebf6d6dd83f15083167bf847b639d24b953fdc9"
EXPECTED_LINES: dict[str, int] = {
    "src/menhir/api/__init__.py": 2,
    "src/menhir/api/auth.py": 676,
    "src/menhir/api/auth_code_store.py": 91,
    "src/menhir/api/auth_mode.py": 15,
    "src/menhir/api/client_token_store.py": 283,
    "src/menhir/api/errors.py": 61,
    "src/menhir/api/jose_provider.py": 110,
    "src/menhir/api/mcp_remote.py": 111,
    "src/menhir/api/oauth.py": 287,
    "src/menhir/api/oauth_as_metadata.py": 65,
    "src/menhir/api/oauth_as_register.py": 197,
    "src/menhir/api/oauth_authorize.py": 684,
    "src/menhir/api/oauth_client_store.py": 65,
    "src/menhir/api/oauth_keys.py": 80,
    "src/menhir/api/oauth_metadata.py": 77,
    "src/menhir/api/oauth_preflight.py": 287,
    "src/menhir/api/oauth_rate_limit.py": 145,
    "src/menhir/api/oauth_token.py": 109,
    "src/menhir/api/request_context.py": 71,
    "src/menhir/api/routes.py": 799,
    "src/menhir/api/routes_handlers.py": 312,
    "src/menhir/api/routes_support.py": 710,
    "src/menhir/api/server.py": 87,
    "src/menhir/api/server_support.py": 241,
}
EXPECTED_TOTAL = 5_565


@dataclass(frozen=True)
class Result:
    status: str
    label: str
    detail: str = ""
    mandatory: bool = False


class Results:
    def __init__(self) -> None:
        self.items: list[Result] = []

    def add(self, status: str, label: str, detail: str = "", *, mandatory: bool = False) -> None:
        self.items.append(Result(status, label, detail, mandatory))

    def render(self) -> int:
        for item in self.items:
            suffix = f" — {item.detail}" if item.detail else ""
            print(f"[{item.status}] {item.label}{suffix}")
        counts = Counter(item.status for item in self.items)
        order = ("CONFIRMED", "PASS", "NOT FOUND", "SKIP", "FAIL")
        print("\nsummary: " + ", ".join(f"{key}={counts[key]}" for key in order))
        failures = counts["FAIL"]
        print(f"strict_failures={failures}")
        return failures


def root_dir() -> Path:
    override = os.environ.get("MENHIR_M2_PROBE_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parents[2]


def text(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def normalized(value: str) -> str:
    return " ".join(value.split())


def command(args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )


def reconcile(root: Path, out: Results) -> None:
    api_dir = root / "src/menhir/api"
    if not api_dir.is_dir():
        out.add("FAIL", "scope directory", str(api_dir), mandatory=True)
        return
    actual = {
        path.relative_to(root).as_posix()
        for path in api_dir.rglob("*.py") if path.is_file()
    }
    expected = set(EXPECTED_LINES)
    if actual == expected:
        out.add("PASS", "exact scoped file set", f"{len(actual)} files", mandatory=True)
    else:
        out.add(
            "FAIL", "exact scoped file set",
            f"missing={sorted(expected-actual)} extra={sorted(actual-expected)}",
            mandatory=True,
        )

    mismatches: list[str] = []
    total = 0
    for relative, expected_count in EXPECTED_LINES.items():
        path = root / relative
        if not path.exists():
            mismatches.append(f"{relative}: missing")
            continue
        count = len(path.read_text(encoding="utf-8").splitlines())
        total += count
        if count != expected_count:
            mismatches.append(f"{relative}: expected {expected_count}, got {count}")
    if mismatches:
        out.add("FAIL", "per-file line counts", "; ".join(mismatches), mandatory=True)
    else:
        out.add("PASS", "per-file line counts", "24/24 exact", mandatory=True)
    out.add(
        "PASS" if total == EXPECTED_TOTAL else "FAIL",
        "scope line total", f"expected={EXPECTED_TOTAL} observed={total}", mandatory=True,
    )


def verify_target(root: Path, out: Results) -> None:
    head = command(["git", "rev-parse", "HEAD"], cwd=root)
    if head.returncode:
        out.add("SKIP", "git target verification", head.stdout.strip() or "git unavailable")
        return
    ancestor = command(["git", "merge-base", "--is-ancestor", AUDITED_COMMIT, "HEAD"], cwd=root)
    scoped_diff = command(
        ["git", "diff", "--quiet", AUDITED_COMMIT, "--", "src/menhir/api"], cwd=root,
    )
    ok = ancestor.returncode == 0 and scoped_diff.returncode == 0
    out.add(
        "PASS" if ok else "FAIL", "audited target and scoped-tree identity",
        f"target={AUDITED_COMMIT} head={head.stdout.strip()}", mandatory=True,
    )


def parse_modules(root: Path, out: Results) -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    failures: list[str] = []
    for relative in EXPECTED_LINES:
        try:
            trees[relative] = ast.parse(text(root, relative), filename=relative)
        except (OSError, SyntaxError) as exc:
            failures.append(f"{relative}: {exc}")
    out.add(
        "PASS" if not failures else "FAIL", "all scoped modules parse",
        f"{len(trees)}/24" if not failures else "; ".join(failures), mandatory=True,
    )
    return trees


def duplicate_sweep(trees: dict[str, ast.Module], out: Results) -> None:
    duplicates: list[str] = []

    def inspect(relative: str, scope: str, body: list[ast.stmt]) -> None:
        definitions: defaultdict[str, list[int]] = defaultdict(list)
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions[node.name].append(node.lineno)
                inspect(relative, f"{scope}.{node.name}", node.body)
        for name, lines in definitions.items():
            if len(lines) > 1:
                duplicates.append(f"{relative}:{scope}:{name}@{lines}")

    for relative, tree in trees.items():
        inspect(relative, "<module>", tree.body)
    out.add(
        "CONFIRMED" if duplicates else "NOT FOUND", "duplicate-definition sweep",
        "; ".join(duplicates) if duplicates else "no duplicate def/class names in one lexical scope",
    )


def pyflakes_and_except_sweep(root: Path, trees: dict[str, ast.Module], out: Results) -> None:
    proc = command([sys.executable, "-m", "pyflakes", "src/menhir/api"], cwd=root)
    output = proc.stdout.strip()
    missing = "No module named pyflakes" in output or "No module named 'pyflakes'" in output
    if proc.returncode == 0:
        out.add("PASS", "python -m pyflakes src/menhir/api", output or "clean")
    elif missing:
        out.add("SKIP", "python -m pyflakes src/menhir/api", output)
    else:
        out.add("CONFIRMED", "python -m pyflakes src/menhir/api", output or f"exit={proc.returncode}")

    suspects: list[str] = []

    def bound(nodes: Iterable[ast.AST]) -> set[str]:
        names: set[str] = set()
        for node in nodes:
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    names.add(child.id)
        return names

    def loaded(nodes: Iterable[ast.AST]) -> set[str]:
        names: set[str] = set()
        for node in nodes:
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    names.add(child.id)
        return names

    def inspect(relative: str, scope: str, body: list[ast.stmt]) -> None:
        for index, node in enumerate(body):
            if isinstance(node, ast.Try):
                handler_names: set[str] = set()
                for handler in node.handlers:
                    if handler.name:
                        handler_names.add(handler.name)
                    handler_names |= bound(handler.body)
                normal_names = bound([*node.body, *node.orelse, *node.finalbody])
                risky = sorted((handler_names - normal_names) & loaded(body[index + 1 :]))
                if risky:
                    suspects.append(f"{relative}:{node.lineno}:{scope}:{risky}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inspect(relative, f"{scope}.{node.name}", node.body)

    for relative, tree in trees.items():
        inspect(relative, "<module>", tree.body)
    out.add(
        "CONFIRMED" if suspects else "NOT FOUND", "except-only-name AST fallback",
        "; ".join(suspects) if suspects else "no conservative matches",
    )


def cancellation_sweep(root: Path, out: Results) -> None:
    source = text(root, "src/menhir/api/server_support.py")
    start = source.find("ctx: RuntimeContext = await start_runtime(settings)")
    manager = source.find("async with mcp_http_instance.session_manager.run():")
    stop = source.find("await stop_runtime()", manager)
    if -1 not in (start, manager, stop) and start < manager < stop:
        out.add(
            "CONFIRMED", "BaseException/CancelledError cleanup sweep",
            "runtime starts before the only cleanup-protected MCP manager context",
        )
    else:
        out.add("NOT FOUND", "BaseException/CancelledError cleanup sweep", "audited ordering absent")


def timestamp_sweep(trees: dict[str, ast.Module], out: Results) -> None:
    timestamp_hint = re.compile(r"(?:time|date|created|updated|expires|redeemed|occurred|verified|mtime)", re.I)
    string_candidates: list[str] = []
    for relative, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.unparse(node)
            if not timestamp_hint.search(rendered):
                continue
            if any(isinstance(item, ast.Constant) and isinstance(item.value, str)
                   for item in [node.left, *node.comparators]) or re.search(r"strftime|isoformat|_iso\b|_str\b", rendered):
                string_candidates.append(f"{relative}:{node.lineno}:{rendered}")
    out.add(
        "CONFIRMED" if string_candidates else "NOT FOUND", "lexicographic timestamp sweep",
        "; ".join(string_candidates) if string_candidates else "no string-timestamp ordering candidates",
    )


def constant_sweep(trees: dict[str, ast.Module], out: Results) -> None:
    candidates: list[str] = []
    for relative, tree in trees.items():
        assigned: dict[str, int] = {}
        loads: Counter[str] = Counter()
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", target.id):
                    assigned[target.id] = node.lineno
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loads[node.id] += 1
        for name, line in assigned.items():
            if loads[name] == 0:
                candidates.append(f"{relative}:{line}:{name}")
    out.add(
        "CONFIRMED" if candidates else "NOT FOUND", "module-constant unread sweep",
        "; ".join(candidates) if candidates else "no module-level uppercase constant is unread locally",
    )


def kwarg_sweep(trees: dict[str, ast.Module], out: Results) -> None:
    mismatches: list[str] = []
    for relative, tree in trees.items():
        signatures: dict[str, tuple[set[str], bool]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
                signatures[node.name] = (parameters, node.args.kwarg is not None)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            signature = signatures.get(node.func.id)
            if signature is None or signature[1]:
                continue
            unexpected = sorted(
                keyword.arg for keyword in node.keywords
                if keyword.arg is not None and keyword.arg not in signature[0]
            )
            if unexpected:
                mismatches.append(f"{relative}:{node.lineno}:{node.func.id}:{unexpected}")
    out.add(
        "CONFIRMED" if mismatches else "NOT FOUND", "literal keyword-contract sweep",
        "; ".join(mismatches) if mismatches else "no same-module literal mismatch",
    )


# Normalized source fragments for every report finding other than M1, which has
# a live pinned-store reproduction below.  Multiple small fragments are used
# instead of one formatting-sensitive block.
FINDING_FRAGMENTS: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {
    "C1 static-key client-policy relabel": (
        "credential tier is authenticated while caller client_name selects MCP policy",
        {
            "src/menhir/api/auth.py": (
                "trust_identity_headers: bool = True",
                'client_name = _identity_header(headers, b"client-name")',
                "client_name=client_name",
            ),
            "src/menhir/mcp/service_access.py": (
                "client_name = (getattr(session, \"client_name\", \"\") or \"\").strip().lower()",
                "return (settings.client_namespaces or {}).get(client_name, \"\")",
                "return (settings.client_tools or {}).get(client_name, frozenset())",
            ),
            "src/menhir/mcp/contracts.py": (
                "allowlist = get_client_tool_allowlist()",
                "pinned = get_pinned_namespace()",
            ),
        },
    ),
    "C2 agent-tier destructive Phase 3 reset": (
        "agent gate reaches namespace deletion and evidence purge; generic policy marks delete_namespace operator",
        {
            "src/menhir/api/routes_handlers.py": (
                'require_tier("agent")', "backend.delete_namespace(namespace)", "adapter.purge_turn_evidence",
            ),
            "src/menhir/api/routes_support.py": ('"delete_memory", "delete_namespace",',),
        },
    ),
    "C3 proxied Explorer bypass on loopback bind": (
        "loopback bind is an independent OR authorization despite forwarding-header-aware direct_loopback",
        {
            "src/menhir/api/auth.py": (
                "direct_loopback = self._client_is_loopback(scope) and not self._has_proxy_forwarding_header(headers)",
                "if is_explorer and (self._loopback_admin_ok or direct_loopback):",
            ),
        },
    ),
    "C4 loopback no-auth DNS-rebinding exposure": (
        "MCP SDK host protection is disabled and AuthMode.NONE forwards without a replacement Host gate",
        {
            "src/menhir/api/mcp_remote.py": ('host="0.0.0.0"', "disables DNS rebinding protection"),
            "src/menhir/api/auth.py": ("if self._auth_mode is AuthMode.NONE:", "await self.app(scope, receive, send)"),
        },
    ),
    "H1 body identity overrides verified caller": (
        "authenticated session is resolved, then body user_id/session_id replaces it",
        {
            "src/menhir/api/routes.py": (
                "caller_session = _resolve_caller_session(request)",
                "if body.user_id is not None:",
                "session = new_session(body.user_id, session_id=body.session_id)",
            ),
        },
    ),
    "H2 unbounded agent-controlled LLM budget": (
        "unbounded schema value reaches real personal-memory consolidation",
        {
            "src/menhir/api/routes_support.py": ("call_budget: int | None = None",),
            "src/menhir/api/routes_handlers.py": ('require_tier("agent")', "call_budget=body.call_budget", "make_sync_chat("),
        },
    ),
    "M2 early-start cancellation cleanup gap": (
        "start_runtime precedes the context containing stop_runtime",
        {
            "src/menhir/api/server_support.py": (
                "ctx: RuntimeContext = await start_runtime(settings)",
                "async with mcp_http_instance.session_manager.run():",
                "await stop_runtime()",
            ),
        },
    ),
    "M3 deny cycles grow consent-JTI cache": (
        "JTI is stored before deny decision and deny is outside approve limiter",
        {
            "src/menhir/api/oauth_authorize.py": (
                "_spent_consent_jtis[jti] = now + ttl", 'if decision != "approve":', "_approve_limiter.allow",
            ),
        },
    ),
    "M4 non-atomic DCR hard cap": (
        "count and registration are separate calls/transactions",
        {"src/menhir/api/oauth_as_register.py": ("get_client_store().count()", "get_client_store().register(")},
    ),
    "M5 preflight scheme validation fails open": (
        "non-http branches and exceptions return true",
        {"src/menhir/api/oauth_preflight.py": ('if parsed.scheme == "http":', "return True", "except Exception:")},
    ),
    "M6 malformed-port redaction fail-open": (
        "parsed.port can raise and exception path returns original URL",
        {"src/menhir/api/oauth_preflight.py": ("if parsed.port:", "except Exception:", "return url")},
    ),
    "M7 authorize/token resource mismatch": (
        "authorization binds arbitrary nonempty resource while token exchange applies AS configuration",
        {
            "src/menhir/api/oauth_authorize.py": (
                "bound_resource = resource or build_oauth_config(settings).resource",
                "resource=bound_resource", "get_auth_code_store().issue(",
            ),
            "src/menhir/api/oauth_token.py": ("build_authorization_server_config(settings)", "exchange_authorization_code("),
        },
    ),
    "M9 dirty status truncated at 500": (
        "status/selection uses list_dirty_namespaces(limit=500)",
        {"src/menhir/api/routes_handlers.py": ("adapter.list_dirty_namespaces, limit=500",)},
    ),
    "M10 serial N+1 view history": (
        "counter loop awaits one history query per row",
        {"src/menhir/api/routes_handlers.py": ("for row in counters:", "adapter.counter_history,")},
    ),
    "M11 generic backend logs full body": (
        "exception log formats body with repr",
        {"src/menhir/api/routes_handlers.py": ('logger.exception("backend_invoke failed: operation=%s body=%r", operation, body)',)},
    ),
    "M12 auth-exempt readiness leaks raw failures": (
        "ready is exempt and serializes capabilities.failures",
        {
            "src/menhir/api/auth.py": ('"/api/ready"',),
            "src/menhir/api/routes.py": ('@router.get("/ready", response_model=ReadyResponse)', "failures=list(capabilities.failures)"),
        },
    ),
    "M13 blocking SQLite in async auth": (
        "ASGI middleware directly calls synchronous token-store resolve",
        {
            "src/menhir/api/auth.py": ("self._client_token_store.resolve(token_str)",),
            "src/menhir/api/client_token_store.py": ("sqlite3.connect(self.db_path)",),
        },
    ),
    "M14 non-transactional two-store reset": (
        "namespace delete commits before evidence purge",
        {"src/menhir/api/routes_handlers.py": ("backend.delete_namespace(namespace)", "adapter.purge_turn_evidence, namespace")},
    ),
    "M15 readonly provider topology": (
        "get_provider_config falls through explicit readonly remainder",
        {"src/menhir/api/routes_support.py": ('"get_provider_config",', 'return "readonly"')},
    ),
    "L1 unbounded stale limit": (
        "plain integer query parameter is forwarded to adapter",
        {"src/menhir/api/routes.py": ("limit: int = 200", "stale_anchored_memories, project=project, limit=limit")},
    ),
    "L2 client-token lifetime/cap gap": (
        "schema has revocation but no expiry and all active rows are returned",
        {"src/menhir/api/client_token_store.py": ("revoked INTEGER DEFAULT 0", "def all(")},
    ),
    "L3 token-store settings drift": (
        "MCP accessor re-reads MemorySettings.from_env",
        {"src/menhir/api/client_token_store.py": ("def get_client_token_store(", "MemorySettings.from_env()")},
    ),
    "L4 DCR redirect profile gap": (
        "validator checks scheme/host but not fragments or userinfo",
        {"src/menhir/api/oauth_as_register.py": ("urlparse(uri)", "parsed.scheme", "parsed.hostname")},
    ),
    "L5 DCR silently filters unknown scopes": (
        "requested scope list is intersected rather than rejected",
        {"src/menhir/api/oauth_as_register.py": ("granted = [s for s in requested_raw.split() if s in supported]",)},
    ),
    "L6 SameSite Strict one-click incompatibility": (
        "consent session cookie is Strict",
        {"src/menhir/api/oauth_authorize.py": ('samesite="strict"',)},
    ),
    "L7 unknown backend operation becomes 500": (
        "caller-controlled operation raises RuntimeError",
        {"src/menhir/api/routes_handlers.py": ('raise RuntimeError(f"Unknown backend operation: {operation}")',)},
    ),
    "L8 unused OAuth compatibility helpers": (
        "helper definitions exist; occurrence-count check below confirms no production call",
        {"src/menhir/api/oauth_token.py": ("def _access_ttl_s(", "def _signing_kid(")},
    ),
}


def finding_sweeps(root: Path, out: Results) -> None:
    for label, (detail, files) in FINDING_FRAGMENTS.items():
        missing: list[str] = []
        for relative, fragments in files.items():
            source = normalized(text(root, relative))
            for fragment in fragments:
                if normalized(fragment) not in source:
                    missing.append(f"{relative}:{fragment!r}")
        out.add(
            "CONFIRMED" if not missing else "FAIL", label,
            detail if not missing else "source drift: " + ", ".join(missing),
        )

    all_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "src/menhir").rglob("*.py")
    )
    out.add(
        "CONFIRMED" if ".purge_expired(" not in all_source else "NOT FOUND",
        "M8 authorization-code cleanup absent",
        "no production call under src/menhir" if ".purge_expired(" not in all_source else "purge call found",
    )

    register_source = text(root, "src/menhir/api/oauth_as_register.py")
    redirect_gap = all(token not in register_source for token in ("parsed.fragment", "parsed.username", "parsed.password"))
    out.add(
        "CONFIRMED" if redirect_gap else "NOT FOUND", "L4 redirect validator absence check",
        "no fragment/userinfo rejection" if redirect_gap else "explicit rejection found",
    )

    token_source = text(root, "src/menhir/api/oauth_token.py")
    helper_counts = {
        name: token_source.count(f"{name}(")
        for name in ("_access_ttl_s", "_signing_kid")
    }
    unused = all(count == 1 for count in helper_counts.values())
    out.add(
        "CONFIRMED" if unused else "NOT FOUND", "L8 helper occurrence check",
        str(helper_counts),
    )


def dynamic_code_burn(out: Results) -> None:
    try:
        from archolith_oauth.stores import AuthorizationCodeStore
    except Exception as exc:
        out.add("SKIP", "M1 live authorization-code burn", f"pinned dependency unavailable: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix="menhir-m2-probe-") as tmp:
        store = AuthorizationCodeStore(Path(tmp) / "oauth.db", ttl_s=120)
        code = store.issue(
            client_id="good-client",
            redirect_uri="http://127.0.0.1/callback",
            scope="menhir:read",
            code_challenge="A" * 43,
            code_challenge_method="S256",
            resource="http://127.0.0.1:8099",
            subject="probe",
        )
        wrong = store.redeem(
            code=code, client_id="wrong-client",
            redirect_uri="http://127.0.0.1/callback",
        )
        correct = store.redeem(
            code=code, client_id="good-client",
            redirect_uri="http://127.0.0.1/callback",
        )
    out.add(
        "CONFIRMED" if wrong is None and correct is None else "FAIL",
        "M1 live authorization-code burn",
        "wrong-client attempt permanently consumed code before PKCE"
        if wrong is None and correct is None else f"wrong={wrong!r} correct_after={correct!r}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    root = root_dir()
    out = Results()
    print(f"Menhir M2 functional probe — target {AUDITED_COMMIT}")
    print(f"repository root: {root}\n")
    reconcile(root, out)
    verify_target(root, out)
    trees = parse_modules(root, out)
    if trees:
        duplicate_sweep(trees, out)
        pyflakes_and_except_sweep(root, trees, out)
        cancellation_sweep(root, out)
        timestamp_sweep(trees, out)
        constant_sweep(trees, out)
        kwarg_sweep(trees, out)
        finding_sweeps(root, out)
        dynamic_code_burn(out)
    failures = out.render()
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
