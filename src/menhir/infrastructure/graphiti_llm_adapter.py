"""Menhir LLM policy adapter for the fork's OpenAI-compatible client.

Menhir-owned halves of the former ``_patch_graphiti_openai_generic_client``
(#17), wired through the fork's explicit seams instead of method rebinding:

- the concise-prompt/truncation-escalation retry loop remains Menhir-owned
  retry policy, expressed as a ``MenhirOpenAIGenericClient`` subclass override;
- strict-schema structured output is a ``_build_response_format`` override;
- provider extras (DeepSeek thinking suppression, OpenRouter reasoning
  suppression) are injected by a Menhir-owned proxy around the AsyncOpenAI
  client Menhir itself constructs and passes in;
- the request ceiling derivation policy (local llama.cpp ``/props`` probing),
  lifecycle telemetry, failure diagnostics, and per-episode request correlation
  are supplied through the fork's ``RequestGuard`` seam.
"""

from __future__ import annotations

import json
import logging
import os
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

try:
    from graphiti_core.errors import GraphitiRequestTooLargeError
except ImportError as exc:
    if "cannot import name 'GraphitiRequestTooLargeError'" not in str(exc):
        raise
    raise ImportError(
        "graphiti_core.errors is missing GraphitiRequestTooLargeError. Menhir requires "
        "archolith-graphiti-core==0.30.2.post3; an older graphiti-core install may have "
        "overwritten the fork's shared graphiti_core files. Use a fresh virtual environment "
        "or uninstall both graphiti-core distributions before reinstalling Menhir "
        "(see docs/post-install.md)."
    ) from exc
from graphiti_core.llm_client.client import get_extraction_language_instruction
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.llm_client.request_guard import (
    RequestGuard,
)
from graphiti_core.prompts.extract_edges import ExtractedEdges
from graphiti_core.prompts.extract_nodes_and_edges import CombinedExtraction
from graphiti_core.prompts.models import Message

from menhir.infrastructure.graphiti_extraction_policy import get_extraction_receipt
from menhir.infrastructure.graphiti_helpers import (
    _build_graphiti_failure_details,
    _normalize_graphiti_json_payload,
    _raw_preview,
)
from menhir.infrastructure.openai_calls import ResilientChatClient, acreate_chat_completion
from menhir.infrastructure.model_profiles import resolve_model_profile

logger = logging.getLogger(__name__)


#: Retry budget for the local-model-friendly retry loop below. Own the constant
#: here instead of reaching into graphiti internals that are free to change.
_LOCAL_MODEL_MAX_RETRIES = 3

#: Char-offset threshold for treating a JSONDecodeError as output truncation
#: rather than malformed output.
_TRUNCATION_CHAR_THRESHOLD = 500


def _looks_like_truncation(exc: Exception) -> bool:
    """Return True if *exc* looks like a JSON parse failure caused by output truncation."""
    offset = _truncation_offset(exc)
    return offset is not None and offset >= _TRUNCATION_CHAR_THRESHOLD


def _truncation_offset(exc: Exception) -> int | None:
    """Extract the char offset from a JSONDecodeError buried in *exc*, or None."""
    import re

    msg = str(exc)
    m = re.search(r"\(char (\d+)\)", msg)
    if m:
        return int(m.group(1))
    cause = exc.__cause__ if exc.__cause__ else exc
    if hasattr(cause, "pos") and isinstance(cause.pos, int):  # type: ignore[union-attr]
        return cause.pos  # type: ignore[union-attr]
    return None


# ---------------------------------------------------------------------------
# Request-ceiling derivation policy (Menhir half of #17)
# ---------------------------------------------------------------------------

#: Endpoint -> (expires_at_monotonic, derived_ceiling_or_None). Keyed on the endpoint because the
#: wake sequence rotates base URLs per task, so one process legitimately talks to several.
_derived_ceilings: dict[str, tuple[float, int | None]] = {}

#: Re-probe this often. The ceiling is a fact about whichever model the scheduler currently has
#: loaded; ten minutes bounds how long a stale ceiling can persist.
_DERIVED_CEILING_TTL_S = 600.0

#: Fraction of the model's window held back for the response.
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

    The key moved between llama.cpp versions; reading only the current spelling would silently
    return None against a build one version away.
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


async def resolve_request_ceiling(endpoint: str | None, configured: int) -> int:
    """Return the estimated-token ceiling to enforce for `endpoint`.

    TWO SAFETY RULES, and both are deliberate:

    * **Derivation may only LOWER the configured ceiling, never raise it.** The setting is also a
      cost bound, so a model with a huge window must not be allowed to widen it.
    * **A configured ceiling of 0 means the check is disabled, and derivation must not re-enable
      it.** Turning a deliberate opt-out back on because a probe succeeded would be the mechanism
      overriding the operator.

    Any failure returns the configured value.
    """
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


