"""CF-305: a graphiti_core without the fork's hook parameters fails with a clear, named fix.

Erasure P4: a fork whose ``add_episode`` lacks the publish fence (an older fork) is
refused the same way, before any publish."""

from __future__ import annotations

import pytest

from menhir.infrastructure import graphiti_client
from menhir.infrastructure.graphiti_client import (
    FORK_HOOK_PARAMETERS,
    FORK_PUBLISH_PARAMETERS,
    require_graphiti_fork_hooks,
)


class _UpstreamGraphiti:
    def __init__(self, uri=None, user=None, password=None, llm_client=None, embedder=None):
        pass


class _ForkGraphiti:
    def __init__(
        self,
        uri=None,
        single_episode_extraction_hook=None,
        identity_gate_hook=None,
        candidate_filter_hook=None,
        node_pre_resolution_hook=None,
    ):
        pass

    async def add_episode(self, name, episode_body, *, publish_fence=None, new_episode_uuid=None):
        pass


class _Post2ForkGraphiti(_ForkGraphiti):
    """The 0.31.2.post2 fork: every constructor hook, no publish fence."""

    async def add_episode(self, name, episode_body, *, update_communities=False):
        pass


class _PartialForkGraphiti:
    def __init__(self, uri=None, single_episode_extraction_hook=None):
        pass


@pytest.mark.unit
def test_upstream_graphiti_gets_clear_error_naming_the_fix() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        require_graphiti_fork_hooks(_UpstreamGraphiti)
    message = str(excinfo.value)
    assert "reinstall" in message
    assert "archolith-graphiti-core" in message
    for name in FORK_HOOK_PARAMETERS:
        assert name in message


@pytest.mark.unit
def test_partial_fork_names_only_the_missing_hooks() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        require_graphiti_fork_hooks(_PartialForkGraphiti)
    message = str(excinfo.value)
    assert "identity_gate_hook" in message
    assert "single_episode_extraction_hook" not in message


@pytest.mark.unit
def test_unreadable_signature_fails_closed() -> None:
    class _Unreadable:
        __init__ = property(lambda self: (_ for _ in ()).throw(TypeError("no signature")))

    with pytest.raises(RuntimeError):
        require_graphiti_fork_hooks(_Unreadable)


@pytest.mark.unit
def test_fork_with_hooks_passes() -> None:
    require_graphiti_fork_hooks(_ForkGraphiti)


@pytest.mark.unit
def test_installed_graphiti_has_the_fork_hooks() -> None:
    require_graphiti_fork_hooks(graphiti_client.Graphiti)


def _stub_from_settings(monkeypatch: pytest.MonkeyPatch, graphiti_cls: type) -> object:
    from menhir.config import MemorySettings

    class _Anything:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    for name in (
        "LLMConfig",
        "MenhirOpenAIGenericClient",
        "OpenAIEmbedderConfig",
        "OpenAIEmbedder",
        "OpenAIRerankerClient",
        "Neo4jDriver",
        "ResilientChatClient",
    ):
        monkeypatch.setattr(graphiti_client, name, _Anything)
    monkeypatch.setattr(graphiti_client, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client, "Graphiti", graphiti_cls)
    monkeypatch.setattr(graphiti_client, "build_async_openai_client", lambda **kw: object())
    settings = MemorySettings(
        neo4j_uri="bolt://db:7687",
        local_llm_base_url="http://local-llm:1234/v1",
        local_llm_api_key="k",
        local_llm_chat_model="c",
        local_llm_embed_model="e",
    )
    return graphiti_client.GraphitiClient.from_settings(settings)


@pytest.mark.unit
def test_from_settings_with_upstream_graphiti_raises_the_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="reinstall"):
        _stub_from_settings(monkeypatch, _UpstreamGraphiti)


@pytest.mark.unit
def test_from_settings_with_fork_hooks_proceeds_to_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict] = []

    class _FullFork:
        def __init__(
            self,
            uri=None,
            user=None,
            password=None,
            graph_driver=None,
            llm_client=None,
            embedder=None,
            cross_encoder=None,
            single_episode_extraction_hook=None,
            identity_gate_hook=None,
            candidate_filter_hook=None,
            node_pre_resolution_hook=None,
            edge_expiry_hook=None,
            edge_invalidation_hook=None,
            preserved_edge_attributes=None,
        ):
            built.append({"identity_gate_hook": identity_gate_hook})

        async def add_episode(self, name, episode_body, *, publish_fence=None,
                              new_episode_uuid=None):
            pass

    wrapper = _stub_from_settings(monkeypatch, _FullFork)
    assert wrapper is not None
    assert built and built[0]["identity_gate_hook"] is not None


@pytest.mark.unit
def test_fork_without_the_publish_fence_is_refused_naming_the_fix() -> None:
    """Fails if construction accepts a fork that would publish without the erasure fence."""
    with pytest.raises(RuntimeError) as excinfo:
        require_graphiti_fork_hooks(_Post2ForkGraphiti)
    message = str(excinfo.value)
    for name in FORK_PUBLISH_PARAMETERS:
        assert f"add_episode({name})" in message
    assert "reinstall" in message and "archolith-graphiti-core==" in message
    assert "identity_gate_hook" not in message


@pytest.mark.unit
def test_missing_or_unreadable_add_episode_fails_closed() -> None:
    """Fails if an absent or unreadable add_episode is treated as fence-capable."""

    class _NoAddEpisode(_ForkGraphiti):
        add_episode = None

    class _UnreadableAddEpisode(_ForkGraphiti):
        add_episode = property(lambda self: (_ for _ in ()).throw(TypeError("no signature")))

    for cls in (_NoAddEpisode, _UnreadableAddEpisode):
        with pytest.raises(RuntimeError, match="publish_fence"):
            require_graphiti_fork_hooks(cls)


@pytest.mark.unit
def test_from_settings_with_a_post2_fork_raises_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup path: from_settings refuses the post2 fork, so bootstrap degrades Graphiti
    (no enrichment, no publish) instead of running unfenced."""
    with pytest.raises(RuntimeError, match="publish_fence"):
        _stub_from_settings(monkeypatch, _Post2ForkGraphiti)
