"""MCP tool: review_inbox."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from menhir.domain.utils import excerpt
from menhir.infrastructure.todo_repository import TODO_STALE_AFTER_DAYS
from menhir.mcp.contracts import ToolScope
from menhir.mcp.formatters import _coerce_conflict_members, _resolve_queue_state_filter
from menhir.mcp.tools.base import BaseTextTool

logger = logging.getLogger(__name__)

_MIN_PER_CATEGORY = 1
_MAX_PER_CATEGORY = 25
_TODO_PAGE = 200
_LABEL_LIMIT = 100


async def review_inbox(namespace: str = "", per_category: int = 5) -> str:
    """Show everything waiting on the owner's decision, in one read.

    Covers unresolved conflicts, failed enrichments, candidates awaiting review, stale
    open TODOs and open artifact questions. Read-only: it changes nothing.

    Args:
        namespace: Optional silo to scope to. Empty spans every silo. Candidates are not
            namespace-scoped, so they are listed only when this is empty.
        per_category: Max items shown per section (default 5, clamped to 1..25).

    Returns:
        One section per category with a count, a few items, and the tools to act with.
    """
    return await ReviewInboxTool().execute(namespace=namespace, per_category=per_category)


def _label(value: object) -> str:
    return excerpt(value, limit=_LABEL_LIMIT) or "(empty)"


def _count(shown_rows: int, cap: int, more: bool) -> str:
    return f"{cap}+" if more else str(shown_rows)


class ReviewInboxTool(BaseTextTool):
    name = "review_inbox"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Review Inbox"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Read, in one call, what is waiting on the owner's decision: unresolved conflicts, "
        "failed enrichments, candidates, stale TODOs and open artifact questions."
    )

    async def endpoint(self, namespace: str = "", per_category: int = 5) -> str:
        cap = max(_MIN_PER_CATEGORY, min(int(per_category), _MAX_PER_CATEGORY))
        backend = self.get_backend()
        ns = namespace or None
        sections: list[Callable[[], Awaitable[tuple[str, list[str]] | None]]] = [
            lambda: self._conflicts(backend, ns, cap),
            lambda: self._failed_enrichments(backend, ns, cap),
            lambda: self._candidates(backend, namespace, cap),
            lambda: self._stale_todos(backend, ns, cap),
            lambda: self._artifact_questions(backend, ns, cap),
        ]
        names = ["Conflicts", "Failed enrichments", "Candidates", "Stale TODOs", "Open artifact questions"]

        lines = [f"Review inbox ({namespace or 'all namespaces'})"]
        any_waiting = False
        body: list[str] = []
        for name, build in zip(names, sections):
            try:
                result = await build()
            except Exception as exc:  # noqa: BLE001 - one failed read must not hide the rest
                logger.warning("review_inbox section %r unavailable: %s", name, type(exc).__name__)
                body.append(f"{name}: unavailable ({type(exc).__name__})")
                any_waiting = True
                continue
            if result is None:
                body.append(f"{name}: none")
                continue
            header, items = result
            any_waiting = any_waiting or bool(items)
            body.append(header)
            body.extend(items)
        if not any_waiting:
            lines.append("Nothing is waiting on a decision.")
            if namespace:
                lines.append("(Candidates are not checked on a namespace-scoped call.)")
            return "\n".join(lines)
        lines.extend(body)
        return "\n".join(lines)

    async def _conflicts(self, backend: Any, ns: str | None, cap: int):
        scope = {"namespace": ns} if ns else {}
        rows = await backend.list_conflict_groups(status="unresolved", limit=cap + 1, **scope)
        if not rows:
            return None
        more = len(rows) > cap
        items = []
        for row in rows[:cap]:
            members = _coerce_conflict_members(row)
            first = members[0] if members else {}
            summary = first.get("name") or first.get("content") or ""
            items.append(
                f"  group_id={row.get('group_id') or '?'} ({len(members)} members) - {_label(summary)}"
            )
        items.append("  Act with: list_conflicts, resolve_conflict")
        return f"Conflicts ({_count(len(rows), cap, more)}):", items

    async def _failed_enrichments(self, backend: Any, ns: str | None, cap: int):
        _, states = _resolve_queue_state_filter("failed")
        scope = {"namespace": ns} if ns else {}
        rows = await backend.list_episode_processing(states=states, limit=cap + 1, **scope)
        if not rows:
            return None
        more = len(rows) > cap
        items = [
            f"  uuid={row.get('uuid') or '?'} - {_label(row.get('processing_error') or row.get('name'))}"
            for row in rows[:cap]
        ]
        items.append("  Act with: get_enrichment_status (detail), force_reenrich (retry)")
        return f"Failed enrichments ({_count(len(rows), cap, more)}):", items

    async def _candidates(self, backend: Any, namespace: str, cap: int):
        if namespace:
            # list_candidates takes no namespace, so a scoped caller must not see other silos'.
            return "Candidates: not listed for a namespace-scoped call (they are not namespace-scoped)", []
        rows = await backend.list_candidates(limit=cap + 1)
        if not rows:
            return None
        more = len(rows) > cap
        items = [
            f"  uuid={row.get('uuid') or '?'} - "
            f"{_label(row.get('label') or row.get('name') or row.get('content'))}"
            for row in rows[:cap]
        ]
        items.append(
            "  Act with: approval is not available over MCP yet (#431); "
            "review and approve in the explorer"
        )
        return f"Candidates awaiting review ({_count(len(rows), cap, more)}):", items

    async def _stale_todos(self, backend: Any, ns: str | None, cap: int):
        rows = await backend.list_todos(status="open", limit=_TODO_PAGE, namespace=ns)
        stale = [row for row in rows if row.get("stale")]
        if not stale:
            return None
        more = len(rows) >= _TODO_PAGE
        items = []
        for row in stale[:cap]:
            age = row.get("age_days")
            age_str = f" (age {age}d)" if age is not None else ""
            items.append(f"  uuid={row.get('uuid') or '?'}{age_str} - {_label(row.get('content'))}")
        items.append("  Act with: resolve_todo, close_todo, supersede_todo")
        count = f"{len(stale)}+" if more else str(len(stale))
        return f"Stale TODOs ({count}, open longer than {TODO_STALE_AFTER_DAYS} days):", items

    async def _artifact_questions(self, backend: Any, ns: str | None, cap: int):
        rows = await backend.list_artifact_questions(status="open", namespace=ns, limit=cap + 1)
        if not rows:
            return None
        more = len(rows) > cap
        items = []
        for row in rows[:cap]:
            owner = row.get("artifact_title") or row.get("artifact_uuid") or "?"
            items.append(
                f"  question_uuid={row.get('question_uuid') or '?'} on {_label(owner)} - "
                f"{_label(row.get('text'))}"
            )
        items.append("  Act with: get_artifact (read the artifact), list_artifact_questions")
        return f"Open artifact questions ({_count(len(rows), cap, more)}):", items
