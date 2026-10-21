"""Drill-down read: the items a scalar View was built from, page by page."""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import stamped_namespace


def _item(raw: dict[str, Any], *, relation: str | None = None) -> dict[str, Any]:
    return {
        "assertion_id": str(raw.get("assertion_id") or ""),
        "relation": relation if relation is not None else str(raw.get("relation") or ""),
        "operation": str(raw.get("operation") or ""),
        "value": raw.get("value"),
        "quote": str(raw.get("stated_span") or ""),
        "valid_at": str(raw.get("valid_at") or ""),
        "evidence_tier": str(raw.get("evidence_tier") or ""),
        "episode_uuid": str(raw.get("episode_uuid") or ""),
    }


def fetch_scalar_view_items(
    adapter: Any,
    *,
    view_uuid: str,
    namespace: str | None,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Page the contributors of a scalar_state View, or the entries of a scalar_history View.

    The adapter wrappers apply the counting kill switch, so occurrences stay hidden when counting
    is off. ``namespace=None`` resolves to the default silo, never an unscoped read.
    """
    safe_limit = max(1, min(int(limit), 50))
    safe_offset = max(0, int(offset))
    ns = stamped_namespace(namespace)

    state = adapter.fetch_scalar_authority_contributors(
        view_uuid=view_uuid, limit=safe_limit, offset=safe_offset, namespace=ns,
    ) or {}
    if int(state.get("total") or 0) > 0:
        return {
            "view_uuid": view_uuid, "kind": "scalar_state",
            "total": int(state.get("total") or 0), "offset": safe_offset,
            "next_offset": state.get("next_offset"),
            "items": [_item(c) for c in (state.get("contributors") or [])],
        }

    history = adapter.list_scalar_history_entries(
        view_uuid=view_uuid, offset=safe_offset, limit=safe_limit, namespace=ns,
    ) or {}
    if int(history.get("total") or 0) > 0:
        return {
            "view_uuid": view_uuid, "kind": "scalar_history",
            "total": int(history.get("total") or 0), "offset": safe_offset,
            "next_offset": history.get("next_offset"),
            "items": [_item(e, relation="HISTORY_ENTRY") for e in (history.get("entries") or [])],
        }

    return {
        "view_uuid": view_uuid, "kind": None, "total": 0, "offset": safe_offset,
        "next_offset": None, "items": [],
    }
