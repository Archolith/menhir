"""CF-189: the chat backends disagree on what "the model produced nothing" means.

**The defect.** `ChatBackend.create_chat_completion` documented only "Return plain text chat
completion output." The implementations then diverged on the empty case: the OpenAI-style
backend returned `""` while the Gemini backend raised `RuntimeError("Gemini returned no text
content.")`. The same model behaviour therefore produced different control flow depending on
which provider was configured -- and `confirm_same_entity` is the k=3 unanimous judge on the
merge path, so which branch runs is not cosmetic. Separately, `response.choices[0]` was
unguarded: a valid HTTP 200 with `{"choices": []}` raised `IndexError` instead of a domain value.

**The contract, now written into the Protocol docstring:** an empty or refused completion
returns `""` and implementations must not raise for that case. The Gemini backend now logs at
warning level (the signal must survive) and returns `""`. `UnimplementedProviderChatBackend`
still raises `NotImplementedError` -- that is "there is no model", not "the model produced
nothing", and is deliberately left alone.

The positive controls are load-bearing: a change that returned `""` unconditionally would pass
every empty-case test here, and a change that swallowed transport failures would make the fix
fail-open.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.providers import (
    ChatBackend,
    GeminiChatBackend,
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
    UnimplementedProviderChatBackend,
)

pytestmark = [pytest.mark.unit]


def _openai_provider() -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.LOCAL,
        base_url="http://fake.invalid/v1",
        api_key="k",
        chat_model="fake-model",
    )


def _gemini_provider() -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.GEMINI,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="k",
        chat_model="gemini-2.5-flash",
    )


def _openai_backend(create: AsyncMock) -> OpenAIStyleChatBackend:
    def factory(**kwargs: object) -> object:
        completions = SimpleNamespace(create=create)
        chat = SimpleNamespace(completions=completions)
        return SimpleNamespace(chat=chat)

    return OpenAIStyleChatBackend(
        provider=_openai_provider(),
        settings=MemorySettings(),
        dependencies=ProviderRuntimeDependencies(openai_client_factory=factory),
    )


def _gemini_backend() -> GeminiChatBackend:
    return GeminiChatBackend(provider=_gemini_provider())


async def _call(backend: object) -> str:
    return await backend.create_chat_completion(
        system_prompt="sys",
        user_prompt="user",
        operation="compression",
        max_tokens=64,
        temperature=0.3,
    )


def _empty_openai_choices() -> SimpleNamespace:
    return SimpleNamespace(choices=[])


def _empty_gemini_candidates() -> dict[str, object]:
    return {"candidates": []}


# A third implementation must join the contract here: to pass it has to return "" too.
_EMPTY_DRIVERS = {
    "openai": lambda: _call(
        _openai_backend(AsyncMock(return_value=_empty_openai_choices()))
    ),
    "gemini": lambda: _call_with_patched_gemini(_empty_gemini_candidates()),
}


async def _call_with_patched_gemini(response: dict[str, object]) -> str:
    with patch(
        "menhir.infrastructure.providers._gemini_generate_content",
        new=AsyncMock(return_value=response),
    ):
        return await _call(_gemini_backend())


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", list(_EMPTY_DRIVERS.values()), ids=list(_EMPTY_DRIVERS.keys()))
async def test_empty_completion_returns_empty_string_on_every_backend(driver) -> None:
    """The finding, as a parity test: an empty completion is `""` on BOTH backends."""
    result = await driver()

    assert result == ""


@pytest.mark.asyncio
async def test_openai_empty_choices_returns_empty_string_without_raising() -> None:
    """A valid HTTP 200 with `{"choices": []}` must not IndexError."""
    backend = _openai_backend(AsyncMock(return_value=_empty_openai_choices()))

    result = await _call(backend)

    assert result == ""


@pytest.mark.asyncio
async def test_openai_present_but_empty_content_still_returns_empty_string() -> None:
    """Preserve the existing `or ""` behaviour for a present-but-empty `content`."""
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
    backend = _openai_backend(AsyncMock(return_value=response))

    result = await _call(backend)

    assert result == ""


@pytest.mark.asyncio
async def test_gemini_empty_case_still_logs_warning(caplog) -> None:
    """The signal must survive the change: an empty completion is still visible to an operator."""
    with caplog.at_level("WARNING", logger="menhir.infrastructure.providers"):
        result = await _call_with_patched_gemini(_empty_gemini_candidates())

    assert result == ""
    assert any(
        "returned no text content" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


@pytest.mark.asyncio
async def test_unimplemented_backend_still_raises_not_implemented_error() -> None:
    """`NotImplementedError` means "there is no model", not "the model produced nothing"."""
    backend = UnimplementedProviderChatBackend(
        provider=ProviderConfig(
            kind=ProviderKind.ANTHROPIC,
            base_url="",
            api_key="",
            chat_model="",
        )
    )

    with pytest.raises(NotImplementedError):
        await _call(backend)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "driver,expected",
    [
        ("openai", "ok"),
        ("gemini", "gemini ok"),
    ],
)
async def test_normal_completion_returns_text_unchanged_on_every_backend(driver, expected) -> None:
    """POSITIVE CONTROL: a change that returned `""` unconditionally would fail here."""
    if driver == "openai":
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        result = await _call(_openai_backend(AsyncMock(return_value=response)))
    else:
        result = await _call_with_patched_gemini(
            {"candidates": [{"content": {"parts": [{"text": "gemini ok"}]}}]}
        )

    assert result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["openai", "gemini"])
async def test_transport_failure_still_propagates_on_every_backend(driver) -> None:
    """POSITIVE CONTROL: the contract is about an empty RESULT, not about swallowing errors.

    A transport-level failure (connection error, HTTP 500) must still raise -- otherwise the
    empty-case fix becomes fail-open.
    """
    if driver == "openai":
        create = AsyncMock(side_effect=RuntimeError("connection reset"))
        backend = _openai_backend(create)
        with pytest.raises(RuntimeError, match="connection reset"):
            await _call(backend)
    else:
        with patch(
            "menhir.infrastructure.providers._gemini_generate_content",
            new=AsyncMock(side_effect=RuntimeError("Gemini request failed: 500 oops")),
        ):
            with pytest.raises(RuntimeError, match="500"):
                await _call(_gemini_backend())


@pytest.mark.unit
def test_protocol_docstring_states_the_empty_case_contract() -> None:
    """The next implementer must read the contract rather than guess."""
    doc = inspect.getdoc(ChatBackend.create_chat_completion)

    assert doc is not None
    assert '"' in doc, "docstring must state that an empty completion returns the empty string"
    assert "empty" in doc.lower()
    assert "must not" in doc.lower() or "raise" in doc.lower()
