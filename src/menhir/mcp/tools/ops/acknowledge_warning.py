"""MCP tool: acknowledge_warning."""

from __future__ import annotations

from menhir.mcp.contracts import ToolScope
from menhir.mcp.service_access import get_request_session
from menhir.mcp.tools.base import BaseTextTool

_KINDS = ("provider_auth", "provider_billing", "failed_episodes")


async def acknowledge_warning(kind: str, subject: str = "") -> str:
    """Acknowledge an operator warning so it stops appearing in tool responses.

    The acknowledgement covers the current incident only: if the condition clears and later
    recurs (or, for failed_episodes, a new episode fails), the warning is raised again.

    Args:
        kind: provider_auth, provider_billing, or failed_episodes.
        subject: LLM call kind (chat, embedding, response) for provider kinds; empty
            acknowledges every active subject of the kind.

    Returns:
        What was acknowledged.
    """

    return await AcknowledgeWarningTool().execute(kind=kind, subject=subject)


class AcknowledgeWarningTool(BaseTextTool):
    name = "acknowledge_warning"
    scope = ToolScope.GLOBAL
    required_tier = "operator"
    description = "Acknowledge an operator warning (provider_auth, provider_billing, failed_episodes) for its current incident."
    title = "Acknowledge Warning"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(self, kind: str, subject: str = "") -> str:
        kind = (kind or "").strip()
        subject = (subject or "").strip()
        if kind not in _KINDS:
            return f"error: unknown warning kind {kind!r}; expected one of: {', '.join(_KINDS)}"
        session = get_request_session()
        actor = ""
        if session is not None:
            actor = session.client_name or session.user_id or ""
        result = await self.get_backend().acknowledge_operator_warning(
            kind=kind, subject=subject, actor=actor
        )
        acked = list(result.get("acknowledged") or [])
        already = list(result.get("already_acknowledged") or [])
        if not result.get("active"):
            return f"nothing to acknowledge: no active {kind} warning" + (
                f" for subject {subject!r}" if subject else ""
            )
        lines = []
        if acked:
            lines.append(f"acknowledged {kind}: {', '.join(acked)}")
        if already:
            lines.append(f"already acknowledged {kind}: {', '.join(already)}")
        if not lines:
            lines.append(f"nothing acknowledged: {kind} changed while acknowledging; retry")
        return "\n".join(lines)
