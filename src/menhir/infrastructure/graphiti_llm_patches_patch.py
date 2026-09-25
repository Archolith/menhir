"""The OpenAI-generic-client patch: loose-JSON handling, retry escalation, strict schemas.

Owns ``_patch_graphiti_openai_generic_client`` -- the ``_generate_response`` replacement plus
the concise local-model ``generate_response`` retry loop -- and its support helpers: the
truncation heuristics (``_looks_like_truncation``, ``_truncation_offset``), the provider
context-length classifier (``_is_context_length_error``), ``_openai_strict_json_schema``, and
the two retry constants. Extracted verbatim from ``graphiti_llm_patches``, which re-exports
everything here.

The configured request ceiling stays a facade-module global (``_MAX_REQUEST_ESTIMATED_TOKENS``)
because the request-size functions and the tests that patch it read it there, so the one write
to it inside ``_patch_graphiti_openai_generic_client`` resolves through the facade namespace.
"""

from __future__ import annotations

import json
import logging
import os
from time import perf_counter
from typing import Any

from menhir.infrastructure.graphiti_helpers import (
    _build_graphiti_failure_details,
    _describe_openai_client_base_url,
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
)
import menhir.infrastructure.graphiti_llm_patches as graphiti_llm_patches
from menhir.infrastructure.graphiti_llm_patches import (
    GraphitiRequestTooLargeError,
    _enforce_request_size,
    resolve_request_ceiling,
)

logger = logging.getLogger(__name__)


#: Retry budget for the local-model-friendly retry loop below. Graphiti 0.28.x's
#: LLMClient base class exposed a MAX_RETRIES class attribute this patch used to read
#: (self.MAX_RETRIES); Graphiti 0.29 replaced that whole retry mechanism with an
#: internal tenacity-decorated _generate_response_with_retry that this patch does not
#: call (generate_response is overridden wholesale for the concise local-model retry
#: prompt), so the attribute no longer exists on the base class at all. Own the
#: constant here instead of reaching into graphiti internals that are free to change.
_LOCAL_MODEL_MAX_RETRIES = 3

#: Char-offset threshold for treating a JSONDecodeError as output truncation
#: rather than malformed output.  When the parser fails beyond this point the
#: model likely ran out of output tokens mid-JSON — retrying with the same
#: max_tokens will hit the same wall, so the retry loop escalates instead.
_TRUNCATION_CHAR_THRESHOLD = 500


def _is_context_length_error(exc: Exception) -> bool:
    """Return True when a provider rejected the assembled request for context size."""

    code = str(getattr(exc, "code", "") or "").strip().lower()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or code).strip().lower()
    if code == "context_length_exceeded":
        return True

    rendered = str(exc).lower()
    return "context_length_exceeded" in rendered or "maximum context length" in rendered


def _looks_like_truncation(exc: Exception) -> bool:
    """Return True if *exc* looks like a JSON parse failure caused by output truncation.

    Truncation errors are typically ``json.JSONDecodeError`` (wrapped in a
    ``ValueError`` by our parse layer) where the parser fails at a high char
    offset — the model started producing valid JSON but ran out of output
    tokens before closing it.  Low-offset failures are more likely malformed
    output (wrong schema, extra text before the JSON, etc.).
    """
    offset = _truncation_offset(exc)
    return offset is not None and offset >= _TRUNCATION_CHAR_THRESHOLD


def _truncation_offset(exc: Exception) -> int | None:
    """Extract the char offset from a JSONDecodeError buried in *exc*, or None."""
    # The parse layer wraps json.JSONDecodeError in a ValueError whose message
    # contains the original error text, e.g.:
    #   "Your response was not valid JSON (Expecting ',' delimiter: line 1 column 3110 (char 3109))."
    import re

    msg = str(exc)
    # Match "char NNNN" from json.JSONDecodeError representation
    m = re.search(r"\(char (\d+)\)", msg)
    if m:
        return int(m.group(1))
    # Also check if the exception itself is a JSONDecodeError (has .pos)
    cause = exc.__cause__ if exc.__cause__ else exc
    if hasattr(cause, "pos") and isinstance(cause.pos, int):  # type: ignore[union-attr]
        return cause.pos  # type: ignore[union-attr]
    return None


