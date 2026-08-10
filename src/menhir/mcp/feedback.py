"""Self-reported recall usefulness feedback (R8 agent_inference grade).

An LLM rating "that recall helped" is the weakest evidence kind under the
retrieval-control rails: it is an *operational* quality signal for the usage
dashboard, and it deliberately never feeds memory heat, promotion, or
reinforcement (those stay gated on external productive outcomes — see
``domain/self_reinforcement.py``).

Flow:
  1. A recall tool calls :func:`attach_recall_receipt` before returning; this mints
     a short ``recall_id`` token, registers a ratable receipt, and stamps the token
     into the response payload.
  2. The agent later calls the ``rate_recall`` tool with a score (and optionally the
     token). :data:`SCORE_SCALE` maps the score label to a numeric value.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger(__name__)

# Ordinal usefulness scale. ``unused`` carries a null value: it counts toward
# rating coverage but is excluded from the average usefulness score.
SCORE_SCALE: dict[str, float | None] = {
    "useful": 1.0,
    "partial": 0.5,
    "noise": 0.0,
    "unused": None,
}

# Recall operations that mint a ratable receipt.
RATABLE_OPERATIONS: frozenset[str] = frozenset(
    {"recall_memories", "recall_context_memories", "read_flagged_memories", "build_context"}
)


def is_valid_score(label: str) -> bool:
    return label in SCORE_SCALE


def score_value(label: str) -> float | None:
    return SCORE_SCALE.get(label)


def _mint_token() -> str:
    # 64 bits of entropy: receipts accumulate across every recall, so a shorter
    # token would hit birthday collisions and INSERT OR IGNORE would then silently
    # point a response's recall_id at an older, unrelated receipt.
    return "r" + secrets.token_hex(8)


def mint_recall_receipt(operation: str) -> str | None:
    """Register a ratable receipt for this recall and return its ``recall_id``.

    Best-effort and side-effect isolated: any failure (no session, telemetry
    error) returns ``None`` so feedback plumbing can never break a recall.
    """
    try:
        from menhir.mcp.service_access import get_request_session
        from menhir.mcp.telemetry import telemetry_store

        session = get_request_session()
        session_id = session.session_id if session and session.session_id else ""
        client_id = session.client_id if session and session.client_id else ""
        token = _mint_token()
        telemetry_store.record_recall_receipt(
            token=token,
            operation=operation,
            client_id=client_id,
            session_id=session_id,
        )
        return token
    except Exception:  # noqa: BLE001 - feedback must never break recall
        logger.debug("mint_recall_receipt skipped for %s", operation, exc_info=True)
        return None


def attach_recall_receipt(operation: str, payload: dict) -> None:
    """Mint a recall_id, register a ratable receipt, and stamp it into ``payload``."""
    token = mint_recall_receipt(operation)
    if token:
        payload["recall_id"] = token


def recall_receipt_footer(operation: str) -> str:
    """Return a text footer carrying the recall_id for prose recall tools.

    Empty string when no receipt could be minted (best-effort).
    """
    token = mint_recall_receipt(operation)
    if not token:
        return ""
    return f"\nrecall_id: {token} (rate with rate_recall)"
