"""Flag off (the default): no resolver is built, nothing is called, edges are byte-identical."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.edges import EntityEdge  # noqa: E402

from menhir.config.settings_model import MemorySettings  # noqa: E402
import menhir.infrastructure.anchored_time as anchored_time  # noqa: E402
import menhir.infrastructure.anchored_time_resolver as resolver_module  # noqa: E402
import menhir.infrastructure.graphiti_client as graphiti_client  # noqa: E402
import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.providers import ProviderConfig  # noqa: E402

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 2, 14, 18, 30, tzinfo=timezone.utc)


def test_settings_default_off(monkeypatch) -> None:
    for name in ("MENHIR_ANCHORED_TIME_RESOLVER", "MENHIR_ANCHORED_TIME_MODEL", "MENHIR_ANCHORED_TIME_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    for settings in (MemorySettings(), MemorySettings.from_env()):
        assert settings.anchored_time_resolver_enabled is False
        assert settings.anchored_time_resolver_model == ""
        assert settings.anchored_time_resolver_timeout_s == 120.0


def test_settings_read_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_RESOLVER", "true")
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_MODEL", " gpt-6-luna ")
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_TIMEOUT_S", "12.5")
    settings = MemorySettings.from_env()
    assert settings.anchored_time_resolver_enabled is True
    assert settings.anchored_time_resolver_model == "gpt-6-luna"
    assert settings.anchored_time_resolver_timeout_s == 12.5


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_timeout_must_be_positive(value) -> None:
    with pytest.raises(ValueError, match="anchored_time_resolver_timeout_s"):
        MemorySettings(anchored_time_resolver_timeout_s=value)


def _captured_hook(monkeypatch, settings: MemorySettings, **kwargs):
    seen: dict = {}

    class _CapturingGraphiti:
        def __init__(self, **graphiti_kwargs) -> None:
            seen.update(graphiti_kwargs)

    monkeypatch.setattr(graphiti_client, "Graphiti", _CapturingGraphiti)
    graphiti_client.GraphitiClient.from_settings_with_capabilities(settings, **kwargs)
    hook = seen["single_episode_extraction_hook"]
    assert isinstance(hook, policy.MenhirExtractionHook)
    return hook


def test_client_builds_the_hook_without_a_resolver_by_default(monkeypatch) -> None:
    built: list = []
    monkeypatch.setattr(resolver_module, "AnchoredTimeResolver", lambda *a, **k: built.append(1))
    assert _captured_hook(monkeypatch, MemorySettings())._anchored_time is None
    assert built == []


def test_client_wires_the_resolver_only_when_enabled_and_llm_is_on(monkeypatch) -> None:
    on = MemorySettings(anchored_time_resolver_enabled=True, anchored_time_resolver_model="gpt-6-luna",
                        anchored_time_resolver_timeout_s=7.0)
    resolver = _captured_hook(monkeypatch, on)._anchored_time
    assert isinstance(resolver, resolver_module.AnchoredTimeResolver)
    assert resolver.model == "gpt-6-luna" and resolver._timeout_s == 7.0
    # Unwrapped client: a retrying wrapper here would nest a second 429 backoff layer.
    assert type(resolver._client).__name__ not in {"_ProviderExtrasAsyncClient", "ResilientChatClient"}
    assert resolver._base_url == ProviderConfig.for_graphiti_llm(on).base_url
    default_on = MemorySettings(anchored_time_resolver_enabled=True)
    default_model = _captured_hook(monkeypatch, default_on)._anchored_time
    assert default_model.model == ProviderConfig.for_graphiti_llm(default_on).chat_model
    assert _captured_hook(monkeypatch, on, llm_enabled=False)._anchored_time is None


@pytest.mark.asyncio
async def test_hook_without_resolver_never_touches_the_resolver_code(monkeypatch) -> None:
    def _edge(fact: str, uuid: str) -> EntityEdge:
        return EntityEdge(
            uuid=uuid, group_id="ns", source_node_uuid="n-user", target_node_uuid=f"n-{uuid}",
            created_at=SPEECH, name="RELATES_TO", fact=fact, episodes=["ep-1"], valid_at=SPEECH,
            reference_time=SPEECH, attributes={"note": "kept"},
        )

    edges = [_edge("The user moved to Denver.", "e0"), _edge("The user loves hiking.", "e1")]
    canned = [e.model_dump() for e in edges]

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        return [], edges, {}

    def _forbidden(*args, **kwargs):
        raise AssertionError("anchored-time code ran with the flag off")

    monkeypatch.setattr(policy, "extract_nodes_and_edges", fake_extract)
    monkeypatch.setattr(policy, "_apply_anchored_time", _forbidden)
    monkeypatch.setattr(anchored_time, "detect", _forbidden)
    monkeypatch.setattr(anchored_time, "plan_overlay", _forbidden)

    class _Clients:
        llm_client = object()

        def model_copy(self, *, update):
            return self

    context = SimpleNamespace(
        clients=_Clients(), episode=SimpleNamespace(uuid="g-1", valid_at=SPEECH), previous_episodes=[],
        entity_types=None, excluded_entity_types=None, edge_type_map={}, edge_types=None,
        custom_extraction_instructions=None,
    )
    receipt = policy.begin_extraction_receipt("ep-1", "user: I moved to Denver last month.")
    try:
        result = await policy.MenhirExtractionHook().extract_single_episode(context)
    finally:
        policy.clear_extraction_receipt()
    assert [e.model_dump() for e in result.edges] == canned
    assert receipt.anchored_time is None
