"""MCP tool: resolve_conflict."""

from __future__ import annotations

import logging
from itertools import combinations

from menhir.mcp.formatters import _coerce_conflict_members, _count_unresolved_members
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope

logger = logging.getLogger(__name__)

_CONFLICT_LOOKUP_LIMIT = 5000


async def resolve_conflict(
    group_id: str,
    action: str,
    keep_uuid: str | None = None,
    remove_uuid: str | None = None,
    dry_run: bool = False,
    allow_promoted_removal: bool = False,
) -> str:
    """Resolve one conflict group using keep/replace/discard actions.

    Args:
        group_id: Conflict group id.
        action: "keep_both", "replace", or "discard_new".
        keep_uuid: UUID to keep (required for replace/discard_new).
        remove_uuid: Optional UUID to remove for replace/discard_new.
        dry_run: If true, return a preview and perform no mutation.
        allow_promoted_removal: Allow removing PROMOTED nodes when true.

    Returns:
        Resolution summary or dry-run preview.
    """

    return await ResolveConflictTool().execute(
        group_id=group_id,
        action=action,
        keep_uuid=keep_uuid,
        remove_uuid=remove_uuid,
        dry_run=dry_run,
        allow_promoted_removal=allow_promoted_removal,
    )


class ResolveConflictTool(BaseJsonTool):
    name = "resolve_conflict"
    scope = ToolScope.OBJECT
    required_tier = "operator"
    description = "Resolve one conflict group using keep/replace/discard actions."

    async def endpoint(
        self,
        group_id: str,
        action: str,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        dry_run: bool = False,
        allow_promoted_removal: bool = False,
    ) -> str:
        try:
            backend = self.get_backend()
            normalized_group = (group_id or "").strip()
            if not normalized_group:
                return self.render_json(
                    {"ok": False, "tool": self.operation, "error": {"message": "Invalid group_id: value is required."}}
                )

            normalized_action = (action or "").strip().lower()
            allowed_actions = {"keep_both", "replace", "discard_new"}
            if normalized_action not in allowed_actions:
                return self.render_json(
                    {
                        "ok": False,
                        "tool": self.operation,
                        "error": {"message": "Invalid action. Use: keep_both, replace, discard_new."},
                    }
                )

            keep = (keep_uuid or "").strip() or None
            remove = (remove_uuid or "").strip() or None

            rows = await backend.list_conflict_groups(status=None, limit=_CONFLICT_LOOKUP_LIMIT)
            group_row = next((row for row in rows if str(row.get("group_id") or "") == normalized_group), None)
            if group_row is None:
                return f"Conflict group not found: {normalized_group}"

            members = _coerce_conflict_members(group_row)
            member_uuids = {str(member.get("uuid") or "") for member in members if member.get("uuid")}

            if normalized_action == "keep_both":
                if keep is not None or remove is not None:
                    return self.render_json(
                        {
                            "ok": False,
                            "tool": self.operation,
                            "error": {"message": "action=keep_both must not include keep_uuid/remove_uuid."},
                        }
                    )
                if dry_run:
                    return self.render_json(
                        {
                            "dry_run": True,
                            "group_id": normalized_group,
                            "action": "keep_both",
                            "nodes_resolved": len(member_uuids),
                            "nodes_gone": 0,
                            "group_clear_count": 1,
                            "bridge_count_estimate": 0,
                        }
                    )
                result = await backend.resolve_conflict_group(
                    normalized_group,
                    action=normalized_action,
                    resolution_status="resolved",
                    allow_promoted_removal=allow_promoted_removal,
                )
                # Record suppression for all member pairs (keep_both = blanket)
                result_uuids = result.get("member_uuids") or []
                for ua, ub in combinations(result_uuids, 2):
                    await _record_suppression(
                        backend,
                        ua,
                        ub,
                        "resolved",
                        normalized_group,
                        "keep_both",
                        "user",
                    )
                refreshed_rows = await backend.list_conflict_groups(status=None, limit=_CONFLICT_LOOKUP_LIMIT)
                refreshed = next(
                    (row for row in refreshed_rows if str(row.get("group_id") or "") == normalized_group),
                    None,
                )
                return self.render_json(
                    {
                        "group_id": normalized_group,
                        "action": normalized_action,
                        "resolved": int(result.get("resolved") or 0),
                        "removed_uuids": result.get("removed_uuids") or [],
                        "bridged_edges": int(result.get("bridged_edges") or 0),
                        "group_cleared": refreshed is None,
                        "remaining_unresolved_in_group": _count_unresolved_members(refreshed),
                        "journal_op_id": None,
                    }
                )

            if keep is None:
                return self.render_json(
                    {
                        "ok": False,
                        "tool": self.operation,
                        "error": {"message": f"action={normalized_action} requires keep_uuid."},
                    }
                )
            if keep not in member_uuids:
                return self.render_json(
                    {
                        "ok": False,
                        "tool": self.operation,
                        "error": {"message": f"keep_uuid {keep} is not a member of group_id={normalized_group}."},
                    }
                )
            if remove is not None and remove not in member_uuids:
                return self.render_json(
                    {
                        "ok": False,
                        "tool": self.operation,
                        "error": {"message": f"remove_uuid {remove} is not a member of group_id={normalized_group}."},
                    }
                )
            if remove is not None and remove == keep:
                return self.render_json(
                    {
                        "ok": False,
                        "tool": self.operation,
                        "error": {"message": "remove_uuid cannot equal keep_uuid."},
                    }
                )

            would_remove = [remove] if remove else sorted(uuid for uuid in member_uuids if uuid != keep)
            if dry_run:
                return self.render_json(
                    {
                        "dry_run": True,
                        "group_id": normalized_group,
                        "action": normalized_action,
                        "keep_uuid": keep,
                        "nodes_resolved": max(0, len(member_uuids) - len(would_remove)),
                        "nodes_gone": len(would_remove),
                        "group_clear_count": 1,
                        "bridge_count_estimate": len(would_remove),
                        "would_remove_uuids": would_remove,
                        "allow_promoted_removal": allow_promoted_removal,
                    }
                )

            result = await backend.resolve_conflict_group(
                normalized_group,
                action=normalized_action,
                keep_uuid=keep,
                remove_uuid=remove,
                resolution_status="resolved",
                allow_promoted_removal=allow_promoted_removal,
            )
            # Record suppression for the keep/remove pair(s)
            for removed in result.get("removed_uuids") or []:
                await _record_suppression(
                    backend,
                    keep,
                    removed,
                    "resolved",
                    normalized_group,
                    normalized_action,
                    "user",
                )
            refreshed_rows = await backend.list_conflict_groups(status=None, limit=_CONFLICT_LOOKUP_LIMIT)
            refreshed = next(
                (row for row in refreshed_rows if str(row.get("group_id") or "") == normalized_group),
                None,
            )
            implicit_keep_one = remove is None and normalized_action in {"replace", "discard_new"}
            payload = {
                "group_id": normalized_group,
                "action": normalized_action,
                "keep_uuid": keep,
                "resolved": int(result.get("resolved") or 0),
                "removed_uuids": result.get("removed_uuids") or [],
                "bridged_edges": int(result.get("bridged_edges") or 0),
                "group_cleared": refreshed is None,
                "remaining_unresolved_in_group": _count_unresolved_members(refreshed),
                "journal_op_id": None,
            }
            if implicit_keep_one:
                payload["confirmation"] = (
                    f"Removed {len(result.get('removed_uuids') or [])} node(s) as non-keep members of group."
                )
            return self.render_json(payload)
        except ValueError as exc:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {"message": str(exc)},
                }
            )


async def _record_suppression(
    backend: object,
    uuid_a: str,
    uuid_b: str,
    status: str,
    group_id: str,
    action: str,
    reviewed_by: str,
) -> None:
    """Best-effort write of a conflict suppression row to SQLite."""
    if not uuid_a or not uuid_b:
        return
    try:
        await backend.record_conflict_resolution(
            uuid_a=uuid_a, uuid_b=uuid_b,
            status=status, group_id=group_id,
            action=action, reviewed_by=reviewed_by,
        )
    except Exception:
        logger.debug("Failed to record suppression for %s <-> %s", uuid_a, uuid_b, exc_info=True)
