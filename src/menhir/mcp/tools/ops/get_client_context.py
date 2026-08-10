"""MCP tool: get_client_context — surfaces caller identity and time-since-last-access."""

from __future__ import annotations

from datetime import datetime, timezone

from menhir.mcp.service_access import get_request_session
from menhir.mcp.telemetry import telemetry_store
from menhir.mcp.tools.base import BaseTextTool


class GetClientContextTool(BaseTextTool):
    name = "get_client_context"
    required_tier = "readonly"
    description = (
        "Return caller identity (client_id, client_name) and last_accessed timestamp. "
        "Use at session start to know how much time has passed since you last interacted with memory."
    )

    async def endpoint(self) -> str:
        session = get_request_session()

        client_id = ""
        client_name = ""
        session_id = ""
        session_started_at = ""
        last_accessed: str | None = None

        if session is not None:
            client_id = session.client_id or ""
            client_name = session.client_name or session.user_id
            session_id = session.session_id
            session_started_at = session.started_at.isoformat()
            if session_id:
                last_accessed = telemetry_store.get_session_last_accessed(session_id)

        lines: list[str] = ["## Client Context"]

        if client_id:
            lines.append(f"- **client_id**: `{client_id}`")
        if client_name:
            lines.append(f"- **client_name**: {client_name}")
        if session_id:
            lines.append(f"- **session_id**: `{session_id}`")
        if session_started_at:
            lines.append(f"- **session_started_at**: {session_started_at}")

        if last_accessed:
            lines.append(f"- **last_accessed**: {last_accessed}")
            try:
                last_dt = datetime.fromisoformat(last_accessed)
                now = datetime.now(timezone.utc)
                delta = now - last_dt
                hours = delta.total_seconds() / 3600
                if hours < 1:
                    elapsed = f"{int(delta.total_seconds() / 60)}m"
                elif hours < 48:
                    elapsed = f"{hours:.1f}h"
                else:
                    elapsed = f"{delta.days}d"
                lines.append(f"- **elapsed_since_last_access**: {elapsed}")
            except Exception:
                pass
        else:
            lines.append("- **last_accessed**: (first session)")

        if not session:
            lines.append("")
            lines.append("_No caller session bound — running in local/unauthenticated mode._")

        return "\n".join(lines)
