"""MCP tool: get_todo."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def get_todo(uuid: str, namespace: str = "") -> str:
    """Read one TODO in full, including content list_todos truncates.

    Args:
        uuid: The UUID of the TODO (from add_todo or list_todos output).
        namespace: Optional silo to enforce. Empty looks up by uuid alone; the
                   TODO's namespace is reported either way.

    Returns:
        The complete TODO record with its linked file, episode, and entities.
    """
    return await GetTodoTool().execute(uuid=uuid, namespace=namespace)


class GetTodoTool(BaseTextTool):
    name = "get_todo"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    description = "Read one TODO in full, including content that list_todos truncates."

    async def endpoint(self, uuid: str, namespace: str = "") -> str:
        backend = self.get_backend()
        row = await backend.get_todo(uuid, namespace=namespace or None)
        if not row:
            scope = f" in namespace {namespace}" if namespace else ""
            return f"TODO {uuid} not found{scope}"

        tag = (row.get("priority") or "normal").upper()
        lines = [f"[{tag}] uuid={row.get('uuid', uuid)}  status={row.get('status', '?')}"]
        if row.get("namespace"):
            lines.append(f"namespace: {row['namespace']}")

        created = (row.get("created_at") or "")[:10]
        closed = (row.get("closed_at") or "")[:10] or "-"
        age = row.get("age_days")
        age_str = f" | age: {age}d" if age is not None else ""
        stale_marker = " | STALE" if row.get("stale") else ""
        lines.append(f"created: {created} | closed: {closed}{age_str}{stale_marker}")

        if row.get("due_date"):
            lines.append(f"due: {row['due_date']}")
        if row.get("source"):
            lines.append(f"source: {row['source']}")
        if row.get("code_ref"):
            lines.append(f"code_ref: {row['code_ref']}")
        if row.get("linked_file_path"):
            project = row.get("linked_file_project")
            suffix = f" ({project})" if project else ""
            lines.append(f"linked file: {row['linked_file_path']}{suffix}")
        if row.get("episode_uuid"):
            lines.append(f"created from episode: {row['episode_uuid']}")
        locations = row.get("locations") or []
        for loc in locations:
            if loc.get("resolution_status") != "resolved":
                lines.append(f"location: (unresolved: {loc.get('unresolved_reason')})")
                continue
            where = f"{loc.get('project')}:{loc.get('path')}" if loc.get("project") else loc.get("path")
            span = ""
            if loc.get("line_start"):
                span = f":{loc['line_start']}"
                if loc.get("line_end"):
                    span += f"-{loc['line_end']}"
            sym = f" ({loc['symbol']})" if loc.get("symbol") else ""
            lines.append(f"location: {where}{span}{sym}")

        for link in row.get("inbound_links") or []:
            name = link.get("memory_name") or link.get("memory_uuid", "?")
            lines.append(f"{str(link.get('relation', '')).lower()}: {name}")

        lines.append("")
        lines.append(row.get("content") or "(no content)")
        return "\n".join(lines)
