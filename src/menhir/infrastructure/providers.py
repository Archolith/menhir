"""Provider abstractions for pluggable chat/embed model backends."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
from typing import Any, Protocol, runtime_checkable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from menhir.config import MemorySettings
from menhir.infrastructure.llama_endpoint import acquire_llama_url_async, should_use_scheduler
from menhir.infrastructure.observability import (
    build_async_openai_client,
    complete_llm_usage_call,
    fail_llm_usage_call,
    start_llm_usage_call,
)

SchedulerUrlAcquire = Callable[..., Awaitable[str]]
OpenAIClientFactory = Callable[..., Any]
RetrySleep = Callable[[float], Awaitable[None]]

logger = logging.getLogger(__name__)


class ProviderKind(StrEnum):
    LOCAL = "local"           # local OpenAI-compatible (llama.cpp, etc.)
    OPENAI = "openai"         # OpenAI API
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


def parse_provider_kind(value: str | ProviderKind | None, *, default: ProviderKind = ProviderKind.LOCAL) -> ProviderKind:
    if value is None:
        return default
    if isinstance(value, ProviderKind):
        return value
    normalized = str(value).strip().lower().replace("-", "_")
    if not normalized:
        return default
    # backward compat alias
    if normalized == "openai_compat":
        return ProviderKind.LOCAL
    try:
        return ProviderKind(normalized)
    except ValueError as exc:
        raise ValueError(
            "Unsupported LLM provider. Use one of: local, openai, gemini, anthropic."
        ) from exc


@dataclass(frozen=True)
class ProviderConfig:
    kind: ProviderKind
    base_url: str
    api_key: str
    chat_model: str
    embed_model: str = ""

    @classmethod
    def _for_local(cls, settings: MemorySettings) -> "ProviderConfig":
        return cls(
            kind=ProviderKind.LOCAL,
            base_url=settings.local_llm_base_url,
            api_key=settings.local_llm_api_key,
            chat_model=settings.local_llm_chat_model,
            embed_model=settings.local_llm_embed_model,
        )

    @classmethod
    def _for_openai(cls, settings: MemorySettings) -> "ProviderConfig":
        return cls(
            kind=ProviderKind.OPENAI,
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
            chat_model=settings.openai_chat_model,
            embed_model=settings.openai_embed_model,
        )

    @classmethod
    def _for_gemini(cls, settings: MemorySettings) -> "ProviderConfig":
        return cls(
            kind=ProviderKind.GEMINI,
            base_url=settings.gemini_base_url,
            api_key=settings.gemini_api_key,
            chat_model=settings.gemini_chat_model,
            embed_model="",
        )

    @classmethod
    def _base_for_kind(cls, kind: ProviderKind, settings: MemorySettings) -> "ProviderConfig":
        if kind is ProviderKind.LOCAL:
            return cls._for_local(settings)
        if kind is ProviderKind.OPENAI:
            return cls._for_openai(settings)
        if kind is ProviderKind.GEMINI:
            return cls._for_gemini(settings)
        return cls(
            kind=ProviderKind.ANTHROPIC,
            base_url="",
            api_key="",
            chat_model="",
            embed_model="",
        )

    @classmethod
    def for_chat(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(settings.chat_provider)
        return cls._base_for_kind(kind, settings)

    @classmethod
    def for_graphiti_llm(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(
            settings.graphiti_provider,
            default=parse_provider_kind(settings.chat_provider),
        )
        return cls._base_for_kind(kind, settings)

    @classmethod
    def for_graphiti_embedder(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(
            settings.graphiti_embed_provider or settings.graphiti_provider,
            default=parse_provider_kind(settings.chat_provider),
        )
        base = cls._base_for_kind(kind, settings)
        # Local embed may use a separate base URL (different port/server)
        if kind is ProviderKind.LOCAL and settings.local_llm_embed_base_url:
            return cls(
                kind=base.kind,
                base_url=settings.local_llm_embed_base_url,
                api_key=base.api_key,
                chat_model=base.chat_model,
                embed_model=settings.local_llm_embed_model or base.embed_model,
            )
        return base

    @classmethod
    def for_graphiti_reranker(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(
            settings.graphiti_reranker_provider or settings.graphiti_provider,
            default=parse_provider_kind(settings.chat_provider),
        )
        return cls._base_for_kind(kind, settings)

    def supports_graphiti_openai_contract(self) -> bool:
        return self.kind in {ProviderKind.LOCAL, ProviderKind.OPENAI}


@runtime_checkable
class ChatBackend(Protocol):
    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Return plain text chat completion output.

        An empty or refused completion returns ""; implementations must not
        raise for that case.
        """


#: The package-wide fail-fast budget for one LLM/embedding HTTP call.
#:
#: Named and exported because the OpenAI SDK's defaults are the opposite policy -- 600 s read plus
#: 2 automatic retries, i.e. ~30 minutes worst case per call. Every seam that constructs a client
#: must pass this and `max_retries=0` explicitly; two seams did not, which is CF-190. Compare
#: `llm.py`: "fail fast -- don't block MCP on a down server".
DEFAULT_REQUEST_TIMEOUT_S: float = 30.0


@dataclass(frozen=True)
class ProviderRuntimeDependencies:
    """Runtime hooks for OpenAI-compatible provider I/O."""

    scheduler_url_acquire: SchedulerUrlAcquire = acquire_llama_url_async
    openai_client_factory: OpenAIClientFactory = build_async_openai_client
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    retry_sleep: RetrySleep = asyncio.sleep


