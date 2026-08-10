"""REST tier enforcement (auth Phase 0 / tracker Q4) + constant-time compares (Q5).

The MCP tool path has enforced per-tool tiers since the tier model landed; these tests pin the
REST mirror: destructive routes/ops require operator, writes require agent, reads pass at
readonly, and an EMPTY tier (auth disabled — no keys configured) skips enforcement, matching
mcp/contracts.py semantics exactly.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.routes import (
    _BACKEND_METHODS,
    _OP_TIER_AGENT,
    _OP_TIER_OPERATOR,
    _require_tier,
    _required_tier_for_operation,
)
from menhir.mcp.service_access import bind_request_tier, reset_request_tier

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _require_tier — contextvar-driven guard
# ---------------------------------------------------------------------------

def _with_tier(tier: str, required: str):
    token = bind_request_tier(tier)
    try:
        _require_tier(required)
    finally:
        reset_request_tier(token)


def test_readonly_tier_refused_on_operator_endpoint():
    with pytest.raises(HTTPException) as exc:
        _with_tier("readonly", "operator")
    assert exc.value.status_code == 403


def test_agent_tier_refused_on_operator_endpoint():
    with pytest.raises(HTTPException) as exc:
        _with_tier("agent", "operator")
    assert exc.value.status_code == 403


def test_readonly_tier_refused_on_agent_endpoint():
    with pytest.raises(HTTPException) as exc:
        _with_tier("readonly", "agent")
    assert exc.value.status_code == 403


def test_operator_tier_passes_everywhere():
    for required in ("readonly", "agent", "operator"):
        _with_tier("operator", required)  # no raise


def test_matching_tiers_pass():
    _with_tier("readonly", "readonly")
    _with_tier("agent", "agent")


def test_empty_tier_skips_enforcement():
    # No keys configured -> middleware never binds a tier -> enforcement is skipped
    # (auth-disabled local mode), mirroring mcp/contracts.py `if tier and ...`.
    _with_tier("", "operator")  # no raise


def test_unknown_tier_is_refused():
    with pytest.raises(HTTPException):
        _with_tier("bogus", "readonly")


# ---------------------------------------------------------------------------
# Internal dispatch op-tier policy — total by construction
# ---------------------------------------------------------------------------

def test_op_tier_sets_are_subsets_of_backend_methods():
    assert _OP_TIER_OPERATOR <= _BACKEND_METHODS
    assert _OP_TIER_AGENT <= _BACKEND_METHODS
    assert not (_OP_TIER_OPERATOR & _OP_TIER_AGENT)


def test_destructive_ops_require_operator():
    for op in ("delete_memory", "delete_namespace", "recover_orphans",
               "resolve_conflict_group", "scheduler_force_takeover"):
        assert _required_tier_for_operation(op) == "operator", op


def test_write_ops_require_agent():
    for op in ("queue_episode", "create_todo", "flag_memory", "create_candidate"):
        assert _required_tier_for_operation(op) == "agent", op


def test_read_ops_are_readonly():
    for op in ("recall", "build_context", "fetch_memory_overview", "list_todos",
               "scheduler_status_snapshot", "get_queue_depth"):
        assert _required_tier_for_operation(op) == "readonly", op


def test_every_backend_method_has_a_deliberate_tier():
    # The remainder rule makes the policy total, but every REMAINDER op must genuinely be a
    # read: enforce the naming convention so a future mutating op named outside it cannot
    # silently land in the readonly bucket (implicit policy is forbidden).
    read_prefixes = ("recall", "build_context", "view_entropy", "fetch_", "list_", "get_",
                     "query_", "scheduler_status", "circuit_breaker", "embedding_cache")
    remainder = _BACKEND_METHODS - _OP_TIER_OPERATOR - _OP_TIER_AGENT
    for op in sorted(remainder):
        assert op.startswith(read_prefixes), (
            f"backend op {op!r} is unclassified and does not look read-only — "
            "add it to _OP_TIER_OPERATOR or _OP_TIER_AGENT (or the read prefixes)"
        )


# ---------------------------------------------------------------------------
# Q5 — constant-time token resolution still resolves correctly
# ---------------------------------------------------------------------------

def test_resolve_tier_matches_each_key():
    mw = BearerAuthMiddleware(lambda *a: None, operator_key="op-k", agent_key="ag-k",
                              readonly_key="ro-k")
    assert mw._resolve_tier("op-k") == "operator"
    assert mw._resolve_tier("ag-k") == "agent"
    assert mw._resolve_tier("ro-k") == "readonly"
    assert mw._resolve_tier("wrong") is None
    assert mw._resolve_tier("") is None


def test_resolve_tier_empty_keys_never_match():
    mw = BearerAuthMiddleware(lambda *a: None, api_key="only-op")
    assert mw._resolve_tier("only-op") == "operator"
    assert mw._resolve_tier("") is None  # empty agent/readonly keys must not match empty token
