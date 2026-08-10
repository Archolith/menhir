"""Graphiti OpenAI-compatible response parsing and retry patches."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import importlib
import importlib.metadata
import json
import logging
from time import perf_counter
from typing import Any

from menhir.infrastructure.graphiti_helpers import (
    _build_graphiti_failure_details,
    _describe_openai_client_base_url,
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graphiti OpenAI-generic-client patch
# ---------------------------------------------------------------------------


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


def _enforce_request_size(
    messages: list[dict[str, str]], model: str, endpoint: str | None
) -> int:
    """Log the assembled request size and reject it if it exceeds the ceiling.

    Returns the estimated token count so callers can log it on the success path.
    """
    total_chars, per_message = _describe_request_size(messages)
    estimated_tokens = max(1, (total_chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)

    if _MAX_REQUEST_ESTIMATED_TOKENS and estimated_tokens > _MAX_REQUEST_ESTIMATED_TOKENS:
        breakdown = ", ".join(
            f"[{index}] {role}={chars}c" for index, role, chars in per_message[:5]
        )
        logger.error(
            "Graphiti request REJECTED: assembled payload ~%s tokens exceeds ceiling %s "
            "(model=%s endpoint=%s messages=%s chars=%s). Largest messages: %s. "
            "The episode text is normally a tiny fraction of this — the overrun is in "
            "assembled context (candidate entities, previous episodes, schema).",
            f"{estimated_tokens:,}",
            f"{_MAX_REQUEST_ESTIMATED_TOKENS:,}",
            model,
            endpoint,
            len(messages),
            f"{total_chars:,}",
            breakdown,
        )
        raise GraphitiRequestTooLargeError(
            f"Assembled extraction request is ~{estimated_tokens:,} estimated tokens, "
            f"over the {_MAX_REQUEST_ESTIMATED_TOKENS:,} ceiling "
            f"(MEMORY_GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS). "
            f"{len(messages)} messages, {total_chars:,} chars; largest: {breakdown}. "
            f"Not sent — the provider would reject it as a context-length error."
        )

    return estimated_tokens


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


def _patch_graphiti_openai_generic_client(
    OpenAIGenericClient: type | None,
    max_request_estimated_tokens: int | None = None,
) -> None:
    """Patch Graphiti's OpenAI-compatible client to handle loose JSON output.

    ``OpenAIGenericClient`` is passed explicitly so this module does not need
    its own conditional import of graphiti_core.  ``max_request_estimated_tokens``
    sets the assembled-request ceiling; 0 disables the check.
    """

    global _MAX_REQUEST_ESTIMATED_TOKENS
    if max_request_estimated_tokens is not None:
        _MAX_REQUEST_ESTIMATED_TOKENS = max(0, int(max_request_estimated_tokens))

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
                json_schema = response_model.model_json_schema()
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": json_schema,
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
            # Measure what is actually about to be sent. The pre-extraction guardrail
            # (graphiti_episode_max_estimated_tokens) only sees the episode text, which
            # is ~1% of this payload, so it cannot catch a context overrun.
            _estimated_request_tokens = _enforce_request_size(openai_messages, model, endpoint)
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
