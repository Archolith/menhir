"""MCP tool: resolve_conflict."""

from __future__ import annotations

import inspect
import logging
from itertools import combinations
from typing import Any

from menhir.mcp.formatters import _coerce_conflict_members, _count_unresolved_members
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope
from menhir.mcp.telemetry.tracker import McpToolRefusal

logger = logging.getLogger(__name__)

_CONFLICT_LOOKUP_LIMIT = 5000


async def resolve_conflict(
    group_id: str,
    action: str,
    keep_uuid: str | None = None,
    remove_uuid: str | None = None,
    dry_run: bool = False,
    allow_promoted_removal: bool = False,
    namespace: str = "",
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
        namespace=namespace,
    )


class ResolveConflictTool(BaseJsonTool):
    name = "resolve_conflict"
    # NAMESPACED once the group lookup is scoped (CF-33 step 4).
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Resolve Conflict"
    oauth_scopes = ("menhir:admin",)
    # Resolves the group and may remove member memory nodes (replace/discard_new).
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False
    description = "Resolve one conflict group using keep/replace/discard actions."

    async def endpoint(
        self,
        group_id: str,
        action: str,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        dry_run: bool = False,
        allow_promoted_removal: bool = False,
        namespace: str = "",
    ) -> str:
        try:
            backend = self.get_backend()
            normalized_group = (group_id or "").strip()
            if not normalized_group:
                raise McpToolRefusal("Invalid group_id: value is required.")

            normalized_action = (action or "").strip().lower()
            allowed_actions = {"keep_both", "replace", "discard_new"}
            if normalized_action not in allowed_actions:
                raise McpToolRefusal("Invalid action. Use: keep_both, replace, discard_new.")

            keep = (keep_uuid or "").strip() or None
            remove = (remove_uuid or "").strip() or None

            # CF-33 step 4: ownership-at-load keeps a foreign group on the not-found path.
            # The same scope must also reach the final mutation and readback. Current writers
            # create homogeneous groups, but legacy data can contain a mixed group; a scoped
            # lookup alone must never authorize mutation or disclosure of its foreign members.
            scope = {"namespace": namespace} if namespace else {}
            rows = await backend.list_conflict_groups(
                status=None, limit=_CONFLICT_LOOKUP_LIMIT, **scope
            )
            group_row = next((row for row in rows if str(row.get("group_id") or "") == normalized_group), None)
            if group_row is None:
                raise McpToolRefusal(f"Conflict group not found: {normalized_group}", code="not_found")

            members = _coerce_conflict_members(group_row)
            member_uuids = {str(member.get("uuid") or "") for member in members if member.get("uuid")}

            if normalized_action == "keep_both":
                if keep is not None or remove is not None:
                    raise McpToolRefusal("action=keep_both must not include keep_uuid/remove_uuid.")
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
                    **scope,
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
                refreshed_rows = await backend.list_conflict_groups(
                    status=None, limit=_CONFLICT_LOOKUP_LIMIT, **scope
                )
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
                raise McpToolRefusal(f"action={normalized_action} requires keep_uuid.")
            if keep not in member_uuids:
                raise McpToolRefusal(f"keep_uuid {keep} is not a member of group_id={normalized_group}.")
            if remove is not None and remove not in member_uuids:
                raise McpToolRefusal(f"remove_uuid {remove} is not a member of group_id={normalized_group}.")
            if remove is not None and remove == keep:
                raise McpToolRefusal("remove_uuid cannot equal keep_uuid.")

            would_remove = [remove] if remove else sorted(uuid for uuid in member_uuids if uuid != keep)
            if dry_run:
                # The real eligible-neighbour counts, with the same filters the bridge uses. A
                # backend without the estimate reports "unavailable", never a made-up number.
                estimator = getattr(backend, "estimate_bridges_for_nodes", None)
                bridge_estimate: dict[str, Any] | None = None
                if callable(estimator):
                    # An older server (unknown operation) or an adapter without the estimate
                    # raises; the preview then says "unavailable" instead of failing.
                    try:
                        pending = estimator(would_remove, **scope)
                        bridge_estimate = await pending if inspect.isawaitable(pending) else pending
                    except Exception as exc:
                        logger.warning("resolve_conflict dry run: bridge estimate unavailable: %s", exc)
                        bridge_estimate = None
                return self.render_json(
                    {
                        "dry_run": True,
                        "group_id": normalized_group,
                        "action": normalized_action,
                        "keep_uuid": keep,
                        "nodes_resolved": max(0, len(member_uuids) - len(would_remove)),
                        "nodes_gone": len(would_remove),
                        "group_clear_count": 1,
                        "bridge_count_estimate": (
                            int(bridge_estimate.get("estimated_edges") or 0)
                            if bridge_estimate is not None else None
                        ),
                        "bridge_over_cap_hubs": (
                            list(bridge_estimate.get("over_cap") or [])
                            if bridge_estimate is not None else []
                        ),
                        "bridge_neighbour_counts": (
                            dict(bridge_estimate.get("neighbour_counts") or {})
                            if bridge_estimate is not None else {}
                        ),
                        "bridge_estimate_available": bridge_estimate is not None,
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
                **scope,
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
            refreshed_rows = await backend.list_conflict_groups(
                status=None, limit=_CONFLICT_LOOKUP_LIMIT, **scope
            )
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
        except McpToolRefusal:
            raise
        except ValueError as exc:
            # A refused resolve carries its reason and changed nothing; it is never a success.
            if str(exc).startswith("Conflict resolve refused"):
                raise McpToolRefusal(
                    str(exc), code="refused",
                    hint="Nothing was changed; retry the resolve once the cause clears.",
                ) from exc
            raise McpToolRefusal(str(exc), code="invalid_argument") from exc


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