class MenhirRequestCeilingResolver:
    """Adapter from Menhir's endpoint-probing ceiling policy to the fork's resolver seam."""

    def __init__(self, configured_ceiling: int) -> None:
        self._configured = configured_ceiling

    async def resolve_request_ceiling(self, context: Any) -> int | None:
        return await resolve_request_ceiling(context.endpoint, self._configured)


class MenhirRequestLifecycleHook:
    """Log request begin/complete lines from the fork's lifecycle seam."""

    def on_request_started(self, context: Any) -> None:
        logger.info(
            "Graphiti OpenAI-compatible request begin model=%s endpoint=%s response_format=json_object "
            "message_count=%s estimated_tokens=%s correlation_id=%s",
            context.model,
            context.endpoint,
            context.message_count,
            context.estimated_tokens,
            context.correlation_id,
        )

    def on_request_completed(self, context: Any, response: Any) -> None:
        logger.info(
            "Graphiti OpenAI-compatible response received model=%s endpoint=%s status=%s id=%s "
            "duration_ms=%s request_tokens_est=%s payload_preview=%r",
            context.model,
            context.endpoint,
            response.status,
            response.response_id,
            response.duration_ms,
            context.estimated_tokens,
            _raw_preview(response.raw_preview, limit=240),
        )


class MenhirRequestFailureListener:
    """Attach Menhir's structured failure diagnostics to provider failures.

    The fork observes every provider/send/parse failure exactly once through this
    seam with structural (content-free) facts only; Menhir attaches its
    ``menhir_failure_details`` diagnostic record here so downstream error
    reporting keeps its shape.
    """

    def on_request_failed(
        self,
        context: Any,
        error: Exception,
        phase: str,
        response: Any,
    ) -> None:
        try:
            details = _build_graphiti_failure_details(
                model=context.model,
                endpoint=context.endpoint or "unknown",
                messages=[{} for _ in range(context.message_count)],
                raw_result="" if response is None else (response.raw_preview or ""),
                response_status=None if response is None else response.status,
                response_id=None if response is None else response.response_id,
            )
            details["graphiti_message_count"] = context.message_count
            details["graphiti_total_chars"] = context.total_chars
            details["graphiti_estimated_tokens"] = context.estimated_tokens
            details["graphiti_largest_messages"] = str(context.describe_largest_messages())
            details["graphiti_failure_phase"] = phase
            details["graphiti_correlation_id"] = context.correlation_id
            if not hasattr(error, "menhir_failure_details"):
                error.menhir_failure_details = details  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
            logger.debug("Failure-diagnostics attachment failed", exc_info=True)
        logger.warning(
            "Graphiti OpenAI-compatible request failed phase=%s model=%s endpoint=%s "
            "estimated_tokens=%s error=%s",
            phase,
            context.model,
            context.endpoint,
            context.estimated_tokens,
            error,
        )


class MenhirRequestCorrelationProvider:
    """Per-episode request correlation from the active extraction receipt."""

    def resolve_correlation(self, context: Any) -> str | None:
        try:
            receipt = get_extraction_receipt()
        except Exception:  # noqa: BLE001
            return None
        if receipt is None:
            return None
        return getattr(receipt, "episode_key", None) or None


def build_menhir_request_guard(configured_ceiling: int) -> RequestGuard:
    """Bundle Menhir's ceiling policy, telemetry, diagnostics, and correlation for the fork."""
    return RequestGuard(
        ceiling_resolver=MenhirRequestCeilingResolver(configured_ceiling),
        lifecycle_hook=MenhirRequestLifecycleHook(),
        failure_listener=MenhirRequestFailureListener(),
        correlation_provider=MenhirRequestCorrelationProvider(),
    )


# ---------------------------------------------------------------------------
# Provider extras proxy (DeepSeek / OpenRouter request policies)
# ---------------------------------------------------------------------------


class _ProviderExtrasAsyncClient:
    """Menhir-owned proxy around the AsyncOpenAI client that adds provider extras.

    The fork's ``_generate_response`` builds its provider call from
    ``self.client.chat.completions.create``; Menhir supplies its own client object
    pre-wrapped in this proxy, so the DeepSeek thinking-suppression and the opt-in
    OpenRouter reasoning-suppression policies apply without touching fork code.
    """

    def __init__(self, inner: Any, base_url: str) -> None:
        self._inner = inner
        self._base_url = base_url

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def base_url(self) -> Any:
        return getattr(self._inner, "base_url", self._base_url)

    @property
    def chat(self) -> Any:
        return _ProviderExtrasChat(self._inner.chat, self._base_url)


#: Kept for importers: the reranker wrapper now lives in ``openai_calls``.
RateLimitedChatClient = ResilientChatClient


