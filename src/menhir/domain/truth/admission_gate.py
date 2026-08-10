"""Admission gate — verifying user-tier claims against turn evidence grounding.

When a caller claims `source="user"` or `source="manual"`, this gate verifies the claim is
grounded in actual user input before admitting it to the high-trust (1.0) tier. Any failure
downgrades the claim to `source="agent_inference"` (0.5 tier).

Design pattern from `services/perception.py::_stated_value_grounded`: deterministic,
LLM-free, fail-closed (precision-first — when in doubt, deny).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdmissionVerdict:
    """Gate verdict on a user-tier claim."""

    granted: bool
    """True if the claim is admitted at user tier; False if downgraded."""

    effective_source: str
    """The (possibly downgraded) source string to actually persist."""

    reason: str
    """Human-readable reason for the verdict."""

    turn_evidence_uuid: str | None
    """The UUID of the cited turn evidence, if any."""


def _normalize_text(text: str | None) -> str:
    """Lowercase and collapse whitespace for comparison."""
    if not text:
        return ""
    return " ".join(str(text).lower().split())


def _text_grounded(claimed: str, source_span: str) -> bool:
    """True if claimed text is meaningfully present in source span.

    Conservative substring/token-overlap check:
    - Normalize both to lowercase, collapsed whitespace.
    - Require a contiguous substring of claimed text, or
    - A high fraction (>= 50%) of significant tokens from claimed appear in source.

    This is deliberately conservative: a loose semantic match (e.g. "bought a bike" in
    "I purchased a bicycle") may not ground here — precision-first, fall back to agent_inference
    if the match is uncertain.
    """
    norm_claimed = _normalize_text(claimed)
    norm_source = _normalize_text(source_span)

    if not norm_claimed or not norm_source:
        return False

    # Exact substring match (most reliable).
    if norm_claimed in norm_source:
        return True

    # Token overlap: split into non-empty words, require >= 50% of claimed tokens in source.
    claimed_tokens = [t for t in re.split(r"\W+", norm_claimed) if t and len(t) > 2]
    if not claimed_tokens:
        # Claimed is all short/empty tokens (e.g. "a b c") — require exact substring.
        return norm_claimed in norm_source

    source_tokens = set(t for t in re.split(r"\W+", norm_source) if t and len(t) > 2)
    overlap = sum(1 for t in claimed_tokens if t in source_tokens)
    return overlap >= len(claimed_tokens) * 0.5


def evaluate_user_tier_claim(
    *,
    requested_source: str,
    turn_evidence: dict[str, Any] | None,
    claimed_text: str,
    session_id: str | None,
    namespace: str | None,
) -> AdmissionVerdict:
    """Evaluate whether a user-tier claim (source='user' or 'manual') is grounded.

    Args:
        requested_source: The caller-declared source string (e.g. "user", "manual").
        turn_evidence: Dict with {turn_id, role, declarant, text, session_id, namespace}
                       from TurnEvidenceRepository.fetch_by_uuid(), or None if not provided.
        claimed_text: The memory text the caller wants to store.
        session_id: The session_id of the ingestion (for session/namespace matching).
        namespace: The namespace of the ingestion (for session/namespace matching).

    Returns:
        AdmissionVerdict with granted, effective_source, reason, turn_evidence_uuid.

    Logic:
    - Only applies when requested_source.strip().lower() in ("user", "manual").
    - Any other source passes through unchanged (granted=True, effective_source=unchanged).
    - Deny (downgrade to agent_inference) when:
      * turn_evidence is None
      * turn_evidence["role"] != "user"
      * session/namespace mismatch (when both are non-empty)
      * claimed_text is not grounded in turn_evidence["text"]
    - On deny: effective_source="agent_inference" (0.5 tier, same as regular agent_inference).
    - On grant: effective_source=requested_source (1.0 tier for user/manual).
    """
    source_lower = requested_source.strip().lower()

    # Gate only applies to apex-tier claims.
    if source_lower not in ("user", "manual"):
        return AdmissionVerdict(
            granted=True,
            effective_source=requested_source,
            reason="passthrough (not user/manual)",
            turn_evidence_uuid=None,
        )

    # Missing evidence -> deny (fail closed).
    if turn_evidence is None:
        return AdmissionVerdict(
            granted=False,
            effective_source="agent_inference",
            reason="no turn_evidence_uuid provided",
            turn_evidence_uuid=None,
        )

    turn_id = str(turn_evidence.get("turn_id") or "")
    role = str(turn_evidence.get("role") or "")
    text = str(turn_evidence.get("text") or "")

    # Role mismatch -> deny.
    if role != "user":
        return AdmissionVerdict(
            granted=False,
            effective_source="agent_inference",
            reason=f"turn role is {role!r}, not 'user'",
            turn_evidence_uuid=turn_id or None,
        )

    # Session/namespace mismatch (tolerant of None on both sides, strict on explicit values).
    evidence_session = str(turn_evidence.get("session_id") or "") if turn_evidence.get("session_id") else None
    evidence_namespace = str(turn_evidence.get("namespace") or "") if turn_evidence.get("namespace") else None
    session_check = session_id or ""
    namespace_check = namespace or ""

    # Only deny if one side is explicit and they differ.
    if evidence_session is not None and session_check and evidence_session != session_check:
        return AdmissionVerdict(
            granted=False,
            effective_source="agent_inference",
            reason=f"session mismatch: turn={evidence_session!r}, ingestion={session_check!r}",
            turn_evidence_uuid=turn_id or None,
        )

    if evidence_namespace is not None and namespace_check and evidence_namespace != namespace_check:
        return AdmissionVerdict(
            granted=False,
            effective_source="agent_inference",
            reason=f"namespace mismatch: turn={evidence_namespace!r}, ingestion={namespace_check!r}",
            turn_evidence_uuid=turn_id or None,
        )

    # Grounding check: claimed text must be present in the turn's text.
    if not _text_grounded(claimed_text, text):
        return AdmissionVerdict(
            granted=False,
            effective_source="agent_inference",
            reason="claimed text not grounded in turn evidence",
            turn_evidence_uuid=turn_id or None,
        )

    # All checks passed -> grant at user tier.
    return AdmissionVerdict(
        granted=True,
        effective_source=requested_source,
        reason="grounded",
        turn_evidence_uuid=turn_id or None,
    )
