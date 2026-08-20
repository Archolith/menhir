"""Shared enrichment failure classification helpers."""

from __future__ import annotations

from menhir.infrastructure.episode_repository import is_context_window_error_text

_MANUAL_REVIEW_TYPE_NAMES = {
    "jsondecodeerror",
    "validationerror",
}
_MANUAL_REVIEW_MARKERS = (
    "jsondecodeerror",
    "validationerror",
    "expecting value",
    "malformed json response",
    "empty json response",
    "extra data",
    "unterminated string",
    "invalid control character",
    "invalid \\escape",
    "invalid json",
    "validation error",
    "field required",
    "input should be",
)
_TERMINAL_ERROR_MARKERS = (
    "zero_extraction",
    "episode_preflight_too_large",
)
_RETRYABLE_ERROR_MARKERS = (
    "combined_extraction_collapsed",
    "failed to load model",
    "model unloaded",
    "model is unloaded",
    "operation canceled",
    "graphiti unavailable",
    "neo.clienterror.statement.entitynotfound",
    "timeout",
    "timed out",
    "connection",
    "refused",
    "503",
    "429",
)
#: A budget refusal is a POLICY decision, not a provider fault. It reaches this classifier only
#: on a surface where the budget actually binds (graphiti's instrumented client today; every
#: surface once CF-234's enforcing half lands).
#:
#: It must never be graded `retryable`. A per-job overrun is deterministic -- the same episode
#: re-extracts roughly the same entities and re-runs the same judge fan-out -- so retrying it
#: spends the budget again to reach the same refusal. That is the one classification that turns
#: a cost control into a cost amplifier.
#:
#: `manual_review` rather than `terminal` because an operator CAN make this episode succeed, by
#: raising the cap: it is a policy limit, not a structural impossibility. Both park the episode
#: permanently (`_NEVER_RETRIED_CLASSIFICATIONS`), so this changes no behaviour today -- the
#: refusal already landed in `manual_review` by falling through every marker list. What changes
#: is that it now lands there by DECISION, and is greppable as its own cause.
_BUDGET_REFUSAL_MARKERS = (
    "exceeded its per-job llm budget",
    "exhausted its llm call budget",
)
_BUDGET_REFUSAL_TYPE_NAMES = {"llmbudgetexceeded"}

_UNKNOWN_REMOTE_TIMEOUT_MARKERS = (
    "graphiti add_episode timed out",
    "remote completion status unknown",
)


def _normalize_error_text(error: object | None) -> str:
    return str(error or "").strip().lower()


def _normalize_error_type(error: object | None, error_type: object | None) -> str:
    if error_type is not None:
        return str(error_type).strip().lower()
    if error is None:
        return ""
    return type(error).__name__.strip().lower()


def is_graphiti_output_parse_error(error: object | None, *, error_type: object | None = None) -> bool:
    """Return True when Graphiti/model output failed JSON or schema parsing."""

    normalized_type = _normalize_error_type(error, error_type)
    if normalized_type in _MANUAL_REVIEW_TYPE_NAMES:
        return True

    error_text = _normalize_error_text(error)
    return any(marker in error_text for marker in _MANUAL_REVIEW_MARKERS)


def is_budget_refusal(error: object | None, *, error_type: object | None = None) -> bool:
    """Whether this failure is an LLM budget refusal rather than a provider fault.

    Matched on the exception TYPE first and the message only as a fallback: the type is exact,
    and the message is the part a future edit is most likely to reword.
    """
    if _normalize_error_type(error, error_type) in _BUDGET_REFUSAL_TYPE_NAMES:
        return True
    text = _normalize_error_text(error)
    return any(marker in text for marker in _BUDGET_REFUSAL_MARKERS)


def classify_enrichment_failure(error: object | None, *, error_type: object | None = None) -> str:
    """Classify an enrichment failure as retryable, terminal, or manual review."""

    if error is None:
        return "retryable"

    # Checked BEFORE every marker list. A budget message that happened to contain a retryable
    # marker would otherwise be requeued, and requeueing a deterministic overrun spends the
    # budget again to reach the same refusal.
    if is_budget_refusal(error, error_type=error_type):
        return "manual_review"

    if is_context_window_error_text(error):
        return "terminal"

    error_text = _normalize_error_text(error)
    if any(marker in error_text for marker in _UNKNOWN_REMOTE_TIMEOUT_MARKERS):
        return "manual_review"

    if is_graphiti_output_parse_error(error, error_type=error_type):
        return "manual_review"

    if any(marker in error_text for marker in _TERMINAL_ERROR_MARKERS):
        return "terminal"

    if any(marker in error_text for marker in _RETRYABLE_ERROR_MARKERS):
        return "retryable"

    return "manual_review"
