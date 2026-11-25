"""Policy for AUTOMATIC merges and deletions: off | propose | on, one switch each.

The process environment is the single authority. Every call reads MemorySettings.from_env() at
call time, with no cache and no override, so no construction path can bypass it. Any failure to
read the setting fails closed to "off".

Scope: only automatic mutation of memories. Operator-requested erasure, delete_entity, candidate
rejection, namespace delete, unmerge and derived-data rebuilds do not consult these switches.

A proposal is a ``pending_actions`` row (attempts 0) holding uuids, numbers, a reason code and a
trigger. Never names, content or summaries. Recording is best effort: a failure is logged and
swallowed and can never lead to a mutation or abort the caller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

AUTO_MERGE_OFF = "AUTO_MERGE_OFF"
AUTO_MERGE_PROPOSED = "AUTO_MERGE_PROPOSED"
AUTO_DELETE_OFF = "AUTO_DELETE_OFF"
AUTO_DELETE_PROPOSED = "AUTO_DELETE_PROPOSED"

MERGE_PROPOSAL = "merge_proposal"
DELETE_PROPOSAL = "delete_proposal"

_VALID = ("off", "propose", "on")


def _mode(attr: str) -> str:
    try:
        from menhir.config import MemorySettings

        value = str(getattr(MemorySettings.from_env(), attr)).strip().lower()
        if value in _VALID:
            return value
        logger.warning("%s has unexpected value %r; treating as off", attr, value)
    except Exception:  # noqa: BLE001 -- fail closed on any read failure
        logger.warning("could not read %s; treating as off", attr, exc_info=True)
    return "off"


def auto_merge_mode() -> str:
    """Current automatic-merge mode: "off", "propose" or "on" (fails closed to "off")."""
    return _mode("auto_merge_mode")


def auto_delete_mode() -> str:
    """Current automatic-delete mode: "off", "propose" or "on" (fails closed to "off")."""
    return _mode("auto_delete_mode")


def proposal_context(reason: str, trigger: str, **numbers: Any) -> str:
    """JSON context for a proposal. Callers pass uuids and numbers only."""
    return json.dumps({"reason": reason, "trigger": trigger, **numbers}, sort_keys=True)


def record_proposals(action: str, rows: Iterable[tuple[str, str | None, str]]) -> int:
    """Best-effort batch record of proposals. Returns rows submitted, 0 on any failure."""
    batch = [(str(n), (str(s) if s else None), c) for n, s, c in rows if n]
    if not batch:
        return 0
    try:
        from menhir.infrastructure.pending_actions import PendingActionStore

        return PendingActionStore().record_proposals(action, batch)
    except Exception:  # noqa: BLE001 -- a proposal failure must never mutate or abort
        logger.warning("could not record %d %s proposals", len(batch), action, exc_info=True)
        return 0


def complete_proposals(action: str, node_uuids: Iterable[str]) -> None:
    """Best-effort: drop proposals for nodes that were really merged or deleted."""
    uuids = [str(u) for u in node_uuids if u]
    if not uuids:
        return
    try:
        from menhir.infrastructure.pending_actions import PendingActionStore

        store = PendingActionStore()
        for node_uuid in uuids:
            store.complete(node_uuid, action)
    except Exception:  # noqa: BLE001
        logger.warning("could not complete %s proposals", action, exc_info=True)


__all__ = [
    "AUTO_DELETE_OFF", "AUTO_DELETE_PROPOSED", "AUTO_MERGE_OFF", "AUTO_MERGE_PROPOSED",
    "DELETE_PROPOSAL", "MERGE_PROPOSAL", "auto_delete_mode", "auto_merge_mode",
    "complete_proposals", "proposal_context", "record_proposals",
]
