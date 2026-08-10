"""A synchronous chat seam for background jobs that need an `(system, user) -> str` callable.

The main LLM path (`LLMAdapter`) is async, but the perception consolidation runs synchronously on a
worker thread (`asyncio.to_thread`) and its `llm_complete` contract is sync. Rather than bridge the
async adapter per call, build a small synchronous OpenAI-compatible client from the chat provider
config (works for OpenAI and any OpenAI-compatible endpoint: local llama-server, etc.). Temperature
defaults > 0 because perception's k-sample self-consistency needs sampling diversity to be meaningful.
"""

from __future__ import annotations

import logging
from typing import Callable

from menhir.config import MemorySettings
from menhir.infrastructure.observability import (
    complete_llm_usage_call,
    fail_llm_usage_call,
    start_llm_usage_call,
)
from menhir.infrastructure.providers import ProviderConfig

logger = logging.getLogger(__name__)

SyncChat = Callable[[str, str], str]


def make_sync_chat(
    settings: MemorySettings, *, temperature: float = 0.7, max_tokens: int = 512,
    model: str | None = None,
) -> SyncChat | None:
    """Return a sync `(system, user) -> str` chat callable, or None when no usable chat provider is
    configured (missing api key / model). None lets callers disable the job rather than crash.
    `model` overrides only the model name (same provider api_key/base_url) — used to run a job on a
    stronger model than the global chat model without touching the rest of the stack."""
    cfg = ProviderConfig.for_chat(settings)
    if not cfg.api_key or not cfg.chat_model:
        logger.info("sync chat seam unavailable: no api_key/chat_model for the configured provider")
        return None

    try:
        from openai import OpenAI
    except Exception:  # pragma: no cover - openai is a hard dep in prod
        logger.warning("sync chat seam unavailable: openai client not importable")
        return None

    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None)
    model = model or cfg.chat_model

    def _complete(system: str, user: str) -> str:
        handle = start_llm_usage_call(
            kind="chat",
            model=model,
            endpoint="chat.completions.create",
            operation="sync_chat",
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            fail_llm_usage_call(handle, exc)
            raise
        complete_llm_usage_call(handle, result=resp)
        return (resp.choices[0].message.content or "").strip()

    return _complete
