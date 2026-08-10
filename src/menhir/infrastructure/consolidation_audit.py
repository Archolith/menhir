"""Toggleable, replayable audit trail for the consolidation job.

A thin, **behavior-neutral** wrapper over the telemetry ``lifecycle_events`` store. When the
audit toggle is ON, the consolidation pipeline emits one structured event per decision point
(perception, binding / pending-repair, scalar fold outcome, View write / retire / supersede,
reconcile, counters, merges, lifecycle promote / demote / delete). When OFF, :func:`audit` is a
cheap no-op and nothing is written. Emission is best-effort and can NEVER raise into the caller,
so toggling this changes only what is recorded, never what consolidation does.

Every event is stamped ``component="consolidation_audit"`` and carries the consolidation
``pass_id`` in the row's ``episode_uuid`` column (indexed) so a whole pass replays with one
indexed lookup; richer correlation keys (namespace, subject_uuid, slot, values, reasons) ride in
``details``. Read it back with :func:`read_pass` / :func:`read_recent`, the ``get_consolidation_audit``
ops tool, or ``scripts/inspect_consolidation_audit.py`` against a run's telemetry DB.

The toggle is sourced from (in order): an explicit :func:`set_enabled` call (bootstrap wires this
from ``MemorySettings.personal_memory_consolidation_audit_enabled``) and, as a fallback for scripts
and tests that never boot the full service, the ``MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED``
environment variable read at import.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import uuid
from typing import Any, Optional, Sequence

from menhir.infrastructure.telemetry import record_lifecycle_event, telemetry_store

logger = logging.getLogger(__name__)

COMPONENT = "consolidation_audit"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_default() -> bool:
    return os.getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED", "").strip().lower() in _TRUTHY


_lock = threading.Lock()
_enabled = _env_default()

# The active consolidation pass id, so nested emit sites correlate WITHOUT threading pass_id through
# every (frozen) function. Set at pass entry via :func:`begin_pass`; propagates across ``await`` within
# the same asyncio task. Emit sites may still pass pass_id explicitly to override.
_current_pass: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "consolidation_pass_id", default=None
)


def set_enabled(value: bool) -> None:
    """Turn the consolidation audit trail on or off (wired from settings at bootstrap)."""
    global _enabled
    with _lock:
        _enabled = bool(value)


def is_enabled() -> bool:
    """True when audit events are being recorded."""
    return _enabled


def new_pass_id() -> str:
    """Mint a correlation id for one consolidation pass; pass it to every :func:`audit` in that pass."""
    return f"cap-{uuid.uuid4().hex[:16]}"


def begin_pass(pass_id: Optional[str] = None) -> str:
    """Mark the start of a consolidation pass and make its id the ambient correlation id.

    Returns the pass id (minted if not supplied). Cheap and safe to call even when the toggle is off.
    """
    pid = pass_id or new_pass_id()
    _current_pass.set(pid)
    return pid


def current_pass_id() -> Optional[str]:
    """The ambient consolidation pass id set by :func:`begin_pass`, or None."""
    return _current_pass.get()


def audit(
    event: str,
    state: str,
    *,
    pass_id: Optional[str] = None,
    namespace: Optional[str] = None,
    subject_uuid: Optional[str] = None,
    slot: Optional[Sequence[Any]] = None,
    episode_uuid: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Record one consolidation decision. No-op (and cheap) when the toggle is OFF.

    ``event`` names the decision point (e.g. ``"fold"``, ``"view_write"``, ``"reconcile_retire"``),
    ``state`` its outcome (e.g. ``"anchor"``, ``"ambiguous_anchor"``, ``"created"``, ``"stale_skipped"``,
    ``"retired"``). Correlation keys and any decision inputs/outputs go in ``details``. This function
    is defensive: it must never raise into the consolidation path.
    """
    if not _enabled:
        return
    try:
        if pass_id is None:
            pass_id = _current_pass.get()
        payload: dict[str, Any] = {}
        if namespace is not None:
            payload["namespace"] = namespace
        if subject_uuid is not None:
            payload["subject_uuid"] = subject_uuid
        if slot is not None:
            payload["slot"] = list(slot) if isinstance(slot, (tuple, list)) else slot
        if episode_uuid is not None:
            payload["episode_uuid"] = episode_uuid
        if details:
            payload.update(details)
        # The pass_id rides in the row's episode_uuid column (indexed) so a whole pass replays with
        # one lookup; the *semantic* episode, when the event is about one, stays in details above.
        record_lifecycle_event(
            component=COMPONENT,
            event=str(event),
            state=str(state),
            episode_uuid=pass_id,
            details=payload or None,
        )
    except Exception as exc:  # pragma: no cover - audit must never break consolidation
        logger.debug("consolidation_audit emit failed (%s.%s): %s", event, state, exc)


def read_pass(pass_id: str, *, limit: int = 200, store: Any = telemetry_store) -> list[dict[str, Any]]:
    """Return every audit event for one consolidation pass, oldest-first (replay order)."""
    rows = store.fetch_recent_lifecycle_events(
        limit=max(1, min(limit, 200)), component=COMPONENT, episode_uuid=pass_id
    )
    return list(reversed(rows))


def read_recent(*, limit: int = 200, store: Any = telemetry_store) -> list[dict[str, Any]]:
    """Return recent consolidation audit events across all passes, oldest-first."""
    rows = store.fetch_recent_lifecycle_events(limit=max(1, min(limit, 200)), component=COMPONENT)
    return list(reversed(rows))
