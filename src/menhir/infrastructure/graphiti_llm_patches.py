"""Graphiti OpenAI-compatible response parsing and retry patches."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import importlib
import importlib.metadata
import json
import logging
import os
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import urlsplit

from menhir.infrastructure.graphiti_helpers import (
    _build_graphiti_failure_details,
    _describe_openai_client_base_url,
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
    check_graphiti_version,
)

logger = logging.getLogger(__name__)

# Version guard - run once at import, matching the other patch modules (CF-87).
check_graphiti_version()

# ---------------------------------------------------------------------------
# Graphiti OpenAI-generic-client patch
# ---------------------------------------------------------------------------


#: Estimated-token ceiling for the ASSEMBLED request. Set at patch time from
#: MemorySettings.graphiti_request_max_estimated_tokens. 0 disables the check.
_MAX_REQUEST_ESTIMATED_TOKENS = 100000

#: Conservative fallback for code-heavy Graphiti prompts.  The old prose-oriented
#: value of 4 materially undercounted the failed code-heavy requests.
_CHARS_PER_TOKEN = 3


class GraphitiRequestTooLargeError(RuntimeError):
    """The assembled extraction request exceeds the configured token ceiling.

    Raised BEFORE the API call so the failure names its own cause. Without it the
    provider returns a bare 400 that gets recorded as the episode's
    processing_error, and the operator sees "maximum context length" with no
    indication of which part of the payload was responsible.
    """


def _describe_request_size(messages: list[dict[str, str]]) -> tuple[int, list[tuple[int, str, int]]]:
    """Return (total_chars, per-message [(index, role, chars)] sorted largest first)."""
    per_message = [
        (index, str(message.get("role", "?")), len(str(message.get("content", ""))))
        for index, message in enumerate(messages)
    ]
    return sum(item[2] for item in per_message), sorted(per_message, key=lambda item: -item[2])


#: Endpoint -> (expires_at_monotonic, derived_ceiling_or_None). Keyed on the endpoint because the
#: wake sequence rotates base URLs per task, so one process legitimately talks to several.
_derived_ceilings: dict[str, tuple[float, int | None]] = {}

#: Re-probe this often. The ceiling is a fact about whichever model the scheduler currently has
#: loaded, and the scheduler can swap models under us -- so a value derived once at startup is a
#: fact that outlives its subject. Ten minutes bounds how long a stale ceiling can persist.
_DERIVED_CEILING_TTL_S = 600.0

#: Fraction of the model's window held back for the response. The ceiling bounds the REQUEST, but
#: the window has to hold request + completion, so spending all of it guarantees a rejection.
_CONTEXT_RESPONSE_RESERVE = 0.25


def _is_loopback(endpoint: str | None) -> bool:
    """True when `endpoint` addresses this machine.

    The probe below only runs against loopback. A remote OpenAI-compatible provider has no
    equivalent of llama.cpp's `/props`, so probing one would mean an unsolicited GET against a
    third-party API on every cache miss, to learn nothing.
    """
    if not endpoint:
        return False
    try:
        host = (urlsplit(endpoint).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _n_ctx_from_props(payload: object) -> int | None:
    """Pull the context size out of a llama.cpp `/props` body.

    The key moved between llama.cpp versions -- newer builds nest it under
    `default_generation_settings`, older ones put `n_ctx` at the top level, and some report
    `ctx_size`. Reading only the current spelling would silently return None against a build one
    version away, which is indistinguishable from "no scheduler" and would disable the derivation
    without saying so.
    """
    if not isinstance(payload, dict):
        return None
    candidates: list[object] = [
        payload.get("n_ctx"),
        payload.get("ctx_size"),
    ]
    nested = payload.get("default_generation_settings")
    if isinstance(nested, dict):
        candidates.extend([nested.get("n_ctx"), nested.get("ctx_size")])
    for value in candidates:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


async def _probe_endpoint_context_window(endpoint: str) -> int | None:
    """Ask a llama.cpp server (``GET /props`` at the endpoint root) for its loaded context window.

    Any other OpenAI-compatible server answers 404 or with a body that has no ``n_ctx``; both
    resolve to ``None`` ("cannot derive"), never to a ceiling.
    """
    parts = urlsplit(endpoint)
    root = f"{parts.scheme}://{parts.netloc}"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{root}/props")
        response.raise_for_status()
        return _n_ctx_from_props(response.json())
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot derive", never "no ceiling"
        logger.debug("Context-window probe failed for %s: %s", root, exc)
        return None


async def resolve_request_ceiling(endpoint: str | None) -> int:
    """Return the estimated-token ceiling to enforce for `endpoint`.

    CF-181 filed the dedupe batch-split as costing up to 2N-1 LLM round trips. The mechanism that
    is supposed to make those splits free already exists -- `_enforce_request_size` rejects an
    oversized payload BEFORE the API call -- but its ceiling was a fixed 100,000 with no
    relationship to whatever model is deployed. Measured on this host: the llama server runs at
    `LLAMA_CONTEXT_SIZE=32768`, so the ceiling sat at 3x the real window and could never fire
    first. Every split attempt therefore cost a real round trip to a local model, which is exactly
    the cost the finding is about.

    TWO SAFETY RULES, and both are deliberate:

    * **Derivation may only LOWER the configured ceiling, never raise it.** The setting is also a
      cost bound -- a 100,000-token request to a metered provider is expensive whether or not it
      fits -- so a model with a huge window must not be allowed to widen it.
    * **A configured ceiling of 0 means the check is disabled, and derivation must not re-enable
      it.** Turning a deliberate opt-out back on because a probe succeeded would be the mechanism
      overriding the operator.

    Any failure returns the configured value. The derivation is an improvement when it works and
    never a regression when it does not.
    """
    configured = _MAX_REQUEST_ESTIMATED_TOKENS
    if not configured or not _is_loopback(endpoint):
        return configured

    assert endpoint is not None
    now = monotonic()
    cached = _derived_ceilings.get(endpoint)
    if cached is not None and cached[0] > now:
        derived = cached[1]
    else:
        window = await _probe_endpoint_context_window(endpoint)
        derived = (
            None if window is None else max(1, int(window * (1.0 - _CONTEXT_RESPONSE_RESERVE)))
        )
        _derived_ceilings[endpoint] = (now + _DERIVED_CEILING_TTL_S, derived)
        if derived is not None and derived < configured:
            logger.info(
                "Graphiti request ceiling derived from endpoint: %s tokens "
                "(model window %s, configured ceiling %s) endpoint=%s",
                f"{derived:,}",
                f"{window:,}",
                f"{configured:,}",
                endpoint,
            )

    return configured if derived is None else min(configured, derived)


def _enforce_request_size(
    messages: list[dict[str, str]], model: str, endpoint: str | None,
    ceiling: int | None = None,
) -> int:
    """Log the assembled request size and reject it if it exceeds the ceiling.

    Returns the estimated token count so callers can log it on the success path. `ceiling` defaults
    to the configured module value; `resolve_request_ceiling` supplies a per-endpoint one. Kept as
    a parameter rather than read from the global so this stays a pure function of its inputs.
    """
    total_chars, per_message = _describe_request_size(messages)
    estimated_tokens = max(1, (total_chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)
    effective_ceiling = _MAX_REQUEST_ESTIMATED_TOKENS if ceiling is None else ceiling

    if effective_ceiling and estimated_tokens > effective_ceiling:
        breakdown = ", ".join(
            f"[{index}] {role}={chars}c" for index, role, chars in per_message[:5]
        )
        logger.error(
            "Graphiti request REJECTED: assembled payload ~%s tokens exceeds ceiling %s "
            "(model=%s endpoint=%s messages=%s chars=%s). Largest messages: %s. "
            "The episode text is normally a tiny fraction of this — the overrun is in "
            "assembled context (candidate entities, previous episodes, schema).",
            f"{estimated_tokens:,}",
            f"{effective_ceiling:,}",
            model,
            endpoint,
            len(messages),
            f"{total_chars:,}",
            breakdown,
        )
        raise GraphitiRequestTooLargeError(
            f"Assembled extraction request is ~{estimated_tokens:,} estimated tokens, "
            f"over the {effective_ceiling:,} ceiling "
            f"(MEMORY_GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS). "
            f"{len(messages)} messages, {total_chars:,} chars; largest: {breakdown}. "
            f"Not sent — the provider would reject it as a context-length error."
        )

    return estimated_tokens


# ---------------------------------------------------------------------------
# OpenAI-generic-client patch (extracted verbatim to graphiti_llm_patches_patch)
# ---------------------------------------------------------------------------

# Imported last: the patch module imports the ceiling names defined above from this module, so
# this import resolves only once they exist. ``_MAX_REQUEST_ESTIMATED_TOKENS`` stays this
# module's global -- the request-size functions and the tests that patch it read it here -- and
# the patch writes it through this namespace (graphiti_llm_patches._MAX_REQUEST_ESTIMATED_TOKENS).
from menhir.infrastructure.graphiti_llm_patches_patch import (
    _LOCAL_MODEL_MAX_RETRIES,
    _TRUNCATION_CHAR_THRESHOLD,
    _is_context_length_error,
    _looks_like_truncation,
    _openai_strict_json_schema,
    _patch_graphiti_openai_generic_client,
    _truncation_offset,
)
