"""MCP tool: get_beacon_evidence -- Menhir as a Beacon memory provider.

Beacon owns all beacon work; a memory provider only answers what it has indexed. This tool is
Menhir's side of that contract: it returns a ``beacon-memory-evidence-1.1`` document for one
indexed project, read from the graph only (no filesystem path, no git, no writes).
"""

from __future__ import annotations

import json

from menhir.mcp.contracts import ToolScope
from menhir.mcp.tools.base import BaseTextTool


async def get_beacon_evidence(project_id: str) -> str:
    """Return Beacon memory evidence (``beacon-memory-evidence-1.1``) for one indexed project.

    The evidence is bound to the repository and git commit the project was indexed from, so
    Beacon can refuse to publish it against a different checkout. Read-only.

    Args:
        project_id: The project's stable Menhir id (``structure_project_id``).

    Returns:
        The evidence document as JSON text. A project that cannot be served (unknown id,
        partial or in-progress index, indexed from a dirty checkout or without git) is an
        error naming the reason.
    """

    return await GetBeaconEvidenceTool().execute(project_id=project_id)


class GetBeaconEvidenceTool(BaseTextTool):
    name = "get_beacon_evidence"
    # GLOBAL like the structure graph it reads: structure data is keyed by project, not by
    # memory namespace, and query_structure already exposes the same facts to readonly callers.
    scope = ToolScope.GLOBAL
    required_tier = "readonly"
    title = "Get Beacon Evidence"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Return Beacon memory evidence (beacon-memory-evidence-1.1) for one indexed project, "
        "bound to the repository and commit it was indexed from. For Beacon's build."
    )

    async def endpoint(self, project_id: str) -> str:
        backend = self.get_backend()
        result = await backend.query_structure(project_id.strip(), "beacon_evidence")
        if isinstance(result, dict) and "error" in result:
            raise ValueError(str(result["error"]))
        return json.dumps(result, sort_keys=True, ensure_ascii=True)
