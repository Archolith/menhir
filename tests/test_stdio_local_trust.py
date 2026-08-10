"""CT-002: the stdio MCP process binds operator tier explicitly.

The stdio server runs in-process as a trusted local agent whose real boundary is
filesystem access to the SQLite stores. Binding operator tier makes that trust
decision visible instead of relying on the implicit empty-tier bypass.
"""

from __future__ import annotations

from menhir.mcp.service_access import (
    bind_stdio_local_trust,
    get_request_tier,
    reset_request_tier,
)


def test_bind_stdio_local_trust_binds_operator():
    assert get_request_tier() == ""  # unbound default
    token = bind_stdio_local_trust()
    try:
        assert get_request_tier() == "operator"
    finally:
        reset_request_tier(token)
    assert get_request_tier() == ""  # restored after reset
