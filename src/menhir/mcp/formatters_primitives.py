"""MCP formatter primitives — datetime coercion, validators, and filter resolvers."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from menhir.domain.models import ProcessingState


# ---------------------------------------------------------------------------
# datetime helpers
# ---------------------------------------------------------------------------

def _coerce_iso(value: object | None) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    rendered = str(value).strip()
    return rendered or None


def _parse_graph_datetime(value: object | None) -> datetime | None:
    iso_value = _coerce_iso(value)
    if not iso_value:
        return None
    candidate = iso_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# string normalizers / validators
# ---------------------------------------------------------------------------

def _require_episode_uuid(episode_uuid: str) -> str:
    candidate = (episode_uuid or "").strip()
    try:
        return str(UUID(candidate))
    except ValueError as exc:
        raise ValueError(f"Invalid episode UUID: {episode_uuid}") from exc


# ---------------------------------------------------------------------------
# filter resolvers
# ---------------------------------------------------------------------------

def _resolve_queue_state_filter(state: str) -> tuple[str, list[str] | None]:
    normalized = (state or "active").strip().lower()

    def _state_name(value: object) -> str:
        raw = getattr(value, "value", value)
        if isinstance(raw, str):
            return raw
        return str(raw)

    if normalized in {"active", "queued"}:
        return "ACTIVE", [
            _state_name(ProcessingState.PENDING),
            _state_name(ProcessingState.ENRICHING),
        ]
    if normalized == "all":
        return "ALL", None

    single_state_map = {
        "pending": _state_name(ProcessingState.PENDING),
        "enriching": _state_name(ProcessingState.ENRICHING),
        "ready": _state_name(ProcessingState.READY),
        "failed": _state_name(ProcessingState.FAILED),
    }
    mapped = single_state_map.get(normalized)
    if mapped is None:
        raise ValueError("Invalid state. Use: active, all, pending, enriching, ready, failed.")
    return mapped, [mapped]


def _resolve_conflict_status_filter(status: str) -> tuple[str, str | None]:
    normalized = (status or "unresolved").strip().lower()
    if normalized == "all":
        return "ALL", None
    allowed = {"unresolved", "resolved", "auto-resolved"}
    if normalized not in allowed:
        raise ValueError("Invalid status. Use: unresolved, resolved, auto-resolved, all.")
    return normalized.upper(), normalized


# ---------------------------------------------------------------------------
# conflict helpers
# ---------------------------------------------------------------------------

def _node_sort_key(member: dict[str, object]) -> tuple[int, str]:
    node_created = _parse_graph_datetime(member.get("node_created_at"))
    if node_created is None:
        return (1, str(member.get("uuid") or ""))
    return (0, node_created.isoformat())


def _coerce_conflict_members(row: dict[str, object]) -> list[dict[str, object]]:
    members_raw = row.get("members")
    members = [dict(member) for member in members_raw] if isinstance(members_raw, list) else []
    members.sort(key=_node_sort_key)
    return members


def _count_unresolved_members(row: dict[str, object] | None) -> int:
    if row is None:
        return 0
    members = _coerce_conflict_members(row)
    return sum(1 for member in members if str(member.get("status") or "") == "unresolved")


# ---------------------------------------------------------------------------
# stale-state helper
# ---------------------------------------------------------------------------

def _stale_reason_for_row(row: dict[str, object], *, now_utc: datetime) -> str | None:
    state = str(row.get("processing_state") or "")
    if state != ProcessingState.ENRICHING:
        return None

    lease_expires = _parse_graph_datetime(row.get("processing_lease_expires_at"))
    owner = row.get("processing_owner")
    if lease_expires is None:
        return "missing_lease"
    if lease_expires < now_utc:
        return "lease_expired"
    if owner is None:
        return "missing_owner"
    return None
