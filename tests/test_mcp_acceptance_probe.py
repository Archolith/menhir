from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


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


def test_rejects_denial_with_success_suffix_or_mixed_success_content() -> None:
    denial = (
        "Error: PermissionError: Token tier 'readonly' cannot invoke "
        "`add_memory` (requires 'agent')"
    )
    suffixed = {
        "result": {
            "content": [{"type": "text", "text": denial + "; mutation nevertheless succeeded"}],
            "isError": False,
        }
    }
    mixed = {
        "result": {
            "content": [
                {"type": "text", "text": denial},
                {"type": "text", "text": "memory added successfully"},
            ],
            "isError": False,
        }
    }
    opposite = {
        "result": {
            "content": [{
                "type": "text",
                "text": (
                    "Error: PermissionError: requires 'agent' check was bypassed; "
                    "caller can now invoke `add_memory`"
                ),
            }],
            "isError": False,
        }
    }
    assert not PROBE._is_explicit_mutation_refusal(200, suffixed)
    assert not PROBE._is_explicit_mutation_refusal(200, mixed)
    assert not PROBE._is_explicit_mutation_refusal(200, opposite)


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


def test_production_policy_requires_exact_read_only_probe() -> None:
    policy = {
        "clients": {
            "menhir-deploy-probe": {
                "label": "menhir-deploy-probe",
                "scopes": ["menhir:read"],
                "maximum_tier": "readonly",
                "namespace": "",
                "allowed_tools": ["recall_memories"],
                "denied_tools": ["add_memory"],
            }
        }
    }
    PROBE._require_probe_policy(policy)
    policy["clients"]["menhir-deploy-probe"]["allowed_tools"] = ["add_memory"]
    with pytest.raises(RuntimeError, match="exact read-only"):
        PROBE._require_probe_policy(policy)


def test_minted_probe_token_is_never_written_or_printed() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert '"exp": now + 60' in source
    assert "token.write" not in source
    assert "write_text(token" not in source
    assert "print(token" not in source


def test_production_mode_is_release_owned() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert "production <base-url> <release-json> <policy-json>" in source
    assert "production database changed during acceptance" in source
