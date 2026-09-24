"""MCP tool: get_beacon_evidence -- Menhir as a Beacon memory provider.

Beacon owns all beacon work; a memory provider only answers what it has indexed. This tool is
Menhir's side of that contract: it returns a ``beacon-memory-evidence-1.1`` document for one
indexed project, read from the graph only (no filesystem path, no git, no writes).

The project is named by its Menhir id, or found by the repository origin its last scan recorded
(identity lives only in Menhir's graph, so Beacon cannot read the id from the checkout).
"""

from __future__ import annotations

import json

from menhir.mcp.contracts import ToolScope
from menhir.mcp.telemetry.tracker import McpToolRefusal
from menhir.mcp.tools.base import BaseTextTool


async def get_beacon_evidence(project_id: str = "", repository: str = "") -> str:
    """Return Beacon memory evidence (``beacon-memory-evidence-1.1``) for one indexed project.

    The evidence is bound to the repository and git commit the project was indexed from, so
    Beacon can refuse to publish it against a different checkout. Read-only.

    Args:
        project_id: The project's stable Menhir id (``structure_project_id``).
        repository: Instead of ``project_id``: the checkout's ``origin`` URL. Served when exactly
            one indexed project recorded it; several checkouts of one repository are an error
            listing each project's id, name and root so the caller can choose.

    Returns:
        The evidence document as JSON text. A project that cannot be served (unknown id or
        repository, several matches, partial or in-progress index, indexed from a dirty checkout
        or without git) is an error naming the reason.
    """

    return await GetBeaconEvidenceTool().execute(project_id=project_id, repository=repository)


def _describe(projects: list[dict[str, str]]) -> str:
    return "; ".join(
        f"project_id={p['project_id']} name={p['name']} root={p['root_path']}" for p in projects
    )


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
        "by project id or by repository origin, bound to the repository and commit it was "
        "indexed from. For Beacon's build."
    )

    async def endpoint(self, project_id: str = "", repository: str = "") -> str:
        project_id, repository = project_id.strip(), repository.strip()
        if bool(project_id) == bool(repository):
            raise McpToolRefusal("name exactly one of project_id or repository")
        backend = self.get_backend()
        if repository:
            found = await backend.query_structure(repository, "beacon_projects_for_repository")
            if isinstance(found, dict) and "error" in found:
                raise McpToolRefusal(str(found["error"]))
            projects = list((found or {}).get("projects") or [])
            if not projects:
                raise McpToolRefusal(f"no indexed project recorded repository {repository}")
            if len(projects) > 1:
                raise McpToolRefusal(
                    f"{len(projects)} indexed projects recorded repository {repository}; "
                    f"name one with project_id: {_describe(projects)}"
                )
            project_id = projects[0]["project_id"]
        result = await backend.query_structure(project_id, "beacon_evidence")
        if isinstance(result, dict) and "error" in result:
            # An MCP error result, so Beacon can tell a refusal from evidence.
            raise McpToolRefusal(str(result["error"]))
        return json.dumps(result, sort_keys=True, ensure_ascii=True)
