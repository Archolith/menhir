"""CF-237: every `timeout_for` override must accept what its `endpoint` accepts.

`timeout_for` is dispatched from `contracts.py` with the caller's raw kwargs, so a
`timeout_for` that is narrower than its own `endpoint` raises a raw TypeError outside
`track_mcp_call` whenever a client passes an endpoint-only argument (e.g. `namespace`).
"""

from __future__ import annotations

import inspect

import pytest

from menhir.mcp.tools import ALL_TOOLS
from menhir.mcp.tools.ingest.ingest_document import IngestDocumentTool
from menhir.mcp.tools.ingest.ingest_project import IngestProjectTool


pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_ingest_tools_accept_namespace_in_timeout_for() -> None:
    for tool_cls in (IngestDocumentTool, IngestProjectTool):
        result = tool_cls().timeout_for(namespace="ns")
        assert isinstance(result, int)


@pytest.mark.unit
def test_schema_advertises_namespace_for_ingest_tools() -> None:
    for tool_cls in (IngestDocumentTool, IngestProjectTool):
        params = inspect.signature(tool_cls.endpoint).parameters
        assert "namespace" in params, (
            f"{tool_cls.__name__}.endpoint no longer advertises `namespace`; "
            "the timeout_for parity test would be vacuous"
        )


@pytest.mark.unit
def test_timeout_for_accepts_every_endpoint_parameter_catalog_wide() -> None:
    for tool_cls in ALL_TOOLS:
        timeout_for = tool_cls.__dict__.get("timeout_for")
        if timeout_for is None:
            continue
        tf_params = inspect.signature(timeout_for).parameters
        if any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in tf_params.values()
        ):
            continue
        endpoint_params = inspect.signature(tool_cls.endpoint).parameters
        missing = sorted(
            name
            for name in endpoint_params
            if name not in tf_params and name != "self"
        )
        assert not missing, (
            f"{tool_cls.__name__}.timeout_for is narrower than its endpoint; "
            f"add these parameter(s) to timeout_for: {missing}"
        )
