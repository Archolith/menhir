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
    """The helper binds operator and its token round-trips.

    The whole body runs inside a cleared tier because the autouse fixture (CF-34) stands in for the
    transport, and this test is specifically about the UNBOUND baseline the helper exists to
    replace -- so it has to establish that baseline itself rather than inherit one.
    """
    from menhir.core.request_context import bind_request_tier, reset_request_tier

    cleared = bind_request_tier("")
    try:
        assert get_request_tier() == ""  # unbound default
        token = bind_stdio_local_trust()
        try:
            assert get_request_tier() == "operator"
        finally:
            reset_request_tier(token)
        assert get_request_tier() == ""  # restored after reset
    finally:
        reset_request_tier(cleared)
