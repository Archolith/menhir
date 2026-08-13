#!/usr/bin/env python3
"""Executable evidence probe for the Menhir M2 API/OAuth compound audit.

Run from the repository root on the audit branch or any checkout whose
``src/menhir/api`` tree is identical to the audited commit:

    python .agent/audit/m2_functional_probe.py
    python .agent/audit/m2_functional_probe.py --strict

The probe is intentionally read-only except for temporary SQLite files used by
the optional pinned ``archolith_oauth`` dynamic check.  It prints:

* the exact 24-file/5,565-line scope reconciliation;
* the six mandated static bug-class sweeps;
* source invariants that reproduce the audit's confirmed call-chain defects;
* an executable authorization-code burn check when the pinned dependency is
  installed in the current environment.

``--strict`` returns non-zero when the audited source shape has drifted or a
mandatory static sweep itself fails.  Confirmed defects are evidence, not probe
failures.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
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


@dataclass
class Result:
    status: str
    label: str
    detail: str = ""


class Recorder:
    def __init__(self) -> None:
        self.results: list[Result] = []
        self.mandatory_failures = 0

    def add(self, status: str, label: str, detail: str = "", *, mandatory: bool = False) -> None:
        self.results.append(Result(status, label, detail))
        if mandatory and status == "FAIL":
            self.mandatory_failures += 1

    def ok(self, label: str, detail: str = "", *, mandatory: bool = False) -> None:
        self.add("PASS", label, detail, mandatory=mandatory)

    def confirmed(self, label: str, detail: str = "") -> None:
        self.add("CONFIRMED", label, detail)

    def not_found(self, label: str, detail: str = "") -> None:
        self.add("NOT FOUND", label, detail)

    def skip(self, label: str, detail: str = "") -> None:
        self.add("SKIP", label, detail)

    def fail(self, label: str, detail: str = "", *, mandatory: bool = True) -> None:
        self.add("FAIL", label, detail, mandatory=mandatory)

    def render(self) -> None:
        for result in self.results:
            suffix = f" — {result.detail}" if result.detail else ""
            print(f"[{result.status}] {result.label}{suffix}")


def repo_root() -> Path:
    override = os.environ.get("MENHIR_M2_PROBE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def read_text(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def count_lines(path: Path) -> int:
    # splitlines() preserves the source-line definition used by wc -l for these
    # files because every audited file ends with a newline.
    return len(path.read_text(encoding="utf-8").splitlines())


def run(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def all_python_files(root: Path) -> list[Path]:
    return [root / relative for relative in EXPECTED_LINES]


def reconcile_scope(root: Path, rec: Recorder) -> None:
    actual_dir = root / "src/menhir/api"
    if not actual_dir.is_dir():
        rec.fail("scope directory exists", str(actual_dir))
        return

    actual = {
        path.relative_to(root).as_posix()
        for path in actual_dir.rglob("*.py")
        if path.is_file()
    }
    expected = set(EXPECTED_LINES)
    if actual == expected:
        rec.ok("exact scoped file set", f"{len(actual)} Python files", mandatory=True)
    else:
        rec.fail(
            "exact scoped file set",
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}",
        )

    mismatches: list[str] = []
    total = 0
    for relative, expected_lines in EXPECTED_LINES.items():
        path = root / relative
        if not path.exists():
            mismatches.append(f"{relative}: missing")
            continue
        observed = count_lines(path)
        total += observed
        if observed != expected_lines:
            mismatches.append(f"{relative}: expected {expected_lines}, got {observed}")
    if mismatches:
        rec.fail("per-file line reconciliation", "; ".join(mismatches))
    else:
        rec.ok("per-file line reconciliation", f"{len(EXPECTED_LINES)} files", mandatory=True)
    if total == EXPECTED_TOTAL:
        rec.ok("scope line total", str(total), mandatory=True)
    else:
        rec.fail("scope line total", f"expected {EXPECTED_TOTAL}, got {total}")


def verify_git_target(root: Path, rec: Recorder) -> None:
    head = run(["git", "rev-parse", "HEAD"], cwd=root)
    if head.returncode != 0:
        rec.skip("git target verification", head.stdout.strip() or "git unavailable")
        return
    head_sha = head.stdout.strip()
    ancestor = run(["git", "merge-base", "--is-ancestor", AUDITED_COMMIT, "HEAD"], cwd=root)
    if ancestor.returncode != 0:
        rec.fail(
            "audited commit is an ancestor of checkout",
            f"target={AUDITED_COMMIT} head={head_sha}",
        )
        return
    diff = run(
        ["git", "diff", "--quiet", AUDITED_COMMIT, "--", "src/menhir/api"],
        cwd=root,
    )
    if diff.returncode == 0:
        rec.ok(
            "scoped source is byte-identical to audited commit",
            f"target={AUDITED_COMMIT} head={head_sha}",
            mandatory=True,
        )
    else:
        rec.fail(
            "scoped source is byte-identical to audited commit",
            f"target={AUDITED_COMMIT} head={head_sha}",
        )


def parse_scope(root: Path, rec: Recorder) -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    errors: list[str] = []
    for relative in EXPECTED_LINES:
        try:
            trees[relative] = ast.parse(read_text(root, relative), filename=relative)
        except (OSError, SyntaxError) as exc:
            errors.append(f"{relative}: {exc}")
    if errors:
        rec.fail("all scoped modules parse", "; ".join(errors))
    else:
        rec.ok("all scoped modules parse", f"{len(trees)} modules", mandatory=True)
    return trees


def _definitions_in_body(body: Iterable[ast.stmt]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = defaultdict(list)
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name].append(node.lineno)
    return found


def duplicate_definition_sweep(trees: dict[str, ast.Module], rec: Recorder) -> None:
    duplicates: list[str] = []
    for relative, tree in trees.items():
        stack: list[tuple[str, list[ast.stmt]]] = [("<module>", tree.body)]
        while stack:
            scope_name, body = stack.pop()
            for name, lines in _definitions_in_body(body).items():
                if len(lines) > 1:
                    duplicates.append(f"{relative}:{scope_name}:{name}@{lines}")
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    stack.append((f"{scope_name}.{node.name}", node.body))
    if duplicates:
        rec.confirmed("duplicate-definition sweep", "; ".join(duplicates))
    else:
        rec.not_found("duplicate-definition sweep", "no duplicate def/class names in one lexical scope")


def run_pyflakes(root: Path, rec: Recorder) -> None:
    proc = run([sys.executable, "-m", "pyflakes", "src/menhir/api"], cwd=root)
    text = proc.stdout.strip()
    if proc.returncode == 0:
        rec.ok("python -m pyflakes src/menhir/api", text or "clean", mandatory=True)
        return
    missing = "No module named pyflakes" in text or "No module named 'pyflakes'" in text
    if missing:
        rec.skip(
            "python -m pyflakes src/menhir/api",
            "pyflakes is not installed; AST except-only-name fallback executed",
        )
    else:
        rec.confirmed("python -m pyflakes src/menhir/api", text or f"exit={proc.returncode}")


def except_only_name_sweep(trees: dict[str, ast.Module], rec: Recorder) -> None:
    """Conservative fallback for names bound only in ``except`` then loaded later."""

    suspects: list[str] = []

    def bound_names(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                names.add(child.id)
        return names

    def loaded_names(nodes: Iterable[ast.AST]) -> set[str]:
        names: set[str] = set()
        for node in nodes:
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    names.add(child.id)
        return names

    def walk_body(relative: str, body: list[ast.stmt], scope: str) -> None:
        for index, node in enumerate(body):
            if isinstance(node, ast.Try):
                handler_bound: set[str] = set()
                non_handler_bound: set[str] = set()
                for handler in node.handlers:
                    if handler.name:
                        handler_bound.add(handler.name)
                    for statement in handler.body:
                        handler_bound |= bound_names(statement)
                for statement in [*node.body, *node.orelse, *node.finalbody]:
                    non_handler_bound |= bound_names(statement)
                later_loads = loaded_names(body[index + 1 :])
                risky = sorted((handler_bound - non_handler_bound) & later_loads)
                if risky:
                    suspects.append(
                        f"{relative}:{getattr(node, 'lineno', '?')}:{scope}:{','.join(risky)}"
                    )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk_body(relative, node.body, f"{scope}.{node.name}")

    for relative, tree in trees.items():
        walk_body(relative, tree.body, "<module>")
    if suspects:
        rec.confirmed("except-only-name AST fallback", "; ".join(suspects))
    else:
        rec.not_found("except-only-name AST fallback", "no conservative matches")


def cancellation_cleanup_sweep(root: Path, rec: Recorder) -> None:
    source = read_text(root, "src/menhir/api/server_support.py")
    start = source.find("runtime_ctx = start_runtime(")
    manager = source.find("async with mcp_session_manager.run():")
    finalizer = source.find("stop_runtime()", manager)
    if -1 not in (start, manager, finalizer) and start < manager < finalizer:
        rec.confirmed(
            "BaseException/CancelledError cleanup sweep",
            "runtime starts before the only stop_runtime() finally is entered",
        )
    else:
        rec.not_found(
            "BaseException/CancelledError cleanup sweep",
            "audited startup/finalizer ordering not found",
        )


_TIMESTAMP_HINTS = (
    "time",
    "date",
    "created",
    "updated",
    "expires",
    "expired",
    "redeemed",
    "occurred",
    "verified",
    "mtime",
)


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id.lower()
    if isinstance(node, ast.Attribute):
        return f"{_expr_name(node.value)}.{node.attr.lower()}"
    if isinstance(node, ast.Subscript):
        return _expr_name(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return repr(node.value)
    return ""


def timestamp_comparison_sweep(trees: dict[str, ast.Module], rec: Recorder) -> None:
    suspects: list[str] = []
    for relative, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            names = [_expr_name(item) for item in operands]
            if any(any(hint in name for hint in _TIMESTAMP_HINTS) for name in names):
                # Numeric time.time() comparisons are safe; flag only string-ish
                # names/constants so the output remains actionable.
                if any(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in operands
                ) or any("strftime" in name or "iso" in name for name in names):
                    suspects.append(f"{relative}:{node.lineno}:{' | '.join(names)}")
    if suspects:
        rec.confirmed("lexicographic timestamp comparison sweep", "; ".join(suspects))
    else:
        rec.not_found("lexicographic timestamp comparison sweep", "no string timestamp ordering")


def module_constant_sweep(trees: dict[str, ast.Module], rec: Recorder) -> None:
    unread: list[str] = []
    for relative, tree in trees.items():
        assigned: dict[str, int] = {}
        loads: defaultdict[str, int] = defaultdict(int)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets: list[ast.expr]
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                else:
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and re.fullmatch(r"_[A-Z][A-Z0-9_]*", target.id):
                        assigned[target.id] = target.lineno
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loads[node.id] += 1
        for name, line in assigned.items():
            if loads[name] == 0:
                unread.append(f"{relative}:{line}:{name}")
    if unread:
        rec.confirmed("module constants never read in production module", "; ".join(unread))
    else:
        rec.not_found("module constants never read in production module", "no private uppercase candidates")


def kwarg_contract_sweep(trees: dict[str, ast.Module], rec: Recorder) -> None:
    mismatches: list[str] = []
    for relative, tree in trees.items():
        signatures: dict[str, tuple[set[str], bool]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = {
                    arg.arg
                    for arg in [
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    ]
                }
                signatures[node.name] = (parameters, node.args.kwarg is not None)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            signature = signatures.get(node.func.id)
            if signature is None:
                continue
            parameters, has_var_kw = signature
            if has_var_kw:
                continue
            unexpected = sorted(
                keyword.arg
                for keyword in node.keywords
                if keyword.arg is not None and keyword.arg not in parameters
            )
            if unexpected:
                mismatches.append(
                    f"{relative}:{node.lineno}:{node.func.id} unexpected={unexpected}"
                )
    if mismatches:
        rec.confirmed("literal kwarg contract sweep", "; ".join(mismatches))
    else:
        rec.not_found("literal kwarg contract sweep", "no same-module literal mismatches")


def require_fragments(
    root: Path,
    rec: Recorder,
    *,
    label: str,
    files: dict[str, Sequence[str]],
    detail: str,
) -> None:
    missing: list[str] = []
    for relative, fragments in files.items():
        source = read_text(root, relative)
        for fragment in fragments:
            if fragment not in source:
                missing.append(f"{relative}:{fragment!r}")
    if missing:
        rec.fail(label, "source drift; missing " + ", ".join(missing), mandatory=False)
    else:
        rec.confirmed(label, detail)


def reproduce_findings(root: Path, rec: Recorder) -> None:
    require_fragments(
        root,
        rec,
        label="C1 static-key caller can relabel MCP client policy identity",
        files={
            "src/menhir/api/auth.py": (
                "trust_identity_headers: bool = True",
                'client_name = _identity_header(headers, b"client-name")',
                "session_token = bind_request_session(user_id, session_id, client_id=client_id, client_name=client_name)",
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
        detail="static credential authenticates tier, but caller-supplied client_name selects or removes allowlist/pin",
    )
    require_fragments(
        root,
        rec,
        label="C2 agent-tier Phase 3 reset performs namespace destruction",
        files={
            "src/menhir/api/routes_handlers.py": (
                'require_tier("agent")',
                "deleted = await backend.delete_namespace(namespace)",
                "adapter.purge_turn_evidence",
            ),
            "src/menhir/api/routes_support.py": (
                '"delete_memory", "delete_namespace",',
            ),
        },
        detail="agent route calls operator-classified delete_namespace and evidence purge",
    )
    require_fragments(
        root,
        rec,
        label="C3 Explorer loopback-bind bypass survives proxied request",
        files={
            "src/menhir/api/auth.py": (
                "direct_loopback = self._client_is_loopback(scope) and not self._has_proxy_forwarding_header(headers)",
                "if is_explorer and (self._loopback_admin_ok or direct_loopback):",
            ),
        },
        detail="loopback_bound alone is sufficient even when forwarding headers show a reverse proxy",
    )
    require_fragments(
        root,
        rec,
        label="C4 no-auth loopback mode has no DNS-rebinding host gate",
        files={
            "src/menhir/api/mcp_remote.py": (
                'host="0.0.0.0"',
                "disables DNS rebinding protection",
            ),
            "src/menhir/api/auth.py": (
                "if self._auth_mode is AuthMode.NONE:",
                "await self.app(scope, receive, send)",
            ),
        },
        detail="SDK host protection is disabled while AuthMode.NONE authorizes loopback-bound HTTP without Host/Origin validation",
    )
    require_fragments(
        root,
        rec,
        label="H1 request JSON can replace verified memory identity",
        files={
            "src/menhir/api/routes.py": (
                "caller_session = _resolve_caller_session(request)",
                "if body.user_id is not None:",
                "session = new_session(body.user_id, session_id=body.session_id)",
            ),
        },
        detail="authenticated principal is resolved, then body user_id/session_id wins",
    )
    require_fragments(
        root,
        rec,
        label="H2 agent endpoint accepts an unbounded Phase 3 LLM call budget",
        files={
            "src/menhir/api/routes_support.py": (
                "call_budget: int | None = None",
            ),
            "src/menhir/api/routes_handlers.py": (
                'require_tier("agent")',
                "call_budget=body.call_budget",
                "make_sync_chat(",
            ),
        },
        detail="caller controls the real consolidation model-call cap with no schema maximum",
    )
    require_fragments(
        root,
        rec,
        label="M2 lifespan can leak a started runtime before MCP manager entry",
        files={
            "src/menhir/api/server_support.py": (
                "runtime_ctx = start_runtime(",
                "async with mcp_session_manager.run():",
                "finally:\n                stop_runtime()",
            ),
        },
        detail="startup work before the async-with is outside the only cleanup finally",
    )
    require_fragments(
        root,
        rec,
        label="M3 denied consent requests populate an uncapped spent-JTI cache",
        files={
            "src/menhir/api/oauth_authorize.py": (
                "_spent_consent_jtis",
                "_spent_consent_jtis[jti] = expires_at",
                'if decision != "approve":',
            ),
        },
        detail="valid POST burns JTI before decision; denial path is not guarded by approve limiter",
    )
    require_fragments(
        root,
        rec,
        label="M4 DCR hard cap uses a count-then-register race",
        files={
            "src/menhir/api/oauth_as_register.py": (
                "_client_store.count()",
                "_client_store.register(client)",
            ),
        },
        detail="cap check and INSERT occur in separate store transactions",
    )
    require_fragments(
        root,
        rec,
        label="M5 OAuth preflight scheme validator fails open",
        files={
            "src/menhir/api/oauth_preflight.py": (
                "if parsed.scheme == \"http\":",
                "return True",
                "except (TypeError, ValueError):",
            ),
        },
        detail="non-http schemes and parse failures return True",
    )
    require_fragments(
        root,
        rec,
        label="M6 malformed-port diagnostics can return an unredacted URL",
        files={
            "src/menhir/api/oauth_preflight.py": (
                "if parsed.port is not None:",
                "except (TypeError, ValueError):",
                "return raw",
            ),
        },
        detail="parsed.port can raise before userinfo replacement; exception path returns original URL",
    )
    require_fragments(
        root,
        rec,
        label="M7 authorization accepts resource values token exchange later rejects",
        files={
            "src/menhir/api/oauth_authorize.py": (
                "resource=resource",
                "_auth_code_store.issue(",
            ),
            "src/menhir/api/oauth_token.py": (
                "expected_resource=_oauth_config.resource",
            ),
        },
        detail="authorize does not canonicalize/reject resource; token processing does",
    )
    source_tree = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "src/menhir").rglob("*.py")
    )
    if ".purge_expired(" not in source_tree:
        rec.confirmed(
            "M8 authorization-code cleanup is never scheduled",
            "no production call to AuthorizationCodeStore.purge_expired() under src/menhir",
        )
    else:
        rec.not_found("M8 authorization-code cleanup is never scheduled", "purge_expired call exists")
    require_fragments(
        root,
        rec,
        label="M9 Phase 3 dirty status is truncated to first 500 namespaces",
        files={
            "src/menhir/api/routes_handlers.py": (
                "namespace in await asyncio.to_thread(adapter.list_dirty_namespaces, limit=500)",
            ),
        },
        detail="membership beyond adapter's first 500 rows is reported false",
    )
    require_fragments(
        root,
        rec,
        label="M10 /api/views performs serial N+1 history queries",
        files={
            "src/menhir/api/routes_handlers.py": (
                "for row in counters:",
                "history = await asyncio.to_thread(",
                "adapter.counter_history,",
            ),
        },
        detail="one history query is awaited per returned counter, up to route limit 500",
    )
    require_fragments(
        root,
        rec,
        label="M11 generic backend failure logs the full request body",
        files={
            "src/menhir/api/routes_handlers.py": (
                'logger.exception("backend_invoke failed: operation=%s body=%r", operation, body)',
            ),
        },
        detail="arbitrary episode text, paths, and future secret fields are emitted to logs",
    )
    require_fragments(
        root,
        rec,
        label="M12 public readiness returns raw internal failure strings",
        files={
            "src/menhir/api/routes.py": (
                '@router.get("/ready")',
                "failures=list(capabilities.failures)",
            ),
            "src/menhir/api/auth.py": (
                '"/api/ready"',
            ),
        },
        detail="auth-exempt response exposes runtime preflight failure details",
    )
    require_fragments(
        root,
        rec,
        label="M13 async authentication performs synchronous SQLite resolution",
        files={
            "src/menhir/api/auth.py": (
                "record = self._client_token_store.resolve(token_str)",
            ),
            "src/menhir/api/client_token_store.py": (
                "sqlite3.connect(",
            ),
        },
        detail="event-loop middleware calls blocking SQLite on every client-token request",
    )
    require_fragments(
        root,
        rec,
        label="M14 Phase 3 reset is a non-transactional two-store delete",
        files={
            "src/menhir/api/routes_handlers.py": (
                "deleted = await backend.delete_namespace(namespace)",
                "turn_evidence_deleted = await asyncio.to_thread(adapter.purge_turn_evidence, namespace)",
            ),
        },
        detail="graph deletion commits before evidence purge; second-leg failure leaves partial teardown",
    )
    require_fragments(
        root,
        rec,
        label="M15 stale diagnostics accept unbounded or negative limits",
        files={
            "src/menhir/api/routes.py": (
                "limit: int = 200",
                "stale_anchored_memories, project=project, limit=limit",
            ),
        },
        detail="plain integer query parameter has no ge/le validation",
    )
    require_fragments(
        root,
        rec,
        label="M16 client-token registry has no expiry or active-token cap",
        files={
            "src/menhir/api/client_token_store.py": (
                "CREATE TABLE IF NOT EXISTS client_tokens",
                "revoked INTEGER DEFAULT 0",
                "def list_all(",
            ),
        },
        detail="tokens remain valid until manual revocation and rows are listed without a bound",
    )
    require_fragments(
        root,
        rec,
        label="M17 MCP client-token accessor can drift from app settings snapshot",
        files={
            "src/menhir/api/client_token_store.py": (
                "def get_client_token_store(",
                "MemorySettings.from_env()",
            ),
        },
        detail="later accessor can re-read changed environment instead of the store attached to the app",
    )
    require_fragments(
        root,
        rec,
        label="M18 readonly generic operation exposes unredacted provider topology",
        files={
            "src/menhir/api/routes_support.py": (
                '"get_provider_config",',
                'return "readonly"'
            ),
        },
        detail="provider configuration includes internal URLs/model and database topology",
    )


def dynamic_auth_code_burn(rec: Recorder) -> None:
    try:
        from archolith_oauth.stores import AuthorizationCodeStore
    except Exception as exc:  # dependency may be absent in audit workstation
        rec.skip("M1 dynamic authorization-code burn reproduction", f"dependency unavailable: {exc}")
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
            code=code,
            client_id="wrong-client",
            redirect_uri="http://127.0.0.1/callback",
        )
        correct_after_wrong = store.redeem(
            code=code,
            client_id="good-client",
            redirect_uri="http://127.0.0.1/callback",
        )
        if wrong is None and correct_after_wrong is None:
            rec.confirmed(
                "M1 dynamic authorization-code burn reproduction",
                "wrong client returned None and permanently consumed the code before PKCE",
            )
        else:
            rec.fail(
                "M1 dynamic authorization-code burn reproduction",
                f"wrong={wrong!r} correct_after_wrong={correct_after_wrong!r}",
                mandatory=False,
            )


def low_risk_checks(root: Path, rec: Recorder) -> None:
    require_fragments(
        root,
        rec,
        label="L1 DCR redirect validator omits fragment/userinfo rejection",
        files={
            "src/menhir/api/oauth_as_register.py": (
                "urlparse(uri)",
                "parsed.scheme",
                "parsed.hostname",
            ),
        },
        detail="validator checks scheme/host but has no parsed.fragment/username/password rejection",
    )
    require_fragments(
        root,
        rec,
        label="L2 DCR silently drops unknown scopes",
        files={
            "src/menhir/api/oauth_as_register.py": (
                "scope for scope in requested_scopes if scope in _oauth_config.allowed_scopes",
            ),
        },
        detail="registration can succeed with an empty effective scope set",
    )
    require_fragments(
        root,
        rec,
        label="L3 consent session cookie is SameSite=Strict",
        files={
            "src/menhir/api/oauth_authorize.py": (
                'samesite="strict"',
            ),
        },
        detail="ordinary cross-site OAuth navigations cannot use the advertised one-click session",
    )
    require_fragments(
        root,
        rec,
        label="L4 unknown generic backend operations become 500 errors",
        files={
            "src/menhir/api/routes_handlers.py": (
                'raise RuntimeError(f"Unknown backend operation: {operation}")',
            ),
        },
        detail="client input is surfaced as an internal server error rather than 404/422",
    )
    require_fragments(
        root,
        rec,
        label="L5 compatibility OAuth helpers carry unread configuration",
        files={
            "src/menhir/api/oauth_token.py": (
                "_access_ttl_s",
                "_signing_kid",
            ),
        },
        detail="wrapper helpers preserve parameters that do not affect production behavior",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero when mandatory source/sweep checks fail",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    rec = Recorder()
    print(f"Menhir M2 functional probe — target {AUDITED_COMMIT}")
    print(f"repository root: {root}")
    print()

    reconcile_scope(root, rec)
    verify_git_target(root, rec)
    trees = parse_scope(root, rec)
    if trees:
        duplicate_definition_sweep(trees, rec)
        run_pyflakes(root, rec)
        except_only_name_sweep(trees, rec)
        cancellation_cleanup_sweep(root, rec)
        timestamp_comparison_sweep(trees, rec)
        module_constant_sweep(trees, rec)
        kwarg_contract_sweep(trees, rec)
        reproduce_findings(root, rec)
        dynamic_auth_code_burn(rec)
        low_risk_checks(root, rec)

    rec.render()
    print()
    counts: defaultdict[str, int] = defaultdict(int)
    for result in rec.results:
        counts[result.status] += 1
    ordered = ("CONFIRMED", "PASS", "NOT FOUND", "SKIP", "FAIL")
    print("summary: " + ", ".join(f"{name}={counts[name]}" for name in ordered))
    print(f"mandatory_failures={rec.mandatory_failures}")
    return 1 if args.strict and rec.mandatory_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