class _ProviderExtrasChat:
    def __init__(self, inner: Any, base_url: str) -> None:
        self._inner = inner
        self._base_url = base_url

    @property
    def completions(self) -> Any:
        return _ProviderExtrasCompletions(self._inner.completions, self._base_url)


class _ProviderExtrasCompletions:
    def __init__(self, inner: Any, base_url: str) -> None:
        self._inner = inner
        self._base_url = base_url

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create(self, **kwargs: Any) -> Any:
        extra: dict[str, Any] = _provider_extra_body(kwargs.get("model"), self._base_url)
        if extra:
            kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), **extra}
        # Merge lineage (merge_audit / merged_from / last_merge_op_id) is kept out of prompts by
        # the fork itself since archolith-graphiti-core 0.30.2.post2 (graphiti #2); see
        # tests/infrastructure/test_merge_lineage_prompt_policy.py.
        # Shaping (model profile, Flex tier) and 429 backoff live here because generate_response
        # re-raises RateLimitError unretried.
        return await acreate_chat_completion(
            self._inner.create, kwargs, base_url=self._base_url, label="graphiti"
        )


def _provider_extra_body(model: str | None, endpoint: str) -> dict[str, Any]:
    """Provider-specific request extras, decided per request from model + endpoint.

    - Model-family extras come from the model profile (DeepSeek: thinking off).
    - OpenRouter reasoning suppression is OPT-IN via MENHIR_GRAPHITI_DISABLE_REASONING,
      because suppressing reasoning is a quality decision that belongs to whoever
      configured the provider.
    """
    profile_extra = resolve_model_profile(model, endpoint=endpoint).provider_extra_body()
    if profile_extra:
        return profile_extra
    if "openrouter" in (endpoint or "").lower() and os.getenv(
        "MENHIR_GRAPHITI_DISABLE_REASONING", ""
    ).strip().lower() in {"1", "true", "yes"}:
        return {"reasoning": {"enabled": False}}
    return {}


# ---------------------------------------------------------------------------
# Structured-output schema policy
# ---------------------------------------------------------------------------


def _openai_strict_json_schema(response_model: type[Any]) -> dict[str, Any]:
    """Return the model schema in the strict subset accepted by OpenAI-compatible APIs.

    Pydantic permits unspecified object extras and defaulted properties. OpenAI structured
    outputs instead require every object to set ``additionalProperties: false`` and every
    declared property to appear in ``required``. Build a transformed copy for the wire
    contract; do not tighten the Pydantic model that parses local and non-strict provider
    responses.
    """

    def _strict(value: Any) -> Any:
        if isinstance(value, list):
            return [_strict(item) for item in value]
        if not isinstance(value, dict):
            return value

        result = {
            key: _strict(item)
            for key, item in value.items()
            if not (key == "default" and item is None)
        }
        if result.get("type") == "object":
            result["additionalProperties"] = False
            properties = result.get("properties")
            if isinstance(properties, dict):
                result["required"] = list(properties)
        return result

    schema = response_model.model_json_schema()
    if not isinstance(schema, dict):
        raise TypeError("response model JSON schema must be an object")
    return _strict(schema)


# ---------------------------------------------------------------------------
# Menhir LLM client subclass
# ---------------------------------------------------------------------------


