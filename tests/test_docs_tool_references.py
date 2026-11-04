"""Docs must not call MCP tools that don't exist (#442).

Every backticked call such as `recall_memories(...)` in the user-facing and agent docs must name a
tool registered in menhir.mcp.tools.ALL_TOOLS, or a name listed in _NOT_TOOLS. Only call syntax is
checked: bare backticked snake_case names are mostly fields and functions, not tools.
"""

from __future__ import annotations

import re
from pathlib import Path

from menhir.mcp.tools import ALL_TOOLS

_ROOT = Path(__file__).resolve().parent.parent

# docs/research/ is excluded: it quotes other products' tools on purpose.
_EXCLUDED_DIRS = (_ROOT / "docs" / "research",)

# Call-syntax names in the scanned docs that are deliberately not MCP tools.
_NOT_TOOLS = {
    # Python functions named in runbooks and the security posture.
    "auth_mode_from",
    "backend_client_mode_enabled",
    "probe_backend_health",
    "prune_old_revisions",
    "resolve_auth_mode",
    # Illustrations in design docs.
    "label_stale_anchors",
    "sync_prices",
}

_CALL = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(")


def _scanned_files() -> list[Path]:
    files = [_ROOT / "README.md", _ROOT / ".agent" / "endpoints.md", _ROOT / ".agent" / "tasks-mcp.md"]
    for path in sorted((_ROOT / "docs").rglob("*.md")):
        if not any(excluded in path.parents for excluded in _EXCLUDED_DIRS):
            files.append(path)
    return [path for path in files if path.exists()]


def test_backticked_tool_calls_in_docs_resolve_to_registered_tools() -> None:
    registered = {tool_cls.name for tool_cls in ALL_TOOLS}
    unknown: list[str] = []
    for path in _scanned_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name in _CALL.findall(line):
                if name not in registered and name not in _NOT_TOOLS:
                    unknown.append(f"{path.relative_to(_ROOT).as_posix()}:{lineno}: `{name}(`")
    assert not unknown, (
        "Docs call tools that are not registered (rename, remove, or add a non-tool name to "
        "_NOT_TOOLS):\n" + "\n".join(unknown)
    )


def test_not_tools_allowlist_has_no_registered_tools() -> None:
    registered = {tool_cls.name for tool_cls in ALL_TOOLS}
    assert not (_NOT_TOOLS & registered)
