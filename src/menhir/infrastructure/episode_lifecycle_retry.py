"""Episode retry classification: the context-window marker lists and the transient retry cap.

Moved verbatim from ``episode_lifecycle`` (which re-exports the public names) so the error
classifiers live beside the constants they read and the retry budget they gate.
"""

from __future__ import annotations

#: Context-window errors an OPERATOR can clear without the payload changing -- local
#: llama.cpp/server phrasing, where the server was started with too small an n_ctx. Reloading
#: the model with a larger window makes the SAME episode succeed, which is why these alone earn
#: the extended retry budget (`get_context_window_retry_attempts`).
_RECOVERABLE_CONTEXT_WINDOW_MARKERS = (
    "cannot truncate prompt",
    "n_keep",
    "n_ctx",
    "available context window",
    "exceeds the available context window",
    "exceeds the available context",
)

#: A hosted provider refusing the request against a fixed model limit. Also a context-window
#: error, but NOT operator-clearable: the ceiling is not ours to raise, and re-sending the same
#: payload fails identically. These must classify as terminal WITHOUT earning extra retries.
_PROVIDER_CONTEXT_LIMIT_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "reduce the length of the messages",
)

#: Retries for TRANSIENT conditions (provider outage, circuit-open, backpressure) do not consume
#: the genuine-failure budget (#79/#70): `processing_attempts` is refunded when the requeue is
#: transient, so an outage that ends can never have parked an episode on its own. Termination
#: instead rides this separate, much larger counter — a permanently dead provider must still stop
#: consuming claim cycles eventually.
TRANSIENT_RETRY_CAP = 20


def is_recoverable_context_window_error(error: object | None) -> bool:
    """Return True for a context-window error a config change could clear.

    Gates the EXTENDED retry budget. Deliberately narrower than
    `is_context_window_error_text`: retrying an unchanged payload against a hosted
    provider's fixed limit burns attempts and cannot ever succeed.
    """
    if error is None:
        return False
    error_text = str(error).lower()
    return any(marker in error_text for marker in _RECOVERABLE_CONTEXT_WINDOW_MARKERS)


def is_context_window_error_text(error: object | None) -> bool:
    """Return True when the error text indicates a context-window mismatch, from any source.

    Covers both local server misconfiguration and a hosted provider's hard limit. Used for
    CLASSIFICATION (these are terminal). For retry-budget decisions use
    `is_recoverable_context_window_error` instead -- only the local variant is retryable.

    The provider markers were missing until 2026-07-28, so an OpenAI 400
    `context_length_exceeded` fell through every list to the `manual_review` default. That
    still parked the episode correctly, so no retry behaviour depended on the gap; the
    classification was simply less accurate than it looked.
    """
    if error is None:
        return False
    error_text = str(error).lower()
    return is_recoverable_context_window_error(error) or any(
        marker in error_text for marker in _PROVIDER_CONTEXT_LIMIT_MARKERS
    )
