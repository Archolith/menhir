"""Chat-completion request policy: a thin facade over the model profile's request shaping.

OpenAI's reasoning models (gpt-5*, gpt-6*, o-series) reject ``max_tokens`` and any non-default
``temperature``; ``MENHIR_OPENAI_SERVICE_TIER`` (e.g. ``flex``) is opt-in for them on the OpenAI
endpoint. The rules live on the profiles in ``model_profiles``; this module keeps the call-site API.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.model_profiles import (
    DEFAULT_FLEX_TIMEOUT_S,
    FLEX_TIMEOUT_ENV,
    SERVICE_TIER_ENV,
    OpenAIReasoningProfile,
    resolve_model_profile,
)

__all__ = [
    "DEFAULT_FLEX_TIMEOUT_S",
    "FLEX_TIMEOUT_ENV",
    "SERVICE_TIER_ENV",
    "apply_openai_request_policy",
    "is_openai_reasoning_model",
]


def is_openai_reasoning_model(model: str | None) -> bool:
    return isinstance(resolve_model_profile(model), OpenAIReasoningProfile)


def apply_openai_request_policy(kwargs: dict[str, Any], *, base_url: Any) -> dict[str, Any]:
    """Return a copy of chat-completion ``kwargs`` adjusted for the model and endpoint."""
    return resolve_model_profile(kwargs.get("model")).shape_request(kwargs, base_url=base_url)
