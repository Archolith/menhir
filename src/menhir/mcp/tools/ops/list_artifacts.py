"""MCP tool: list_artifacts."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def list_artifacts(
    artifact_type: str = "", status: str = "", namespace: str = "", limit: int = 25
) -> str:
    """List work artifacts: plans, reviews, investigations, reports, handoffs.

    Args:
        artifact_type: Filter by plan | review | investigation | implementation_report
                       | handoff. Empty lists every type.
        status: Filter by lifecycle status, e.g. APPROVED or IMPLEMENTED.
        namespace: Optional silo. Empty lists every silo; supplying one narrows
                   to that silo plus the shared "default" bucket.
        limit: Max rows (default 25, max 200).

    Returns:
        A summary per artifact (type, status, uuid and title), plus a note when
        some statuses could not be read. Call get_artifact with a uuid to see
        source paths and discussed code locations.
    """
    return await ListArtifactsTool().execute(
        artifact_type=artifact_type, status=status, namespace=namespace, limit=limit
    )


class ListArtifactsTool(BaseTextTool):
    name = "list_artifacts"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "List Work Artifacts"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "List recorded plans, reviews, investigations, reports and handoffs, filtered by type, "
        "status, or namespace. Returns artifact summaries (type, status, uuid and title). Use "
        "get_artifact for source paths and the code each artifact discusses."
    )

    async def endpoint(
        self, artifact_type: str = "", status: str = "", namespace: str = "", limit: int = 25
    ) -> str:
        backend = self.get_backend()
        rows = await backend.list_artifacts(
            artifact_type=artifact_type or None,
            status=status or None,
            namespace=namespace or None,
            limit=min(limit, 200),
        )

        filters = ", ".join(
            f"{label}={value}"
            for label, value in (
                ("type", artifact_type), ("status", status), ("namespace", namespace)
            )
            if value
        )
        header = f"artifacts ({len(rows)})" + (f" [{filters}]" if filters else "")
        lines = [header]
        if not rows:
            lines.append("(none)")
            return "\n".join(lines)

        for row in rows:
            kind = (row.get("artifact_type") or "?")[:20]
            uuid = row.get("artifact_uuid", "?")
            title = row.get("title") or "(untitled)"
            lines.append(f"[{kind}] {row.get('status', '?')}  uuid={uuid}")
            lines.append(f"        {title}")

        unmapped = sum(1 for r in rows if r.get("status_unresolved_reason"))
        if unmapped:
            lines.append(
                f"note: {unmapped} artifact(s) hold their type's initial status because their "
                "Status header could not be read -- not because they are in that state"
            )
        return "\n".join(lines)
