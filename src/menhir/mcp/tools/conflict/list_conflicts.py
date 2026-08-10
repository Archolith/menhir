"""MCP tool: list_conflicts."""

from __future__ import annotations

from menhir.domain.utils import excerpt
from menhir.mcp.formatters import (
    _coerce_conflict_members,
    _coerce_iso,
    _resolve_conflict_status_filter,
)
from menhir.mcp.tools.base import BaseJsonTool


async def list_conflicts(status: str = "unresolved", limit: int = 25) -> str:
    """List conflict groups detected by contradiction checks.

    Args:
        status: Filter by status ("unresolved", "resolved", "auto-resolved", "all").
        limit: Max groups to return (default: 25, max: 200).

    Returns:
        Grouped conflict summary with member details and a suggested resolve command.
    """

    return await ListConflictsTool().execute(status=status, limit=limit)


class ListConflictsTool(BaseJsonTool):
    name = "list_conflicts"
    required_tier = "readonly"
    description = "List conflict groups detected by contradiction checks."

    async def endpoint(self, status: str = "unresolved", limit: int = 25) -> str:
        try:
            backend = self.get_backend()
            _, status_filter = _resolve_conflict_status_filter(status)
            rows = await backend.list_conflict_groups(status=status_filter, limit=limit)
            groups: list[dict[str, object]] = []
            for row in rows:
                group_id = str(row.get("group_id") or "")
                members = _coerce_conflict_members(row)
                group_payload: dict[str, object] = {
                    "status": str(status_filter or "all"),
                    "group_id": group_id or None,
                    "detected_at": _coerce_iso(row.get("created_at")),
                    "member_count": len(members),
                    "members": [
                        {
                            "uuid": member.get("uuid"),
                            "name": member.get("name") or member.get("uuid"),
                            "status": member.get("status") or "unknown",
                            "tag": "older" if index == 0 else "newer",
                            "summary": excerpt(member.get("content"), limit=120),
                        }
                        for index, member in enumerate(members)
                    ],
                }
                if len(members) >= 2:
                    group_payload["suggested_resolution"] = {
                        "tool": "resolve_conflict",
                        "group_id": group_id or None,
                        "action": "replace",
                        "keep_uuid": members[0].get("uuid"),
                        "remove_uuid": members[1].get("uuid"),
                    }
                groups.append(group_payload)

            return self.render_json(
                {
                    "status": str(status_filter or "all"),
                    "count": len(groups),
                    "groups": groups,
                }
            )
        except ValueError as exc:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {"message": str(exc)},
                }
            )
