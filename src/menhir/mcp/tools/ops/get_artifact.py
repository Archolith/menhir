"""MCP tool: get_artifact."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def get_artifact(artifact_uuid: str, namespace: str = "") -> str:
    """Read one work artifact: a plan, review, investigation, report or handoff.

    Args:
        artifact_uuid: The artifact's stable uuid (from list_artifacts).
        namespace: Optional silo to enforce. Empty looks up by uuid alone.

    Returns:
        Status, embodiment locators, discussed code locations, and shape verdict.
    """
    return await GetArtifactTool().execute(artifact_uuid=artifact_uuid, namespace=namespace)


class GetArtifactTool(BaseTextTool):
    name = "get_artifact"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Get Work Artifact"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = "Read one work artifact (plan, review, investigation, report, handoff) in full."

    async def endpoint(self, artifact_uuid: str, namespace: str = "") -> str:
        backend = self.get_backend()
        row = await backend.get_artifact(artifact_uuid, namespace=namespace or None)
        if not row:
            scope = f" in namespace {namespace}" if namespace else ""
            return f"Artifact {artifact_uuid} not found{scope}"

        lines = [
            f"[{(row.get('artifact_type') or '?').upper()}] {row.get('title') or '(untitled)'}",
            f"uuid={row.get('artifact_uuid', artifact_uuid)}  status={row.get('status', '?')}"
            f"  namespace={row.get('namespace', '?')}",
        ]

        # The document's own words about its status, kept when they could not be
        # mapped -- so "nobody could read the header" stays distinguishable from
        # "this genuinely is PROPOSED".
        if row.get("status_unresolved_reason"):
            raw = (row.get("status_raw") or "").strip()
            detail = f' — document says: "{raw}"' if raw else ""
            lines.append(f"status unmapped: {row['status_unresolved_reason']}{detail}")

        created = (row.get("created_at") or "")[:10]
        changed = (row.get("status_changed_at") or "")[:10] or "-"
        lines.append(f"created: {created} | status changed: {changed}")

        for emb in row.get("embodiments") or []:
            if not emb:
                continue
            path = emb.get("locator_path") or "(no path)"
            repo = emb.get("locator_repository")
            where = f"{repo}:{path}" if repo else path
            version = f" @ {str(emb['version'])[:8]}" if emb.get("version") else ""
            lines.append(f"embodiment ({emb.get('medium', '?')}): {where}{version}")

        for loc in row.get("locations") or []:
            if not loc:
                continue
            if loc.get("resolution_status") != "resolved":
                lines.append(f"discusses: (unresolved: {loc.get('unresolved_reason')})")
                continue
            where = f"{loc.get('project')}:{loc.get('path')}" if loc.get("project") else loc.get("path")
            lines.append(f"discusses: {where}")

        shape = row.get("shape_status")
        if shape:
            violations = row.get("shape_violations") or []
            suffix = f" ({', '.join(violations[:5])})" if violations else ""
            lines.append(f"shape: {shape}{suffix}")

        return "\n".join(lines)