class MenhirOpenAIGenericClient(OpenAIGenericClient):
    """OpenAI-compatible client with Menhir's strict-schema and retry policies.

    Subclassing — not rebinding. The fork owns request sizing, ceiling enforcement,
    context-length normalization (it raises the fork's
    ``GraphitiRequestTooLargeError``), code-fence tolerance, and prose-JSON
    normalization; this subclass keeps Menhir's concise local-model retry prompt,
    truncation escalation, and the strict OpenAI structured-output schema.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        client: Any = None,
        max_tokens: int = 16384,
        structured_output_mode: str = "json_schema",
        request_guard: RequestGuard | None = None,
    ) -> None:
        super().__init__(
            config=config,
            client=client,
            max_tokens=max_tokens,
            structured_output_mode=structured_output_mode,  # type: ignore[arg-type]
            request_guard=request_guard,
        )

    def _build_response_format(self, response_model: type[Any] | None) -> dict[str, Any]:
        """Strict-schema structured output; json_object fallback otherwise."""
        if response_model is None or self.structured_output_mode == "json_object":
            return {"type": "json_object"}

        schema_name = getattr(response_model, "__name__", "structured_response")
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": _openai_strict_json_schema(response_model),
                "strict": True,
            },
        }

    async def generate_response(
        self,
        messages: list[Any],
        response_model: type[Any] | None = None,
        max_tokens: int | None = None,
        model_size: Any = None,
        group_id: str | None = None,
        prompt_name: str | None = None,
        *,
        attribute_extraction: bool = False,
    ) -> dict[str, Any]:
        """Menhir's concise retry loop around the fork's single-shot request path.

        The default fork retry wrapper uses verbose boilerplate that wastes context
        tokens and confuses local Qwen3 models; this sends a concise, imperative
        retry message and escalates max_tokens on output truncation.
        """
        import openai as _openai

        from graphiti_core.llm_client.client import RateLimitError

        self._apply_attribute_extraction_preamble(messages, attribute_extraction)
        if response_model is not None and self.structured_output_mode == "json_object":
            serialized_model = json.dumps(response_model.model_json_schema())
            messages[-1].content += (
                f"\n\nRespond with a JSON object in the following format:\n\n{serialized_model}"
            )

        if max_tokens is None:
            max_tokens = self.max_tokens
        effective_max_tokens = max_tokens

        messages[0].content += get_extraction_language_instruction(group_id)

        retry_count = 0
        last_error: Exception | None = None

        while retry_count <= _LOCAL_MODEL_MAX_RETRIES:
            try:
                response = await self._generate_response(
                    messages,
                    response_model,
                    max_tokens=effective_max_tokens,
                    model_size=model_size,
                    group_id=group_id,
                    prompt_name=prompt_name,
                )
                # This normalizer rewrites generic keys such as ``type`` and
                # ``entity``. Apply it only to extraction envelopes; typed
                # attribute models may use those keys literally.
                if (
                    getattr(self, "structured_output_mode", "json_schema") == "json_object"
                    and response_model in (CombinedExtraction, ExtractedEdges)
                ):
                    return _normalize_graphiti_json_payload(response)
                return response
            except RateLimitError:
                raise
            except GraphitiRequestTooLargeError:
                # Retrying cannot help: the retry prompt only makes the payload larger.
                raise
            except (
                _openai.APITimeoutError,
                _openai.APIConnectionError,
                _openai.InternalServerError,
            ):
                raise
            except Exception as exc:
                last_error = exc
                if retry_count >= _LOCAL_MODEL_MAX_RETRIES:
                    logger.error(
                        "Graphiti LLM max retries (%d) exceeded. Last error: %s",
                        _LOCAL_MODEL_MAX_RETRIES,
                        exc,
                    )
                    raise

                retry_count += 1

                _is_truncation = _looks_like_truncation(exc)

                if _is_truncation and retry_count == 1:
                    effective_max_tokens = min(effective_max_tokens * 2, 16384)
                    retry_prompt = (
                        f"Retry {retry_count}/{_LOCAL_MODEL_MAX_RETRIES}. "
                        f"Your previous response was truncated (output too long). "
                        f"Output ONLY a raw JSON object. "
                        f"Start immediately with {{ and end with }}. "
                        f"No thinking tags, no markdown fences, no extra text."
                    )
                    logger.warning(
                        "Graphiti LLM retry %d/%d: truncation detected (char %s), "
                        "escalating max_tokens %d -> %d",
                        retry_count,
                        _LOCAL_MODEL_MAX_RETRIES,
                        _truncation_offset(exc),
                        max_tokens,
                        effective_max_tokens,
                    )
                elif _is_truncation and retry_count >= 2:
                    effective_max_tokens = min(effective_max_tokens * 2, 16384)
                    retry_prompt = (
                        f"Retry {retry_count}/{_LOCAL_MODEL_MAX_RETRIES}. "
                        f"Your response was STILL truncated. You MUST fit "
                        f"the entire JSON in one response. Reduce the number "
                        f"of extracted entities and edges to the most important "
                        f"ones. Output ONLY a raw JSON object. "
                        f"Start immediately with {{ and end with }}. "
                        f"No thinking tags, no markdown fences, no extra text."
                    )
                    logger.warning(
                        "Graphiti LLM retry %d/%d: persistent truncation, "
                        "requesting reduced extraction (max_tokens=%d)",
                        retry_count,
                        _LOCAL_MODEL_MAX_RETRIES,
                        effective_max_tokens,
                    )
                else:
                    retry_prompt = (
                        f"Retry {retry_count}/{_LOCAL_MODEL_MAX_RETRIES}. "
                        f"Previous attempt failed: {exc}\n\n"
                        f"Output ONLY a raw JSON object. "
                        f"Start immediately with {{ and end with }}. "
                        f"No thinking tags, no markdown fences, no extra text."
                    )
                    logger.warning(
                        "Graphiti LLM retry %d/%d after error: %s",
                        retry_count,
                        _LOCAL_MODEL_MAX_RETRIES,
                        exc,
                    )

                messages.append(Message(role="user", content=retry_prompt))

        raise last_error or Exception("Max retries exceeded with no specific error")
