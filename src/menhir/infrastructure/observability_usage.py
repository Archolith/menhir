"""LLM usage event model, callback plumbing, and call lifecycle.

Extracted verbatim from ``observability.py``, which re-exports these names. The process-wide
fallback sink (``_default_llm_usage_callback``) stays on ``observability`` -- tests patch it
there -- so ``_emit_llm_usage_event`` resolves it through that module at call time.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from menhir.infrastructure.observability_provider_auth import (
    clear_provider_auth_failure,
    is_provider_auth_error,
    record_provider_auth_failure,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMUsageEvent:
    """One observed LLM endpoint invocation."""

    kind: str
    phase: str
    model: str | None = None
    endpoint: str | None = None
    operation: str | None = None
    call_id: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    provider_usage: dict[str, Any] | None = None
    error: str | None = None
    #: This call is being MEASURED, not governed: a budget callback must count it and let it
    #: proceed rather than refusing it (CF-234). Set by the announcement site, never inferred, so
    #: a surface that does not opt in keeps whatever enforcement it has today.
    report_only: bool = False


@dataclass(frozen=True)
class LLMCallHandle:
    """Correlation state for one instrumented provider-client call."""

    call_id: str
    kind: str
    model: str | None
    endpoint: str | None
    operation: str | None
    started_at: float
    report_only: bool = False


LLMUsageCallback = Callable[[LLMUsageEvent], None]
_llm_usage_callback: ContextVar[LLMUsageCallback | None] = ContextVar(
    "menhir_llm_usage_callback",
    default=None,
)


def set_llm_usage_callback(callback: LLMUsageCallback | None) -> Token[LLMUsageCallback | None]:
    """Install a request-scoped callback for LLM usage instrumentation."""

    return _llm_usage_callback.set(callback)


def reset_llm_usage_callback(token: Token[LLMUsageCallback | None]) -> None:
    """Reset the request-scoped LLM usage callback to a previous token."""

    _llm_usage_callback.reset(token)


class LlmUsageControlSignal(Exception):
    """Raised BY a usage callback to REFUSE the call it is being notified about.

    The emitter below swallows exceptions from usage callbacks, and that is correct for what it
    was written for: an instrumentation fault must never take down the caller it is observing.
    But a budget REFUSAL is not an instrumentation fault -- it is a control decision whose entire
    purpose is to affect the caller, and the blanket `except Exception` swallowed it identically.

    That made CF-79's per-job reservation ineffective in production: `_record_episode_llm_usage`
    raised, this module logged it at DEBUG, and the call proceeded -- which is precisely the
    "counts and warns, never stops a call" behaviour CF-79 was filed about, one layer further
    down than anyone looked.

    Opting in explicitly, rather than narrowing the `except` to a specific type, keeps the
    original principle intact: anything a callback raises by ACCIDENT is still swallowed, and
    only a signal deliberately declaring itself control flow propagates.
    """


def _emit_llm_usage_event(
    kind: str,
    *,
    phase: str,
    model: str | None = None,
    endpoint: str | None = None,
    operation: str | None = None,
    call_id: str | None = None,
    duration_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
    provider_usage: dict[str, Any] | None = None,
    error: str | None = None,
    report_only: bool = False,
) -> None:
    from menhir.infrastructure import observability as _observability

    callback = _llm_usage_callback.get() or _observability._default_llm_usage_callback
    if callback is None:
        return
    try:
        callback(
            LLMUsageEvent(
                kind=kind,
                phase=phase,
                model=model,
                endpoint=endpoint,
                operation=operation,
                call_id=call_id,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_input_tokens=cached_input_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                provider_usage=provider_usage,
                error=error,
                report_only=report_only,
            )
        )
    except LlmUsageControlSignal:
        # A deliberate refusal, not a fault. It MUST reach the caller -- swallowing it is what
        # left CF-79's reservation unable to stop anything.
        raise
    except Exception:  # pragma: no cover - instrumentation must not affect callers
        logger.debug(
            "LLM usage callback failed for kind=%s phase=%s endpoint=%s",
            kind,
            phase,
            endpoint,
            exc_info=True,
        )


def _value(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        payload = dict(usage)
    elif hasattr(usage, "model_dump"):
        payload = usage.model_dump(exclude_none=True)
    elif hasattr(usage, "__dict__"):
        payload = {name: value for name, value in vars(usage).items() if value is not None}
    else:
        payload = {}
    if not payload:
        payload = {
            name: value
            for name in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "input_tokens",
                "output_tokens",
                "promptTokenCount",
                "candidatesTokenCount",
                "totalTokenCount",
            )
            if (value := getattr(usage, name, None)) is not None
        }
    return payload or None


def _normalized_usage(usage: Any) -> dict[str, Any]:
    """Normalize common OpenAI-style (snake_case and camelCase) usage shapes without estimating."""

    raw = _usage_dict(usage)
    if raw is None:
        return {}
    prompt_details = _value(usage, "prompt_tokens_details", "input_tokens_details") or {}
    completion_details = _value(usage, "completion_tokens_details", "output_tokens_details") or {}
    input_tokens = _nonnegative_int(_value(usage, "prompt_tokens", "input_tokens", "promptTokenCount"))
    output_tokens = _nonnegative_int(
        _value(usage, "completion_tokens", "output_tokens", "candidatesTokenCount")
    )
    total_tokens = _nonnegative_int(_value(usage, "total_tokens", "totalTokenCount"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cached_input_tokens = _nonnegative_int(
        _value(usage, "prompt_cache_hit_tokens", "cachedContentTokenCount")
    )
    if cached_input_tokens is None:
        cached_input_tokens = _nonnegative_int(_value(prompt_details, "cached_tokens"))
    reasoning_output_tokens = _nonnegative_int(_value(usage, "thoughtsTokenCount"))
    if reasoning_output_tokens is None:
        reasoning_output_tokens = _nonnegative_int(_value(completion_details, "reasoning_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "provider_usage": raw,
    }


def start_llm_usage_call(
    *,
    kind: str,
    model: str | None,
    endpoint: str | None,
    operation: str | None = None,
    report_only: bool = False,
) -> LLMCallHandle:
    """Announce a call. ``report_only`` asks a budget callback to count it, not refuse it.

    Carried on the handle so the completed/failed phases report it identically -- a call that was
    measured at reservation must not appear governed at completion.
    """
    handle = LLMCallHandle(
        call_id=uuid4().hex,
        kind=kind,
        model=model,
        endpoint=endpoint,
        operation=operation,
        started_at=perf_counter(),
        report_only=report_only,
    )
    _emit_llm_usage_event(
        kind,
        phase="started",
        model=model,
        endpoint=endpoint,
        operation=operation,
        call_id=handle.call_id,
        report_only=report_only,
    )
    return handle


def complete_llm_usage_call(
    handle: LLMCallHandle,
    *,
    result: Any = None,
    usage: Any = None,
) -> None:
    clear_provider_auth_failure()
    try:
        normalized = _normalized_usage(usage if usage is not None else _value(result, "usage"))
        _emit_llm_usage_event(
            handle.kind,
            phase="completed",
            model=handle.model,
            endpoint=handle.endpoint,
            operation=handle.operation,
            call_id=handle.call_id,
            duration_ms=int((perf_counter() - handle.started_at) * 1000),
            **normalized,
            report_only=handle.report_only,
        )
    except LlmUsageControlSignal:
        # CF-227 taught the EMITTER to let a refusal through; this handler is the emitter's own
        # caller, and a blanket `except Exception` here re-swallows exactly what that fix released.
        # Re-emitting instead would also call the budget callback a second time for one LLM call.
        raise
    except Exception:  # pragma: no cover - instrumentation must not affect callers
        logger.debug("Could not normalize LLM usage for call_id=%s", handle.call_id, exc_info=True)
        _emit_llm_usage_event(
            handle.kind,
            phase="completed",
            model=handle.model,
            endpoint=handle.endpoint,
            operation=handle.operation,
            call_id=handle.call_id,
            duration_ms=int((perf_counter() - handle.started_at) * 1000),
            report_only=handle.report_only,
        )


def fail_llm_usage_call(handle: LLMCallHandle, error: BaseException) -> None:
    if is_provider_auth_error(error):
        record_provider_auth_failure(handle, error)
    _emit_llm_usage_event(
        handle.kind,
        phase="failed",
        model=handle.model,
        endpoint=handle.endpoint,
        operation=handle.operation,
        call_id=handle.call_id,
        duration_ms=int((perf_counter() - handle.started_at) * 1000),
        error=str(error),
        report_only=handle.report_only,
    )
