"""CF-196: `_resolve_base_url` must not accept a prompt it does not read.

The parameter was unused, which is harmless on its own. It is recorded because of the near-miss:
the obvious way to "use" a `user_prompt` parameter is interpolating it into a request label that
an external service logs -- a prompt-content leak. Deleting the parameter means it cannot be wired
up later.

Asserted structurally, so the parameter cannot creep back in a future edit.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.providers import (
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
)


def _backend() -> OpenAIStyleChatBackend:
    return OpenAIStyleChatBackend(
        provider=ProviderConfig(
            kind=ProviderKind.LOCAL,
            base_url="http://localhost:1234/v1",
            api_key="test-key",
            chat_model="test-model",
        ),
        settings=MemorySettings.from_env(),
        dependencies=ProviderRuntimeDependencies(request_timeout_s=0.5),
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
async def test_resolve_base_url_returns_the_configured_base_url() -> None:
    backend = _backend()

    resolved = await backend._resolve_base_url("compression")

    assert resolved == "http://localhost:1234/v1"
