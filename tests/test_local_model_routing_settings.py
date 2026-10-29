"""Local-model routing settings (#399 item 3): anchored-time endpoint/extra_body and the
personal-memory chat extra_body. With every setting unset, request bodies, clients and base URLs
must be exactly what they were before these settings existed (no network)."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

from menhir.config.settings_model import MemorySettings, parse_extra_body  # noqa: E402
import menhir.infrastructure.anchored_time_attach_client as attach_module  # noqa: E402
import menhir.infrastructure.anchored_time_resolver as resolver_module  # noqa: E402
import menhir.infrastructure.graphiti_client as graphiti_client  # noqa: E402
from menhir.infrastructure.providers import ProviderConfig, ProviderKind  # noqa: E402

pytestmark = pytest.mark.unit

_ENV_NAMES = (
    "MENHIR_ANCHORED_TIME_BASE_URL",
    "MENHIR_ANCHORED_TIME_API_KEY",
    "MENHIR_ANCHORED_TIME_EXTRA_BODY",
    "MENHIR_PERSONAL_MEMORY_CHAT_EXTRA_BODY",
    "MENHIR_ANCHORED_TIME_MODEL",
    "MENHIR_ANCHORED_TIME_RESOLVER",
)


def _clear_env(monkeypatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# --- settings ---------------------------------------------------------------------------------


def test_defaults_are_empty(monkeypatch) -> None:
    _clear_env(monkeypatch)
    for s in (MemorySettings(), MemorySettings.from_env()):
        assert s.anchored_time_base_url == ""
        assert s.anchored_time_api_key == "not-needed"
        assert s.anchored_time_extra_body == ""
        assert s.personal_memory_consolidation_chat_extra_body == ""


def test_env_parsing_strips(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_MODEL", "qwen3")
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_BASE_URL", "  http://127.0.0.1:8080/v1  ")
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_API_KEY", "sekret-key")
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_EXTRA_BODY", ' {"a": 1} ')
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_CHAT_EXTRA_BODY", ' {"b": 2} ')
    s = MemorySettings.from_env()
    assert s.anchored_time_base_url == "http://127.0.0.1:8080/v1"
    assert s.anchored_time_api_key == "sekret-key"
    assert s.anchored_time_extra_body == '{"a": 1}'
    assert s.personal_memory_consolidation_chat_extra_body == '{"b": 2}'


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '"str"'])
def test_bad_extra_body_names_the_env_var(monkeypatch, bad) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ValueError, match="MENHIR_ANCHORED_TIME_EXTRA_BODY"):
        MemorySettings(anchored_time_extra_body=bad)
    with pytest.raises(ValueError, match="MENHIR_PERSONAL_MEMORY_CHAT_EXTRA_BODY"):
        MemorySettings(personal_memory_consolidation_chat_extra_body=bad)
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_EXTRA_BODY", bad)
    with pytest.raises(ValueError, match="MENHIR_ANCHORED_TIME_EXTRA_BODY"):
        MemorySettings.from_env()


def test_parse_extra_body_helper() -> None:
    assert parse_extra_body("", env_name="X") == {}
    assert parse_extra_body("   ", env_name="X") == {}
    assert parse_extra_body('{"a": {"b": 1}}', env_name="X") == {"a": {"b": 1}}


def test_base_url_requires_model_when_resolver_enabled() -> None:
    with pytest.raises(ValueError, match="MENHIR_ANCHORED_TIME_MODEL"):
        MemorySettings(anchored_time_resolver_enabled=True, anchored_time_base_url="http://x/v1")
    MemorySettings(
        anchored_time_resolver_enabled=True,
        anchored_time_base_url="http://x/v1",
        anchored_time_resolver_model="m",
    )
    MemorySettings(anchored_time_resolver_enabled=False, anchored_time_base_url="http://x/v1")


def test_repr_hides_anchored_time_api_key() -> None:
    s = MemorySettings(anchored_time_api_key="sekret-key-123")
    assert "sekret-key-123" not in repr(s)


# --- make_sync_chat ---------------------------------------------------------------------------


def _sync_chat_kwargs(monkeypatch, **chat_kwargs) -> dict:
    import openai

    from menhir.infrastructure import sync_llm

    class _Cfg:
        api_key = "k"
        chat_model = "global-model"
        base_url = "http://127.0.0.1:9099/v1"
        kind = ProviderKind.LOCAL

    seen: dict = {}

    class _Resp:
        usage = None

        class _Choice:
            class _Msg:
                content = "ok"

            message = _Msg()

        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            seen.update(kw)
            return _Resp()

    class _OpenAI:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr(ProviderConfig, "for_chat", classmethod(lambda cls, s: _Cfg()))
    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    chat = sync_llm.make_sync_chat(object(), **chat_kwargs)
    assert chat is not None and chat("sys", "usr") == "ok"
    return seen


def test_sync_chat_default_has_no_extra_body(monkeypatch) -> None:
    assert "extra_body" not in _sync_chat_kwargs(monkeypatch)
    assert "extra_body" not in _sync_chat_kwargs(monkeypatch, extra_body={})
    assert "extra_body" not in _sync_chat_kwargs(monkeypatch, extra_body=None)


def test_sync_chat_disable_reasoning_only(monkeypatch) -> None:
    seen = _sync_chat_kwargs(monkeypatch, disable_reasoning=True)
    assert seen["extra_body"] == {"reasoning": {"enabled": False}}


def test_sync_chat_extra_body_only(monkeypatch) -> None:
    body = {"chat_template_kwargs": {"enable_thinking": False}}
    assert _sync_chat_kwargs(monkeypatch, extra_body=body)["extra_body"] == body


def test_sync_chat_both_merge_extra_body_wins(monkeypatch) -> None:
    seen = _sync_chat_kwargs(
        monkeypatch,
        disable_reasoning=True,
        extra_body={"reasoning": {"enabled": True}, "other": 1},
    )
    assert seen["extra_body"] == {"reasoning": {"enabled": True}, "other": 1}


# --- resolver and attacher requests -----------------------------------------------------------


_FAKE_CLIENT = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=None)))


def _captured_request(monkeypatch, module, make, call) -> dict:
    seen: dict = {}

    async def _fake(create, request, *, base_url, label):
        seen.update(request)
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(module, "acreate_chat_completion", _fake)
    obj = make()
    asyncio.run(call(obj))
    return seen


def _resolver_request(monkeypatch, model: str, **kw) -> dict:
    return _captured_request(
        monkeypatch,
        resolver_module,
        lambda: resolver_module.AnchoredTimeResolver(
            _FAKE_CLIENT, base_url="http://127.0.0.1:9099/v1", model=model, timeout_s=5.0, **kw
        ),
        lambda r: r.resolve("I moved last month.", date(2024, 2, 14), ["The user moved."]),
    )


def _attach_request(monkeypatch, model: str, **kw) -> dict:
    return _captured_request(
        monkeypatch,
        attach_module,
        lambda: attach_module.AnchoredTimeAttacher(
            _FAKE_CLIENT, base_url="http://127.0.0.1:9099/v1", model=model, timeout_s=5.0, **kw
        ),
        lambda a: a.attach("I moved last month.", date(2024, 2, 14), ["The user moved."]),
    )


@pytest.mark.parametrize("runner", [_resolver_request, _attach_request])
def test_request_default_extra_body_unchanged(monkeypatch, runner) -> None:
    from menhir.infrastructure.graphiti_llm_adapter import _provider_extra_body

    plain = runner(monkeypatch, "plain-model")
    assert "extra_body" not in plain
    deepseek = runner(monkeypatch, "deepseek-chat")
    expected = _provider_extra_body("deepseek-chat", "http://127.0.0.1:9099/v1")
    assert expected, "deepseek profile should carry provider extras"
    assert deepseek["extra_body"] == expected


@pytest.mark.parametrize("runner", [_resolver_request, _attach_request])
def test_request_extra_body_merges_setting_wins(monkeypatch, runner) -> None:
    from menhir.infrastructure.graphiti_llm_adapter import _provider_extra_body

    only = runner(monkeypatch, "plain-model", extra_body={"k": 1})
    assert only["extra_body"] == {"k": 1}
    base = _provider_extra_body("deepseek-chat", "http://127.0.0.1:9099/v1")
    key = next(iter(base))
    merged = runner(monkeypatch, "deepseek-chat", extra_body={key: "override", "k": 1})
    assert merged["extra_body"] == {**base, key: "override", "k": 1}


def test_extra_body_is_copied() -> None:
    src = {"k": 1}
    r = resolver_module.AnchoredTimeResolver(object(), base_url="u", model="m", timeout_s=1.0, extra_body=src)
    src["k"] = 2
    assert r._extra_body == {"k": 1}


# --- graphiti_client wiring -------------------------------------------------------------------


def _build_hook(monkeypatch, settings: MemorySettings):
    seen: dict = {}
    calls: list[tuple[dict, object]] = []
    real_build = graphiti_client.build_async_openai_client

    def _recording_build(**kw):
        client = real_build(**kw)
        calls.append((kw, client))
        return client

    class _CapturingGraphiti:
        def __init__(self, *, edge_expiry_hook=None, preserved_edge_attributes=None, **graphiti_kwargs) -> None:
            seen.update(graphiti_kwargs)

    monkeypatch.setattr(graphiti_client, "Graphiti", _CapturingGraphiti)
    monkeypatch.setattr(graphiti_client, "build_async_openai_client", _recording_build)
    graphiti_client.GraphitiClient.from_settings_with_capabilities(settings)
    return seen["single_episode_extraction_hook"], calls


def test_wiring_unset_reuses_ingest_client_and_base_url(monkeypatch) -> None:
    _clear_env(monkeypatch)
    settings = MemorySettings(anchored_time_resolver_enabled=True)
    hook, calls = _build_hook(monkeypatch, settings)
    ingest_kw, ingest_client = calls[0]
    llama_base_url = ProviderConfig.for_graphiti_llm(settings).base_url
    assert ingest_kw["base_url"] == llama_base_url
    for obj in (hook._anchored_time, hook._anchored_time_attach):
        assert obj._client is ingest_client
        assert obj._base_url == llama_base_url
        assert obj._extra_body == {}
    assert not any(kw["base_url"] == "http://127.0.0.1:9099/v1" for kw, _ in calls)


def test_wiring_set_builds_separate_client(monkeypatch) -> None:
    _clear_env(monkeypatch)
    settings = MemorySettings(
        anchored_time_resolver_enabled=True,
        anchored_time_resolver_model="qwen3",
        anchored_time_base_url="http://127.0.0.1:9099/v1",
        anchored_time_api_key="local-key",
        anchored_time_extra_body='{"chat_template_kwargs": {"enable_thinking": false}}',
    )
    hook, calls = _build_hook(monkeypatch, settings)
    ingest_client = calls[0][1]
    at_calls = [(kw, c) for kw, c in calls if kw["base_url"] == "http://127.0.0.1:9099/v1"]
    assert len(at_calls) == 1
    at_kw, at_client = at_calls[0]
    assert at_kw["api_key"] == "local-key"
    assert at_client is not ingest_client
    for obj in (hook._anchored_time, hook._anchored_time_attach):
        assert obj._client is at_client
        assert obj._base_url == "http://127.0.0.1:9099/v1"
        assert obj._extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def _sync_chat_client_kwargs(monkeypatch, **chat_kwargs) -> dict:
    import openai

    from menhir.infrastructure import sync_llm

    class _Cfg:
        api_key = "k"
        chat_model = "global-model"
        base_url = "http://127.0.0.1:9099/v1"
        kind = ProviderKind.LOCAL

    init: dict = {}

    class _OpenAI:
        def __init__(self, **kw):
            init.update(kw)
            resp = type("R", (), {"usage": None, "choices": [
                type("Ch", (), {"message": type("M", (), {"content": "ok"})()})()]})()
            self.chat = type("C", (), {"completions": type("P", (), {"create": lambda _s, **k: resp})()})()

    monkeypatch.setattr(ProviderConfig, "for_chat", classmethod(lambda cls, s: _Cfg()))
    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    chat = sync_llm.make_sync_chat(object(), **chat_kwargs)
    assert chat is not None and chat("sys", "usr") == "ok"
    return init


def test_sync_chat_timeout_default_and_override(monkeypatch) -> None:
    from menhir.infrastructure.providers import DEFAULT_REQUEST_TIMEOUT_S

    assert _sync_chat_client_kwargs(monkeypatch)["timeout"] == DEFAULT_REQUEST_TIMEOUT_S
    kw = _sync_chat_client_kwargs(monkeypatch, timeout_s=900)
    assert kw["timeout"] == 900.0 and kw["max_retries"] == 0


def test_chat_timeout_setting(monkeypatch) -> None:
    _clear_env(monkeypatch)
    assert MemorySettings().personal_memory_consolidation_chat_timeout_s == 0.0
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_CHAT_TIMEOUT_S", "900")
    assert MemorySettings.from_env().personal_memory_consolidation_chat_timeout_s == 900.0
    with pytest.raises(ValueError, match="chat_timeout_s"):
        MemorySettings(personal_memory_consolidation_chat_timeout_s=-1)
