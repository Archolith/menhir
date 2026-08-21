"""CF-196: `_resolve_base_url` must not accept a prompt it does not read.

The parameter was unused, which is harmless on its own. It is recorded because of the near-miss:
the obvious way to "use" a `user_prompt` parameter here is interpolating it into the scheduler
`task` label -- which the scheduler logs and renders on a dashboard. That would be a
prompt-content leak. Deleting the parameter means it cannot be wired up later.

Asserted structurally, so the parameter cannot creep back in a future edit.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.providers import (
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
)

CANARY = "SECRET-PROMPT-TEXT"


def _backend(acquire: AsyncMock) -> OpenAIStyleChatBackend:
    return OpenAIStyleChatBackend(
        provider=ProviderConfig(
            kind=ProviderKind.LOCAL,
            base_url="http://localhost:1234/v1",
            api_key="test-key",
            chat_model="test-model",
        ),
        settings=MemorySettings.from_env(),
        dependencies=ProviderRuntimeDependencies(
            scheduler_url_acquire=acquire, request_timeout_s=0.5
        ),
    )


@pytest.mark.unit
def test_the_prompt_parameter_is_gone() -> None:
    params = inspect.signature(OpenAIStyleChatBackend._resolve_base_url).parameters

    assert "user_prompt" not in params
    assert not any("prompt" in name for name in params), f"prompt-like parameter present: {params}"
    # POSITIVE CONTROL: the parameter it legitimately needs is still there.
    assert "operation" in params


@pytest.mark.unit
def test_the_body_references_no_prompt_at_all() -> None:
    """A parameter can be removed while a prompt is still reachable through `self` or a closure.
    This reads the actual function source."""
    source = inspect.getsource(OpenAIStyleChatBackend._resolve_base_url)
    body = source.split('"""')[-1]  # drop the docstring, which discusses prompts by design

    assert "prompt" not in body.lower(), f"prompt referenced in body: {body}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_scheduler_task_label_carries_no_prompt_text() -> None:
    """The leak that was one edit away: the label the scheduler logs and renders."""
    acquire = AsyncMock(return_value="http://localhost:8082/v1/t/memory--llm-compression")
    backend = _backend(acquire)

    with patch("menhir.infrastructure.providers.should_use_scheduler", return_value=True):
        await backend._resolve_base_url("compression")

    label = acquire.await_args.kwargs["task"]
    assert CANARY not in label
    assert label == "memory: llm compression"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolution_still_works_through_the_scheduler() -> None:
    """POSITIVE CONTROL: without this, the assertions above would pass against a function that
    stopped resolving anything."""
    acquire = AsyncMock(return_value="http://localhost:8082/v1/t/memory--llm-compression")
    backend = _backend(acquire)

    with patch("menhir.infrastructure.providers.should_use_scheduler", return_value=True):
        resolved = await backend._resolve_base_url("compression")

    assert resolved == "http://localhost:8082/v1/t/memory--llm-compression"
    assert acquire.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_non_scheduler_path_still_returns_the_configured_base_url() -> None:
    acquire = AsyncMock()
    backend = _backend(acquire)

    with patch("menhir.infrastructure.providers.should_use_scheduler", return_value=False):
        resolved = await backend._resolve_base_url("compression")

    assert resolved == "http://localhost:1234/v1"
    acquire.assert_not_awaited()
