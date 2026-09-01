from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "deploy" / "lib" / "mcp_acceptance_probe.py"
SPEC = importlib.util.spec_from_file_location("mcp_acceptance_probe", MODULE)
PROBE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PROBE)


def test_accepts_exact_fastmcp_permission_refusal() -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {
            "content": [{
                "type": "text",
                "text": (
                    "Error: PermissionError: Token tier 'readonly' cannot invoke "
                    "`add_memory` (requires 'agent')"
                ),
            }],
            "isError": False,
        },
    }
    assert PROBE._is_explicit_mutation_refusal(200, response)


def test_rejects_success_and_lookalike_error_text() -> None:
    success = {"result": {"content": [{"type": "text", "text": "memory added"}]}}
    lookalike = {
        "result": {
            "content": [{
                "type": "text",
                "text": "Error: something mentioned add_memory but was not a permission denial",
            }],
            "isError": False,
        }
    }
    assert not PROBE._is_explicit_mutation_refusal(200, success)
    assert not PROBE._is_explicit_mutation_refusal(200, lookalike)


def test_accepts_existing_structured_refusals() -> None:
    assert PROBE._is_explicit_mutation_refusal(
        503, {"error": "temporarily_unavailable"}
    )
    assert PROBE._is_explicit_mutation_refusal(
        200, {"result": {"isError": True}}
    )
    assert PROBE._is_explicit_mutation_refusal(
        200, {"error": {"code": -32603, "message": "permission denied"}}
    )
