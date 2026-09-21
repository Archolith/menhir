"""E2E-1 — cold install and MCP protocol.

Plan acceptance (Phase C, E2E-1):
  - clean Python 3.12+ environment;
  - install from the candidate package/build artifact, not an editable checkout;
  - provision/start the documented local graph/backend prerequisites;
  - launch the stdio MCP command exactly as user documentation specifies;
  - ``initialize`` succeeds;
  - ``tools/list`` exposes the intended MVP tools;
  - each required tool schema parses in a stock MCP client;
  - graceful shutdown leaves no corrupt state.

This lane is implemented first because it is what proves the harness itself is honest:
the venv, the wheel and the non-editable import check all live in fixtures every other
lane depends on. If this lane is green for the wrong reason, all eight are.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]


#: The agent-facing surface the MVP contract promises, pinned here rather than read back
#: from the server -- a test that asks the server what it exposes and then asserts the
#: server exposes it proves nothing.
#:
#: Sourced from ``mcp/server.py``'s ``always_visible`` list plus the gateway's own two
#: discovery tools. A name leaving this set is a contract change that belongs in the same
#: commit as the documentation change.
REQUIRED_TOOLS = frozenset(
    {
        "recall_memories",
        "add_memory",
        "query_structure",
        "build_context",
        "read_flagged_memories",
        "recall_context_memories",
        "list_todos",
        "add_todo",
        "search_tools",
        "call_tool",
    }
)


async def test_e2e_01_cold_install_and_protocol(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(**e2e_installed.as_evidence())

    # "install from the candidate package, not an editable checkout" is proven in
    # install_into_venv by _assert_not_running_from_checkout, which raises rather than
    # returning a value -- so reaching this line IS the evidence.
    lane_evidence.record(
        "non_editable_install",
        passed=True,
        detail={"wheel": e2e_installed.wheel_path.name, "sha256": e2e_installed.wheel_sha256},
    )
    lane_evidence.record(
        "backend_prerequisites_started",
        passed=running_stack.is_alive(),
        detail={"url": running_stack.url, "features": feature_combo.label},
    )

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=feature_env
    ) as client:
        # initialize() already ran inside stdio_session; reaching here means the
        # handshake completed against the documented `python -m menhir.mcp.server`.
        lane_evidence.record("initialize", passed=True)

        listing = await client.list_tools()
        exposed = {tool.name for tool in listing.tools}
        missing = REQUIRED_TOOLS - exposed
        lane_evidence.record(
            "tools_list",
            passed=not missing,
            detail={"missing": sorted(missing), "exposed_count": len(exposed)},
        )
        lane_evidence.attach("tools_list.json", json.dumps(sorted(exposed), indent=2))
        assert not missing, (
            f"MVP tools absent from tools/list under features={feature_combo.label}: "
            f"{sorted(missing)}"
        )

        # "each required tool schema parses in a stock MCP client": the SDK has already
        # validated the envelope by returning typed objects, so what remains is whether
        # each schema is a usable JSON Schema object rather than None or a bare string.
        unusable: list[str] = []
        for tool in listing.tools:
            if tool.name not in REQUIRED_TOOLS:
                continue
            schema = tool.inputSchema
            if not isinstance(schema, dict) or schema.get("type") != "object":
                unusable.append(tool.name)
                continue
            try:
                json.dumps(schema)
            except (TypeError, ValueError):
                unusable.append(tool.name)
        lane_evidence.record("tool_schemas_parse", passed=not unusable, detail={"unusable": unusable})
        assert not unusable, f"tool schemas unusable by a stock client: {unusable}"

    # Leaving the context manager closed the stdio bridge. The backend must still be
    # healthy: a bridge exit that takes the runtime owner down with it would corrupt
    # every subsequent lane, and is exactly the "graceful shutdown" failure this checks.
    lane_evidence.record("graceful_shutdown_backend_survives", passed=running_stack.is_alive())
    assert running_stack.is_alive(), "backend died when the stdio bridge disconnected"

    lane_evidence.close(status="PASS")
