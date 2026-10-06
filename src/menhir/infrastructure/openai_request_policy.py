"""Chat-completion request policy for OpenAI reasoning models and the opt-in service tier.

OpenAI's reasoning models (gpt-5*, gpt-6*, o-series) reject ``max_tokens`` (they take
``max_completion_tokens``) and reject any ``temperature`` other than the default. Graphiti's
fork only recognises gpt-5/o-series for the token parameter and always sends ``temperature``,
and Menhir's own chat paths send both, so a gpt-6 model sent to OpenAI directly failed every
call with a 400. Bare-name matching keeps OpenRouter slugs (``openai/gpt-...``) unchanged,
because OpenRouter translates these parameters itself.

``MENHIR_OPENAI_SERVICE_TIER`` (e.g. ``flex``) is opt-in and applies only to reasoning models
on the OpenAI endpoint. Flex requests can queue, so they get a longer per-request timeout
(``MENHIR_OPENAI_FLEX_TIMEOUT_S``, default 900 s) than the 30 s fail-fast default.
"""

from __future__ import annotations

import os
from typing import Any

_REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
SERVICE_TIER_ENV = "MENHIR_OPENAI_SERVICE_TIER"
FLEX_TIMEOUT_ENV = "MENHIR_OPENAI_FLEX_TIMEOUT_S"
DEFAULT_FLEX_TIMEOUT_S = 900.0


def is_openai_reasoning_model(model: str | None) -> bool:
    return (model or "").strip().lower().startswith(_REASONING_PREFIXES)


def _is_openai_endpoint(base_url: Any) -> bool:
    url = str(base_url or "").strip().lower()
    return not url or "api.openai.com" in url


def apply_openai_request_policy(kwargs: dict[str, Any], *, base_url: Any) -> dict[str, Any]:
    """Return a copy of chat-completion ``kwargs`` adjusted for the model and endpoint."""
    out = dict(kwargs)
    if not is_openai_reasoning_model(out.get("model")):
        return out
    if "max_tokens" in out:
        tokens = out.pop("max_tokens")
        if tokens is not None:
            out.setdefault("max_completion_tokens", tokens)
    out.pop("temperature", None)
    tier = os.getenv(SERVICE_TIER_ENV, "").strip().lower()
    if tier and _is_openai_endpoint(base_url) and "service_tier" not in out:
        out["service_tier"] = tier
        if tier == "flex" and "timeout" not in out:
            out["timeout"] = float(os.getenv(FLEX_TIMEOUT_ENV) or DEFAULT_FLEX_TIMEOUT_S)
    return out
