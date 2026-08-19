"""Admission gate — verifying user-tier claims against turn evidence grounding.

When a caller claims `source="user"` or `source="manual"`, this gate verifies the claim is
grounded in actual user input before admitting it to the high-trust (1.0) tier. Any failure
downgrades the claim to `source="agent_inference"` (0.5 tier).

Design pattern from `services/perception.py::_stated_value_grounded`: deterministic,
LLM-free, fail-closed (precision-first — when in doubt, deny).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.domain.truth.assertion_spans import claim_is_grounded, normalize_claim_text


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


#: One normalization authority, shared with span extraction so the two can never compare
#: differently-shaped strings.
_normalize_text = normalize_claim_text


def _text_grounded(claimed: str, source_span: str) -> bool:
    """True if the source span ASSERTS the claimed text (CF-17).

    Delegates to ``assertion_spans.claim_is_grounded``: equality against the whole evidence text,
    or equality against one extracted single-assertion span. See that module for the safety
    argument and for what is deliberately denied.

    This function previously granted on a contiguous substring OR on >= 50% of retained claimed
    tokens appearing anywhere in the source, and its docstring called that "conservative" and
    "precision-first". It was neither, which is a large part of why the defect survived review:
    every single-word contradiction of a multi-token claim grounded at the apex tier, and so did
    quotation, attribution and conditionals. Both branches are gone.
    """
    return claim_is_grounded(claimed, source_span)


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
