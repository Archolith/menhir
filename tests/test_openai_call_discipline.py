"""Code-smell guard: OpenAI calls, request shaping, 429 backoff and work timeouts use one API.

The rule (``menhir.infrastructure.openai_calls``):
- chat and embeddings requests go through ``acreate_chat_completion`` / ``create_chat_completion``
  / ``create_embedding`` / ``ResilientChatClient``, which apply the model profile's shaping
  (reasoning params, the opt-in Flex tier) and throttling-429 backoff;
- the low-level shaping and backoff functions are not called anywhere else;
- a timeout around work that can make OpenAI calls is ``await_with_backoff_aware_timeout``, so
  backoff sleeps don't spend the deadline. ``asyncio.wait_for`` / ``asyncio.timeout`` sites that
  predate the rule are listed below with a reason; a new one fails until it is converted or
  listed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[1] / "src" / "menhir"
INFRA = "infrastructure/"

#: Files that implement the API and may use its building blocks directly.
_API_FILES = {
    INFRA + "openai_calls.py",
    INFRA + "openai_rate_limit.py",
    INFRA + "openai_request_policy.py",
    INFRA + "model_profiles.py",
}

_LOW_LEVEL = {
    "shape_request",
    "apply_openai_request_policy",
    "acall_with_rate_limit_backoff",
    "call_with_rate_limit_backoff",
}
#: Low-level use outside the API, with the reason it is allowed.
_LOW_LEVEL_ALLOWED = {
    # The instrumented transport wraps embeddings generically (create(*args, **kwargs)); chat
    # on it is deliberately unwrapped so the Graphiti proxy's retry is never nested.
    INFRA + "observability.py": "instrumented embeddings endpoint",
}

#: OpenAI SDK endpoint chains that must not be called directly.
_ENDPOINTS = {("chat", "completions", "create"), ("embeddings", "create"), ("responses", "create")}

_TIMEOUT_FUNCS = {"wait_for", "timeout", "timeout_at"}
#: Existing work timeouts, per file: (count, reason). OPENAI marks sites whose awaited work can
#: make OpenAI calls -- candidates for await_with_backoff_aware_timeout, not yet converted.
_TIMEOUTS_ALLOWED: dict[str, tuple[int, str]] = {
    INFRA + "openai_rate_limit.py": (1, "implements await_with_backoff_aware_timeout"),
    "cli/hook.py": (2, "OPENAI: build_context (recall embeddings/reranker)"),
    "mcp/telemetry/tracker.py": (1, "OPENAI: MCP tool runner (recall tools)"),
    "services/enrichment_steps.py": (1, "OPENAI: source-memory embed_query"),
    "services/oracle_executor.py": (1, "OPENAI if an oracle calls a model; per-oracle bound"),
    "services/shadow_context_composition.py": (1, "OPENAI: shadow prediction LLM call"),
    "mcp/tools/ingest/ingest_document.py": (1, "enqueue only (queue_episode)"),
    "services/project_ingest.py": (1, "enqueue only (queue_episode)"),
    "core/runtime.py": (1, "startup resume of pending episodes (enqueue)"),
    INFRA + "graphiti_client.py": (1, "Neo4j index build"),
    "services/ingest_worker.py": (1, "idle queue poll"),
    "services/maintenance_scheduler.py": (3, "stop / lease-lost event waits"),
}


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return out


def _attr_chain(node: ast.expr) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return tuple(reversed(parts))


def direct_endpoint_calls(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(_attr_chain(node.func)[-len(chain) :] == chain for chain in _ENDPOINTS)
    ]


def low_level_uses(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name in _LOW_LEVEL for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in _LOW_LEVEL:
            lines.append(node.lineno)
    return lines


def timeout_calls(tree: ast.Module) -> int:
    asyncio_names = {"asyncio"}
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            asyncio_names |= {a.asname for a in node.names if a.name == "asyncio" and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            bare |= {a.asname or a.name for a in node.names if a.name in _TIMEOUT_FUNCS}
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _TIMEOUT_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id in asyncio_names
        ) or (isinstance(func, ast.Name) and func.id in bare):
            count += 1
    return count


def test_openai_endpoints_are_only_called_through_openai_calls() -> None:
    offenders = {
        rel: lines
        for rel, tree in _modules()
        if rel not in _API_FILES and (lines := direct_endpoint_calls(tree))
    }
    assert not offenders, (
        "Call OpenAI through menhir.infrastructure.openai_calls (shaping + 429 backoff), "
        f"not the SDK endpoint directly: {offenders}"
    )


def test_shaping_and_backoff_are_not_hand_rolled() -> None:
    offenders = {
        rel: lines
        for rel, tree in _modules()
        if rel not in _API_FILES
        and rel not in _LOW_LEVEL_ALLOWED
        and (lines := low_level_uses(tree))
    }
    assert not offenders, (
        "Use openai_calls instead of composing apply_openai_request_policy / "
        f"*call_with_rate_limit_backoff by hand: {offenders}"
    )


def test_work_timeouts_are_backoff_aware_or_listed() -> None:
    found = {rel: n for rel, tree in _modules() if (n := timeout_calls(tree))}
    allowed = {rel: count for rel, (count, _reason) in _TIMEOUTS_ALLOWED.items()}
    assert found == allowed, (
        "New asyncio.wait_for/timeout around work: use await_with_backoff_aware_timeout if it "
        "can make OpenAI calls, else list it in _TIMEOUTS_ALLOWED with a reason. A count that "
        f"dropped should be lowered. found={found} allowed={allowed}"
    )


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("async def f(c):\n    return await c.chat.completions.create(model='m')\n", "endpoint"),
        ("def f(c):\n    return c.embeddings.create(model='m', input=['x'])\n", "endpoint"),
        (
            "from menhir.infrastructure.openai_rate_limit import acall_with_rate_limit_backoff\n",
            "low_level",
        ),
        ("import asyncio\nasync def f(x):\n    await asyncio.wait_for(x, 1)\n", "timeout"),
        ("import asyncio as aio\nasync def f(x):\n    await aio.wait_for(x, 1)\n", "timeout"),
        ("from asyncio import wait_for\nasync def f(x):\n    await wait_for(x, 1)\n", "timeout"),
    ],
)
def test_guard_catches_planted_violations(source: str, rule: str) -> None:
    tree = ast.parse(source)
    if rule == "endpoint":
        assert direct_endpoint_calls(tree)
    elif rule == "low_level":
        assert low_level_uses(tree)
    else:
        assert timeout_calls(tree) == 1


def test_passing_an_endpoint_to_the_api_is_not_a_direct_call() -> None:
    tree = ast.parse("def f(c):\n    return create_chat_completion(c.chat.completions.create, {})\n")
    assert direct_endpoint_calls(tree) == []