def _openai_strict_json_schema(response_model: type[Any]) -> dict[str, Any]:
    """Return the model schema in the strict subset accepted by OpenAI-compatible APIs.

    Pydantic permits unspecified object extras and defaulted properties. OpenAI structured
    outputs instead require every object to set ``additionalProperties: false`` and every
    declared property to appear in ``required``. OpenRouter forwards the same validation to
    Luna's OpenAI/Azure backends, so sending ``model_json_schema()`` unchanged is rejected before
    inference. Build a transformed copy for the wire contract; do not tighten the Pydantic model
    that parses local and non-strict provider responses.
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


def _patch_graphiti_openai_generic_client(
    OpenAIGenericClient: type | None,
    max_request_estimated_tokens: int | None = None,
) -> None:
    """Patch Graphiti's OpenAI-compatible client to handle loose JSON output.

    ``OpenAIGenericClient`` is passed explicitly so this module does not need
    its own conditional import of graphiti_core.  ``max_request_estimated_tokens``
    sets the assembled-request ceiling; 0 disables the check.
    """

    # The configured ceiling is the facade module's global (the request-size functions and
    # the tests that patch it read it there), so the write goes through the facade namespace.
    if max_request_estimated_tokens is not None:
        graphiti_llm_patches._MAX_REQUEST_ESTIMATED_TOKENS = max(0, int(max_request_estimated_tokens))

    if OpenAIGenericClient is None or getattr(OpenAIGenericClient, "_yawn_patched", False):
        return

    async def _generate_response(
        self,
        messages: list[Any],
        response_model: type[Any] | None = None,
        max_tokens: int = 2048,
        model_size: Any = None,
    ) -> dict[str, Any]:
        model = self.model or "gpt-4.1-mini"
        openai_messages: list[dict[str, str]] = []
        for message in messages:
            message.content = self._clean_input(message.content)
            if message.role == "user":
                openai_messages.append({"role": "user", "content": message.content})
            elif message.role == "system":
                openai_messages.append({"role": "system", "content": message.content})
        endpoint = _describe_openai_client_base_url(self.client)
        logger.info(
            "Graphiti OpenAI-compatible request begin model=%s endpoint=%s response_format=%s message_count=%s",
            model,
            endpoint,
            "json_object",
            len(openai_messages),
        )
        raw_result = ""
        response_status = None
        response_id = None
        try:
            _model_str = (model or "").lower()
            _is_deepseek = "deepseek" in (endpoint or "").lower() or "deepseek" in _model_str
            response_format: dict[str, Any] = {"type": "json_object"}
            # deepseek-v4-flash does NOT support the json_schema response_format (returns
            # 400 "This response_format type is unavailable now", with thinking on OR off).
            # It DOES support json_object and returns valid JSON, so keep json_object for
            # deepseek and only use structured json_schema outputs on models that allow it.
            if response_model is not None and not _is_deepseek:
                schema_name = getattr(response_model, "__name__", "structured_response")
                json_schema = _openai_strict_json_schema(response_model)
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": json_schema,
                        "strict": True,
                    },
                }
            elif response_model is not None and _is_deepseek:
                # deepseek uses plain json_object (no schema enforcement), so it does not
                # know the expected field shape and returns generic JSON that graphiti
                # parses into zero entities. Put the response_model's JSON Schema in the
                # prompt so the model emits a conforming object.
                try:
                    _schema_json = json.dumps(response_model.model_json_schema(), ensure_ascii=True)
                    openai_messages.append({
                        "role": "system",
                        "content": (
                            "Respond with ONLY a single JSON object that conforms exactly to this "
                            "JSON Schema (no markdown, no prose, no code fences):\n" + _schema_json
                        ),
                    })
                except (TypeError, ValueError):
                    logger.debug("Could not serialize response_model schema for deepseek prompt injection", exc_info=True)

            _effective_max_tokens = max_tokens if max_tokens else self.max_tokens
            request_started = perf_counter()
            # gpt-5* and o-series models require max_completion_tokens; older models use max_tokens
            _uses_completion_tokens = _model_str.startswith("gpt-5") or _model_str.startswith("o1") or _model_str.startswith("o3") or _model_str.startswith("o4")
            _token_kwargs: dict[str, Any] = (
                {"max_completion_tokens": _effective_max_tokens}
                if _uses_completion_tokens
                else {"max_tokens": _effective_max_tokens}
            )
            # deepseek-v4-flash "thinks" by default (~2.3x latency, ~3x output tokens),
            # which makes per-episode extraction overrun the ingest wait window. Graphiti
            # extraction needs no chain-of-thought, so disable thinking for deepseek.
            # {"type": "disabled"} is the only form the DeepSeek API accepts (boolean /
            # enable_thinking / reasoning_effort all fail). With json_object + thinking off,
            # extraction is ~1.1s and returns valid JSON.
            _extra: dict[str, Any] = {}
            if _is_deepseek:
                _extra["extra_body"] = {"thinking": {"type": "disabled"}}
            # Same problem as the DeepSeek case above, different provider vocabulary. Measured
            # 2026-09-08 on archolith-bench's 1-item date smoke: openai/gpt-5.6-luna via
            # OpenRouter spent 12,997 of 19,859 output tokens (65%) on reasoning, ran 2.1x the
            # wall clock of gpt-4o-mini, and cost 2.2x. Graphiti extraction asks for a JSON
            # object; it needs no chain-of-thought. OpenRouter's unified form is
            # {"reasoning": {"enabled": false}} -- verified to drive reasoning_tokens to 0,
            # where "effort": "minimal" only reduces them.
            #
            # OPT-IN, default off: this is read from the environment rather than applied to every
            # OpenRouter endpoint, because suppressing reasoning is a quality decision that
            # belongs to whoever configured the provider, not to a default. Promote it to
            # MemorySettings if it ever becomes policy rather than an experiment.
            elif "openrouter" in (endpoint or "").lower() and os.getenv(
                "MENHIR_GRAPHITI_DISABLE_REASONING", ""
            ).strip().lower() in {"1", "true", "yes"}:
                _extra["extra_body"] = {"reasoning": {"enabled": False}}
            # Measure what is actually about to be sent. The pre-extraction guardrail
            # (graphiti_episode_max_estimated_tokens) only sees the episode text, which
            # is ~1% of this payload, so it cannot catch a context overrun.
            _ceiling = await resolve_request_ceiling(endpoint)
            _estimated_request_tokens = _enforce_request_size(
                openai_messages, model, endpoint, _ceiling
            )
            response = await self.client.chat.completions.create(
                model=model,
                messages=openai_messages,
                temperature=self.temperature,
                **_token_kwargs,
                response_format=response_format,  # type: ignore[arg-type]
                **_extra,
            )
            duration_ms = int((perf_counter() - request_started) * 1000)
            response_status = getattr(response, "status_code", None)
            response_id = getattr(response, "id", None)
            first_choice = response.choices[0] if getattr(response, "choices", []) else None
            message = getattr(first_choice, "message", None)
            raw_result = str(getattr(message, "content", "") or "")
            logger.info(
                "Graphiti OpenAI-compatible response received model=%s endpoint=%s status=%s id=%s duration_ms=%s request_tokens_est=%s payload_preview=%r",
                model,
                endpoint,
                response_status,
                response_id,
                duration_ms,
                _estimated_request_tokens,
                _raw_preview(raw_result, limit=240),
            )
            json_payload = _extract_first_json_payload(raw_result)
            try:
                parsed = json.loads(json_payload)
            except json.JSONDecodeError as exc:
                diagnostics = _build_graphiti_failure_details(
                    model=model,
                    endpoint=endpoint,
                    messages=openai_messages,
                    raw_result=raw_result,
                    response_status=response_status,
                    response_id=response_id,
                )
                logger.warning(
                    "Graphiti OpenAI-compatible response failed JSON decode model=%s endpoint=%s status=%s payload_preview=%r error=%s",
                    model,
                    endpoint,
                    response_status,
                    _raw_preview(raw_result, limit=240),
                    exc,
                )
                failure = ValueError(
                    f"Your response was not valid JSON ({exc}). "
                    f"You MUST respond with ONLY a valid JSON object — no extra text, "
                    f"no truncation, no markdown fences. Complete the full JSON output."
                )
                failure.menhir_failure_details = diagnostics  # type: ignore[attr-defined]
                raise failure from exc
            return _normalize_graphiti_json_payload(parsed)
        except ValueError as exc:
            if not hasattr(exc, "menhir_failure_details"):
                exc.menhir_failure_details = _build_graphiti_failure_details(  # type: ignore[attr-defined]
                    model=model,
                    endpoint=endpoint,
                    messages=openai_messages,
                    raw_result=raw_result,
                    response_status=response_status,
                    response_id=response_id,
                )
            logger.warning(
                "Graphiti OpenAI-compatible response parse failure model=%s endpoint=%s payload_preview=%r error=%s",
                model,
                endpoint,
                _raw_preview(raw_result, limit=240),
                exc,
            )
            raise
        except Exception as exc:
            logger.error("Error in generating LLM response: %s", exc)
            raise

    async def generate_response(
        self,
        messages: list[Any],
        response_model: type[Any] | None = None,
        max_tokens: int | None = None,
        model_size: Any = None,
        group_id: str | None = None,
        prompt_name: str | None = None,
    ) -> dict[str, Any]:
        """Override the retry loop to use a tighter retry prompt for local models.

        The default graphiti retry template buries the instruction in verbose
        boilerplate ("The previous response attempt was invalid. Error type: …
        Error details: … Please try again…"), which wastes context tokens and
        confuses local Qwen3 models.  This replacement sends a concise,
        imperative retry message instead.
        """
        import openai as _openai

        try:
            from graphiti_core.llm_client.client import (
                RateLimitError,
                get_extraction_language_instruction,
            )
            from graphiti_core.prompts.models import Message
        except ImportError:
            from graphiti_core.llm_client.client import RateLimitError  # type: ignore[no-redef]

            class Message:  # type: ignore[no-redef]
                def __init__(self, role: str, content: str) -> None:
                    self.role = role
                    self.content = content

            def get_extraction_language_instruction(group_id: str | None) -> str:  # type: ignore[misc]
                return ""

        if max_tokens is None:
            max_tokens = self.max_tokens
        effective_max_tokens = max_tokens

        messages[0].content += get_extraction_language_instruction(group_id)

        retry_count = 0
        last_error: Exception | None = None

        while retry_count <= _LOCAL_MODEL_MAX_RETRIES:
            try:
                return await self._generate_response(
                    messages, response_model, max_tokens=effective_max_tokens, model_size=model_size
                )
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
                if _is_context_length_error(exc):
                    # The payload is deterministic. Retrying it unchanged only burns
                    # provider calls and makes the prompt larger with retry messages.
                    raise GraphitiRequestTooLargeError(
                        "Provider rejected the assembled Graphiti request because it exceeds "
                        "the model context window. The node-deduplication path may split and retry it."
                    ) from exc
                last_error = exc
                if retry_count >= _LOCAL_MODEL_MAX_RETRIES:
                    logger.error(
                        "Graphiti LLM max retries (%d) exceeded. Last error: %s",
                        _LOCAL_MODEL_MAX_RETRIES,
                        exc,
                    )
                    raise

                retry_count += 1

                # Detect output truncation: JSONDecodeError at a high char
                # offset means the model ran out of output tokens mid-JSON.
                # Retrying with the same limit hits the same wall.
                _is_truncation = _looks_like_truncation(exc)

                if _is_truncation and retry_count == 1:
                    # Escalation 1: double max_tokens — often enough for
                    # gpt-4o-mini which has 16K output capacity.
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
                    # Escalation 2: max_tokens already doubled — ask the
                    # model to limit its extraction scope.
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
                    # Non-truncation error: standard retry prompt
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

    OpenAIGenericClient._generate_response = _generate_response  # type: ignore[assignment]
    OpenAIGenericClient.generate_response = generate_response  # type: ignore[assignment]
    OpenAIGenericClient._yawn_patched = True  # type: ignore[attr-defined]