@dataclass
class OpenAIStyleChatBackend:
    """OpenAI SDK-backed chat backend for local and openai providers."""

    provider: ProviderConfig
    settings: MemorySettings
    dependencies: ProviderRuntimeDependencies = field(default_factory=ProviderRuntimeDependencies)

    async def _resolve_base_url(self, operation: str) -> str:
        """Resolve the base URL for ``operation``, via the scheduler when one is configured.

        Takes NO prompt. It previously accepted a ``user_prompt`` it never read, and the obvious
        way to "use" that parameter is interpolating it into the scheduler ``task`` label below --
        which the scheduler logs and renders on a dashboard. That would be a prompt-content leak.
        The parameter is gone so it cannot be wired up; the label is built from ``operation`` alone.
        """
        fallback_base_url = self.provider.base_url
        if not should_use_scheduler(fallback_base_url):
            return fallback_base_url
        try:
            return await self.dependencies.scheduler_url_acquire(
                fallback=fallback_base_url,
                task=f"memory: llm {operation}",
                timeout_s=self.dependencies.request_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return fallback_base_url

    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        base_url = await self._resolve_base_url(operation)
        client = self.dependencies.openai_client_factory(
            base_url=base_url,
            api_key=self.provider.api_key,
            settings=self.settings,
            request_timeout_s=self.dependencies.request_timeout_s,
        )
        # CF-234: this backend announced nothing, so every `LLMAdapter` call it served was
        # invisible to both LLM budgets -- including the judge fan-out (3 calls per proposal per
        # extracted node) that CF-79 was filed to bound. `build_chat_backend` routes both LOCAL
        # and OPENAI here and `chat_provider` defaults to "local", so that was the default
        # configuration.
        #
        # `report_only=True` is deliberate and temporary: it makes the calls VISIBLE without
        # making the budget bind. Enforcing needs a landing zone first -- a refusal currently has
        # no handler and falls into `_process_episode`'s generic `except Exception`, which marks
        # the episode FAILED. Flip this to enforcing only together with that handler and a cap
        # calibrated on what this measurement reports.
        handle = start_llm_usage_call(
            kind="chat",
            model=self.provider.chat_model,
            endpoint="chat.completions.create",
            operation=operation,
            report_only=True,
        )
        try:
            response = await client.chat.completions.create(
                model=self.provider.chat_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except BaseException as exc:
            fail_llm_usage_call(handle, exc)
            raise
        complete_llm_usage_call(handle, result=response)
        choices = response.choices or []
        if not choices:
            return ""
        return choices[0].message.content or ""


@dataclass
class GeminiChatBackend:
    """Google Gemini REST backend for memory-processing chat tasks."""

    provider: ProviderConfig

    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        if not self.provider.api_key:
            raise ValueError("GEMINI_API_KEY is required when chat_provider=gemini.")

        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        handle = start_llm_usage_call(
            kind="chat",
            model=self.provider.chat_model,
            endpoint="models.generateContent",
            operation=operation,
            # Same surface as the OpenAI-style backend above, so the same mode. Leaving this one
            # enforcing would keep exactly the split CF-234 is about -- and its enforcement is
            # phantom anyway: CF-235 has `_chat_text` catch the refusal, retry it, and return
            # None, so no refusal on this path has ever reached an actor.
            report_only=True,
        )
        try:
            response = await _gemini_generate_content(
                base_url=self.provider.base_url,
                api_key=self.provider.api_key,
                model=self.provider.chat_model,
                payload=payload,
            )
        except Exception as exc:
            fail_llm_usage_call(handle, exc)
            raise
        complete_llm_usage_call(handle, usage=response.get("usageMetadata"))
        candidates = response.get("candidates") or []
        for candidate in candidates:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                text = part.get("text")
                if text:
                    return str(text)
        logger.warning("Gemini returned no text content.")
        return ""


@dataclass
class UnimplementedProviderChatBackend:
    """Placeholder backend for non-openai providers until SDK bridges are added."""

    provider: ProviderConfig

    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        raise NotImplementedError(
            f"{self.provider.kind.value} chat backend is scaffolded but not implemented yet."
        )


def _gemini_generate_content_sync(
    *,
    base_url: str,
    api_key: str,
    model: str,
    payload: dict[str, object],
) -> dict[str, object]:
    base = base_url.rstrip("/")
    encoded_model = urllib_parse.quote(model, safe="")
    url = f"{base}/models/{encoded_model}:generateContent"
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini request failed: {exc.code} {body}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Gemini request failed: {exc.reason}") from exc


async def _gemini_generate_content(
    *,
    base_url: str,
    api_key: str,
    model: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return await asyncio.to_thread(
        _gemini_generate_content_sync,
        base_url=base_url,
        api_key=api_key,
        model=model,
        payload=payload,
    )


def build_chat_backend(
    settings: MemorySettings,
    provider: ProviderConfig | None = None,
    dependencies: ProviderRuntimeDependencies | None = None,
) -> ChatBackend:
    provider = provider or ProviderConfig.for_chat(settings)
    if provider.kind in {ProviderKind.LOCAL, ProviderKind.OPENAI}:
        return OpenAIStyleChatBackend(
            provider=provider,
            settings=settings,
            dependencies=dependencies or ProviderRuntimeDependencies(),
        )
    if provider.kind is ProviderKind.GEMINI:
        return GeminiChatBackend(provider=provider)
    if provider.kind is ProviderKind.ANTHROPIC:
        return UnimplementedProviderChatBackend(provider=provider)
    raise ValueError(f"Unsupported provider kind: {provider.kind}")
