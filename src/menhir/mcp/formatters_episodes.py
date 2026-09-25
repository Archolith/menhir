"""MCP formatter episode helpers — status polling and episode output rendering."""

from __future__ import annotations

import asyncio
from time import perf_counter

from menhir.domain.models import ProcessingState

from menhir.mcp.formatters_primitives import _coerce_iso


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

def _episode_status_guidance(*, state: str | None, timed_out: bool) -> str:
    """Explain an observation without promising completion or recommending another write."""
    normalized = (state or "").upper()
    if state is None:
        return (
            "guidance: no processing status was found in the current authorized scope. "
            "This does not establish that a write failed or that its source text was deleted. "
            "Check the existing episode_id and authorized namespace; do not submit it again "
            "based only on this observation."
        )
    if normalized == "READY":
        return (
            "guidance: READY is the observed enrichment state, not a guarantee that a "
            "recall query will return this memory. Verify retrieval separately."
        )
    if normalized == "FAILED":
        return (
            "guidance: FAILED is the observed enrichment state, not proof that the source "
            "text was deleted or every retry is exhausted. Inspect this episode's error "
            "and use only authorized retry/repair on the existing episode, not a new write."
        )

    if normalized in {"PENDING", "QUEUED"}:
        observation = "the recorded state indicates queued work; completion is not guaranteed. "
    elif normalized == "ENRICHING":
        observation = "ENRICHING records a claimed job, not proof its worker is alive. "
    else:
        observation = "no recognized terminal enrichment state was observed. "
    timeout_note = (
        "the tracking wait expired; this does not cancel the queued write. " if timed_out else ""
    )
    return (
        f"guidance: {timeout_note}{observation}"
        "Do not submit this memory again. If available to this client, continue with "
        "get_enrichment_status(episode_uuid=<episode_id>, wait=True) or watch_enrichment "
        "on the same episode and namespace. Otherwise report that completion is unverified; "
        "do not broaden permissions."
    )


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
        lines.append(_episode_status_guidance(state=None, timed_out=timed_out))
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
            # The Graphiti-minted node that carries this write's MENTIONS edges (#92). The
            # episode_id above is Menhir's receipt; get_provenance names both, and this is
            # the value that matches its `uuid`. "(pending)" until enrichment completes.
            f"enriched_episode_uuid: {row.get('resolved_episode_uuid') or '(pending)'}",
            f"timed_out: {timed_out}",
            f"updates: {len(history)}",
        ]
    )
    lines.append(
        _episode_status_guidance(
            state=str(row.get("processing_state") or "UNKNOWN"), timed_out=timed_out,
        )
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
        lines.append(_episode_status_guidance(state=None, timed_out=timed_out))
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
    lines.append(
        _episode_status_guidance(
            state=str(row.get("processing_state") or "UNKNOWN"), timed_out=timed_out,
        )
    )
    return "\n".join(lines)
