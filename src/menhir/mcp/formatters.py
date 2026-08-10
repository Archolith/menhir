"""MCP formatting helpers — pure data transforms and episode polling helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from time import monotonic, perf_counter
from uuid import UUID

from menhir.core.reader_identity import normalize_reader_id as _normalize_reader_id
from menhir.domain.models import ProcessingState
from menhir.domain.utils import excerpt
from menhir.services.stale_labeling import (
    STALE_ACTION, STALE_ACTION_OUTDATED,
    STALE_ADVISORY, STALE_ADVISORY_OUTDATED, STALE_ADVISORY_STILL_VALID,
)

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# JSON / compact item serializers
# ---------------------------------------------------------------------------

def _compact_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, default=str)


def _compact_memory_item(row: dict[str, object], *, tag: str) -> dict[str, object]:
    item = {
        "uuid": row.get("uuid"),
        "name": row.get("name") or row.get("uuid") or "(unnamed)",
        "scope": row.get("scope") or "UNKNOWN",
        "type": row.get("type") or "UNKNOWN",
        "tag": tag,
        "summary": excerpt(row.get("summary") or row.get("content")),
    }
    if row.get("similarity") is not None:
        item["similarity"] = row.get("similarity")
    # Stale-anchor advisory: pass through when the recall service labeled this item.
    stale_info = row.get("stale_anchor_info")
    if stale_info is not None:
        item["stale_anchor"] = stale_info.get("stale_anchor", False)
        if stale_info.get("stale_anchor"):
            item["stale_reason"] = stale_info.get("stale_reason")
            item["dirty_at"] = stale_info.get("dirty_at")
            item["anchored_at"] = stale_info.get("anchored_at")
            item["path"] = stale_info.get("path")
            action, advisory = _resolve_stale_advisory(stale_info)
            item["stale_action"] = action
            item["stale_advisory"] = advisory
            verification = stale_info.get("stale_verification")
            if verification:
                item["stale_verification"] = verification
    return item


def _bd(breakdown: object | None, key: str) -> float:
    """Extract a breakdown value that may be a dict (from JSON round-trip) or a dataclass."""
    if breakdown is None:
        return 0.0
    if isinstance(breakdown, dict):
        return float(breakdown.get(key, 0.0) or 0.0)
    return float(getattr(breakdown, key, 0.0) or 0.0)


def _tf(fact: object, key: str) -> object:
    """Extract a temporal-fact value that may be a dict (post-asdict JSON round-trip) or a dataclass.

    The MCP recall path serializes RecallResult via asdict() before this formatter
    runs, so each temporal fact arrives as a plain dict; direct paths may pass the
    TemporalFact dataclass. Mirror _bd and tolerate both.
    """
    if isinstance(fact, dict):
        return fact.get(key)
    return getattr(fact, key, None)


def _resolve_stale_advisory(stale_info: dict[str, object]) -> tuple[str, str]:
    """Return (action, advisory) for a stale item, adjusted by verification outcome."""
    action: str = STALE_ACTION
    advisory: str = STALE_ADVISORY
    verification = stale_info.get("stale_verification")
    if verification and isinstance(verification, dict):
        outcome = str(verification.get("outcome") or "")
        if outcome == "still_valid":
            advisory = STALE_ADVISORY_STILL_VALID
        elif outcome == "outdated":
            action = STALE_ACTION_OUTDATED
            advisory = STALE_ADVISORY_OUTDATED
    return action, advisory


def _compact_scored_item(scored: object, compact: bool = False) -> dict[str, object]:
    breakdown = getattr(scored, "breakdown", None)
    sim_raw = _bd(breakdown, "semantic_similarity")
    relevance = "high" if sim_raw >= 0.7 else "medium" if sim_raw >= 0.4 else "low"
    item: dict[str, object] = {
        "uuid": getattr(scored, "uuid", None),
        "name": getattr(scored, "name", None),
        "scope": getattr(scored, "scope", None),
        "score": round(float(getattr(scored, "final_score", 0.0) or 0.0), 3),
        "relevance": relevance,
        "summary": excerpt(getattr(scored, "summary", None) or getattr(scored, "content", None)),
        "retrieval_score": round(
            float(getattr(scored, "retrieval_score", sim_raw) or 0.0), 6
        ),
        "retrieval_score_kind": getattr(
            getattr(scored, "retrieval_score_kind", "graphiti_rrf"),
            "value",
            getattr(scored, "retrieval_score_kind", "graphiti_rrf"),
        ),
        "relevance_basis": "legacy_rrf_threshold_unvalidated",
    }
    # Frontier warden gate: a FLAG label (historical/conflict/uncertain) is decision-relevant,
    # so it is kept even in compact mode. Absent on the old path (None) -> omitted.
    warden_label = getattr(scored, "warden_label", None)
    if warden_label:
        item["warden_label"] = warden_label
    # Phase 4a.4/4c: the deterministically-injected current scalar_state View is the authoritative
    # CURRENT value for a surfaced slot. Marked so the consumer leads with it (the other observations
    # are its history/provenance). Decision-relevant -> kept even in compact mode.
    if getattr(scored, "is_scalar_authority", False):
        item["is_scalar_authority"] = True
    # Hook Center stale-anchor metadata: present only when the recall service
    # performed stale labeling (stale_anchor_info is a dict). Always includes
    # "stale_anchor": true|false. Additional fields for stale items:
    # stale_reason, dirty_at, anchored_at, path.
    stale_info = getattr(scored, "stale_anchor_info", None)
    if stale_info is not None:
        item["stale_anchor"] = stale_info.get("stale_anchor", False)
        if stale_info.get("stale_anchor"):
            item["stale_reason"] = stale_info.get("stale_reason")
            item["dirty_at"] = stale_info.get("dirty_at")
            item["anchored_at"] = stale_info.get("anchored_at")
            item["path"] = stale_info.get("path")
            action, advisory = _resolve_stale_advisory(stale_info)
            item["stale_action"] = action
            item["stale_advisory"] = advisory
            verification = stale_info.get("stale_verification")
            if verification:
                item["stale_verification"] = verification
    # Source/world time changes answer selection, so it is decision-relevant and survives compact
    # mode. Belief-time fields stay explicit and never stand in for an absent valid_at.
    temporal_facts_raw = getattr(scored, "temporal_facts", None) or ()
    if temporal_facts_raw:
        item["temporal_facts"] = [
            {
                "fact": _tf(tf, "fact"),
                "valid_at": _tf(tf, "valid_at"),
                "invalid_at": _tf(tf, "invalid_at"),
                "created_at": _tf(tf, "created_at"),
                "expired_at": _tf(tf, "expired_at"),
                "is_current_belief": _tf(tf, "is_current_belief"),
                "temporal_role": _tf(tf, "temporal_role"),
                # Rung 1C: render happened-time (world) vs learned-time (belief) legibly.
                "when": _format_when(tf),
            }
            for tf in temporal_facts_raw
        ]
    if compact:
        # Drop explainability/diagnostic fields the LLM does not act on. The
        # decision-relevant fields (score, scope, relevance) are retained.
        return item
    item["type"] = getattr(scored, "memory_type", None)
    item["breakdown"] = {
        "sim": round(sim_raw, 3),
        "adj": round(_bd(breakdown, "adjacency_bonus"), 3),
        "rec": round(_bd(breakdown, "recency_bonus"), 3),
        "prom": round(_bd(breakdown, "prominence_bonus"), 3),
    }
    return item


def _format_when(tf: object) -> str:
    """Rung 1C: a compact happened-vs-learned phrase for one temporal fact.

    happened = world time (valid_at..invalid_at); learned = belief time
    (created_at, and expired_at when superseded). Keeps the LLM from conflating
    'when it was true' with 'when we found out'.
    """
    valid_at = _tf(tf, "valid_at")
    invalid_at = _tf(tf, "invalid_at")
    created_at = _tf(tf, "created_at")
    expired_at = _tf(tf, "expired_at")

    if invalid_at:
        happened = f"happened {valid_at or '?'} until {invalid_at}"
    elif valid_at:
        happened = f"happened from {valid_at}"
    else:
        happened = "happened (time unstated)"

    if expired_at:
        learned = f"learned {created_at or '?'}, superseded {expired_at}"
    elif created_at:
        learned = f"learned {created_at}"
    else:
        learned = "learned (time unstated)"

    return f"{happened}; {learned}"


# ---------------------------------------------------------------------------
# episode status polling
# ---------------------------------------------------------------------------

async def _collect_episode_status(
    backend: object,
    episode_uuid: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> tuple[dict[str, object] | None, list[dict[str, object]], bool]:
    deadline = perf_counter() + max(0.0, timeout_s)
    history: list[dict[str, object]] = []
    last_signature: tuple[object, ...] | None = None
    timed_out = False
    current: dict[str, object] | None = None

    while True:
        if hasattr(backend, "fetch_episode_processing"):
            current = await backend.fetch_episode_processing(episode_uuid)
        else:
            current = backend.graph_adapter.fetch_episode_processing(episode_uuid)
        if current is None:
            break

        state = str(current.get("processing_state") or "UNKNOWN")
        signature = (
            state,
            current.get("processing_stage"),
            current.get("processing_substage"),
            _coerce_iso(current.get("processing_substage_started_at")),
            current.get("processing_progress"),
            current.get("processing_steps_total"),
            current.get("processing_steps_completed"),
            current.get("processing_llm_tasks_attempt"),
            current.get("processing_llm_tasks_total"),
            current.get("processing_attempts"),
            current.get("processing_error"),
            current.get("processing_llm_active_task"),
            current.get("processing_llm_active_kind"),
            current.get("processing_llm_active_model"),
            current.get("processing_llm_active_endpoint"),
            _coerce_iso(current.get("processing_llm_last_task_at")),
            _coerce_iso(current.get("processing_heartbeat_at")),
            _coerce_iso(current.get("processing_started_at")),
            _coerce_iso(current.get("processing_completed_at")),
        )
        if signature != last_signature:
            history.append(
                {
                    "state": state,
                    "stage": str(current.get("processing_stage") or ""),
                    "substage": str(current.get("processing_substage") or ""),
                    "substage_started_at": _coerce_iso(current.get("processing_substage_started_at")),
                    "progress": float(current.get("processing_progress") or 0.0),
                    "steps_total": int(current.get("processing_steps_total") or 0),
                    "steps_completed": int(current.get("processing_steps_completed") or 0),
                    "llm_tasks_attempt": int(current.get("processing_llm_tasks_attempt") or 0),
                    "llm_tasks_total": int(current.get("processing_llm_tasks_total") or 0),
                    "attempts": int(current.get("processing_attempts") or 0),
                    "queue_depth": (
                        int(await backend.get_queue_depth())
                        if hasattr(backend, "get_queue_depth")
                        else backend.ingest_service.get_queue_depth()
                    ),
                    "processing_error": current.get("processing_error"),
                    "llm_active_task": current.get("processing_llm_active_task"),
                    "llm_active_kind": current.get("processing_llm_active_kind"),
                    "llm_active_model": current.get("processing_llm_active_model"),
                    "llm_active_endpoint": current.get("processing_llm_active_endpoint"),
                    "llm_last_task_at": _coerce_iso(current.get("processing_llm_last_task_at")),
                    "heartbeat_at": _coerce_iso(current.get("processing_heartbeat_at")),
                    "started_at": _coerce_iso(current.get("processing_started_at")),
                    "completed_at": _coerce_iso(current.get("processing_completed_at")),
                }
            )
            last_signature = signature

        if state in {ProcessingState.READY, ProcessingState.FAILED}:
            break
        if perf_counter() >= deadline:
            timed_out = True
            break
        await asyncio.sleep(max(0.05, poll_interval_s))

    return current, history, timed_out


# ---------------------------------------------------------------------------
# episode output formatters
# ---------------------------------------------------------------------------

def _format_episode_status(
    *,
    episode_uuid: str,
    row: dict[str, object] | None,
    history: list[dict[str, object]],
    timed_out: bool,
) -> str:
    lines = [f"episode_id: {episode_uuid}"]
    if row is None:
        lines.append("status: not_found")
        return "\n".join(lines)

    lines.extend(
        [
            f"status: {row.get('processing_state') or 'UNKNOWN'}",
            f"stage: {row.get('processing_stage') or '(none)'}",
            f"substage: {row.get('processing_substage') or '(none)'}",
            f"progress: {float(row.get('processing_progress') or 0.0):.1f}",
            f"steps: {int(row.get('processing_steps_completed') or 0)}/{int(row.get('processing_steps_total') or 0)}",
            f"llm_tasks_attempt: {int(row.get('processing_llm_tasks_attempt') or 0)}",
            f"llm_tasks_total: {int(row.get('processing_llm_tasks_total') or 0)}",
            f"attempts: {int(row.get('processing_attempts') or 0)}",
            f"error: {row.get('processing_error') or '(none)'}",
            f"timed_out: {timed_out}",
            f"updates: {len(history)}",
        ]
    )
    if timed_out:
        state = str(row.get("processing_state") or "").upper()
        if state in ("ENRICHING", "PENDING", "QUEUED", ""):
            # The episode is still being enriched in the background — it is NOT lost.
            # Tell the agent not to re-add (a retry duplicates work and lands on the
            # in-progress enrichment). Enrichment commonly takes 20-50s.
            lines.append(
                "guidance: still enriching in the background — the memory IS queued and "
                "will finish on its own. Do NOT call add_memory again for this; if you "
                "need to confirm completion, poll get_enrichment_status or call "
                "add_memory_and_track with a larger timeout_s (enrichment p95 ~50s)."
            )
        elif state == "FAILED":
            lines.append(
                "guidance: enrichment FAILED (see error above) — safe to retry once; "
                "if it fails again, report the error rather than looping."
            )
    if history:
        lines.append("history:")
        for idx, entry in enumerate(history, 1):
            lines.append(
                f"  [{idx}] state={entry['state']} stage={entry.get('stage') or '(none)'} "
                f"substage={entry.get('substage') or '(none)'} "
                f"progress={float(entry.get('progress') or 0.0):.1f} "
                f"steps={int(entry.get('steps_completed') or 0)}/{int(entry.get('steps_total') or 0)} "
                f"llm_tasks={int(entry.get('llm_tasks_attempt') or 0)}/{int(entry.get('llm_tasks_total') or 0)} "
                f"attempts={entry['attempts']} queue_depth={entry['queue_depth']} "
                f"error={entry['processing_error'] or '(none)'}"
            )
    return "\n".join(lines)


def _format_row_memory(index: int, row: dict[str, object], *, tag: str) -> str:
    name = str(row.get("name") or row.get("uuid") or "(unnamed)")
    scope = str(row.get("scope") or "UNKNOWN")
    memory_type = str(row.get("type") or "UNKNOWN")
    content = row.get("content") or row.get("summary") or "(none)"
    return (
        f"\n[{index}] {name} [{tag}]\n"
        f"    uuid: {row.get('uuid')}\n"
        f"    scope: {scope} | type: {memory_type}\n"
        f"    content: {content}"
    )


#: How long a stuck-backlog count is reused before re-querying. `add_memory` is a hot path
#: and the backlog moves slowly, so a stale-by-a-minute number is worth far more than the
#: latency of counting it on every single write.
_STUCK_COUNT_TTL_S = 60.0

#: (monotonic_deadline, count). Module-level so every writer shares one refresh.
_stuck_count_cache: tuple[float, int] | None = None


async def _standing_unrecallable_count(backend: object) -> int:
    """Return how many episodes are currently FAILED, hence holding content with no entities.

    Recall searches `:Entity`, so a FAILED episode's text is in the graph but unreachable --
    and `add_memory` has already told its caller the write succeeded. Surfacing the count in
    the write's own response is the only signal that reaches EVERY client (Claude, Codex,
    opencode, Qwen); a host hook would only cover whichever harness installs it.

    Counts all FAILED, not just the never-retried ones: from the caller's side "waiting for a
    retry" and "parked forever" are the same state -- not recallable right now. The
    terminal/manual_review split is an operator concern and lives in the queue-health warning.

    Best-effort by construction: a failure to count must never fail the write.
    """
    global _stuck_count_cache

    now = monotonic()
    if _stuck_count_cache is not None and now < _stuck_count_cache[0]:
        return _stuck_count_cache[1]

    try:
        if hasattr(backend, "get_queue_depth"):
            overview = await backend.fetch_memory_overview()
        else:
            overview = backend.graph_adapter.fetch_memory_overview()
        count = int((overview or {}).get("failed_count") or 0)
    except Exception:  # noqa: BLE001 - advisory only; never break an ingest response
        logger.debug("could not read standing unrecallable count", exc_info=True)
        return _stuck_count_cache[1] if _stuck_count_cache is not None else 0

    _stuck_count_cache = (now + _STUCK_COUNT_TTL_S, count)
    return count


async def _queue_summary(backend: object) -> str:
    if hasattr(backend, "get_queue_depth"):
        queue_depth = int(await backend.get_queue_depth())
        active_rows = await backend.list_episode_processing(states=[ProcessingState.ENRICHING], limit=200)
        snapshot = await backend.scheduler_status_snapshot()
    else:
        queue_depth = int(backend.ingest_service.get_queue_depth())
        active_rows = backend.graph_adapter.list_episode_processing(
            processing_states=[ProcessingState.ENRICHING],
            limit=200,
        )
        scheduler = getattr(backend, "scheduler", None)
        snapshot = None
        if scheduler is not None and hasattr(scheduler, "status_snapshot"):
            try:
                snapshot = scheduler.status_snapshot()
            except (AttributeError, TypeError, RuntimeError):
                snapshot = None
    active_enriching = len(active_rows)
    scheduler_state = "running" if snapshot and snapshot.get("running") else "stopped" if snapshot is not None else "unknown"
    summary = (
        f"queue_depth={queue_depth}, "
        f"active_enriching={active_enriching}, "
        f"scheduler={scheduler_state}"
    )

    unrecallable = await _standing_unrecallable_count(backend)
    if unrecallable > 0:
        summary += (
            f"\nWARNING: {unrecallable} earlier "
            f"{'memory is' if unrecallable == 1 else 'memories are'} FAILED and NOT recallable "
            f"-- the text is stored but has no entities, so recall cannot return it. "
            f"This write may end the same way; check get_enrichment_status(episode_id) if it matters."
        )
    return summary


def _format_episode_watch(
    *,
    episode_uuid: str,
    row: dict[str, object] | None,
    history: list[dict[str, object]],
    timed_out: bool,
) -> str:
    lines = [f"episode_id: {episode_uuid}"]
    if row is None:
        lines.append("status: not_found")
        return "\n".join(lines)

    lines.extend(
        [
            f"status: {row.get('processing_state') or 'UNKNOWN'}",
            f"stage: {row.get('processing_stage') or '(none)'}",
            f"substage: {row.get('processing_substage') or '(none)'}",
            f"progress: {float(row.get('processing_progress') or 0.0):.1f}",
            f"timed_out: {timed_out}",
            f"updates: {len(history)}",
            "deltas:",
        ]
    )
    if not history:
        lines.append("  (no updates observed)")
    else:
        for idx, entry in enumerate(history, 1):
            lines.append(
                f"  [{idx}] state={entry['state']} stage={entry.get('stage') or '(none)'} "
                f"substage={entry.get('substage') or '(none)'} "
                f"progress={float(entry.get('progress') or 0.0):.1f} "
                f"steps={int(entry.get('steps_completed') or 0)}/{int(entry.get('steps_total') or 0)} "
                f"llm_tasks={int(entry.get('llm_tasks_attempt') or 0)}/{int(entry.get('llm_tasks_total') or 0)} "
                f"attempts={entry['attempts']} queue_depth={entry['queue_depth']} "
                f"error={entry['processing_error'] or '(none)'} "
                f"llm_active_task={entry.get('llm_active_task') or '(none)'} "
                f"llm_active_endpoint={entry.get('llm_active_endpoint') or '(none)'} "
                f"heartbeat_at={entry.get('heartbeat_at') or '(none)'}"
            )
    return "\n".join(lines)
