"""Authority is served only while its lane is on (#170, opt-out decision).

With a lane off, its stored Views or event assertions stop updating. Serving them as the current
answer would be false-current, so recall must not wire authority on unless the lane is on too.
Both RecallService construction sites are checked: the backend (core.bootstrap) and the hook CLI
(cli.bootstrap).
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from menhir.config import MemorySettings
from menhir.config.authority_gates import (
    event_history_authority_effective,
    scalar_view_authority_effective,
)

pytestmark = pytest.mark.unit

COMBOS = list(itertools.product([False, True], [False, True]))


def _settings(scalar_lane, scalar_auth, event_lane, event_auth):
    return MemorySettings(
        personal_memory_scalar_state_enabled=scalar_lane,
        personal_memory_scalar_view_authority_enabled=scalar_auth,
        personal_memory_event_history_enabled=event_lane,
        personal_memory_event_history_authority_enabled=event_auth,
    )


@pytest.mark.parametrize(("lane", "authority"), COMBOS)
def test_effective_authority_needs_both_flags(lane, authority):
    scalar = _settings(lane, authority, False, False)
    event = _settings(False, False, lane, authority)
    assert scalar_view_authority_effective(scalar) is (lane and authority)
    assert event_history_authority_effective(event) is (lane and authority)
    # The other lane's flags never leak across.
    assert event_history_authority_effective(scalar) is False
    assert scalar_view_authority_effective(event) is False


class _Captured(dict):
    pass


def _fake_recall(captured):
    class FakeRecallService:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    return FakeRecallService


class _Inert:
    def __init__(self, *args, **kwargs):
        pass


@pytest.mark.parametrize(("lane", "authority"), COMBOS)
def test_backend_wiring_requires_lane(monkeypatch, lane, authority):
    from menhir.core.bootstrap import build_memory_services

    class FakeGraphiti:
        @classmethod
        def from_settings_with_capabilities(cls, settings, **kwargs):
            return SimpleNamespace()

    class FakeLLM:
        @classmethod
        def from_settings(cls, settings):
            return SimpleNamespace()

    captured = _Captured()
    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", _Inert)
    monkeypatch.setattr("menhir.core.bootstrap.MemoryGraphAdapter", _Inert)
    monkeypatch.setattr("menhir.core.bootstrap.GraphitiClient", FakeGraphiti)
    monkeypatch.setattr("menhir.core.bootstrap.LLMAdapter", FakeLLM)
    monkeypatch.setattr("menhir.core.bootstrap.RecallService", _fake_recall(captured))

    build_memory_services(settings=_settings(lane, authority, lane, authority))

    assert captured["scalar_view_authority_enabled"] is (lane and authority)
    assert captured["event_history_authority_enabled"] is (lane and authority)


@pytest.mark.parametrize(("settings_kwargs", "expected"), [({}, False), ({"personal_memory_scalar_state_enabled": True}, True)])
def test_backend_wiring_with_default_authority_follows_lane(monkeypatch, settings_kwargs, expected):
    from menhir.core.bootstrap import build_memory_services

    class FakeGraphiti:
        @classmethod
        def from_settings_with_capabilities(cls, settings, **kwargs):
            return SimpleNamespace()

    class FakeLLM:
        @classmethod
        def from_settings(cls, settings):
            return SimpleNamespace()

    captured = _Captured()
    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", _Inert)
    monkeypatch.setattr("menhir.core.bootstrap.MemoryGraphAdapter", _Inert)
    monkeypatch.setattr("menhir.core.bootstrap.GraphitiClient", FakeGraphiti)
    monkeypatch.setattr("menhir.core.bootstrap.LLMAdapter", FakeLLM)
    monkeypatch.setattr("menhir.core.bootstrap.RecallService", _fake_recall(captured))

    build_memory_services(settings=MemorySettings(**settings_kwargs))

    assert captured["scalar_view_authority_enabled"] is expected


@pytest.mark.parametrize(("lane", "authority"), COMBOS)
def test_hook_cli_wiring_requires_lane(monkeypatch, lane, authority):
    from menhir.cli.bootstrap import build_hook_services

    class FakeGraphiti:
        @classmethod
        def from_settings(cls, settings):
            return SimpleNamespace()

    captured = _Captured()
    monkeypatch.setattr("menhir.cli.bootstrap.Neo4jRepository", _Inert)
    monkeypatch.setattr("menhir.cli.bootstrap.MemoryGraphAdapter", _Inert)
    monkeypatch.setattr("menhir.infrastructure.graphiti_client.GraphitiClient", FakeGraphiti)
    monkeypatch.setattr("menhir.services.recall_service.RecallService", _fake_recall(captured))
    monkeypatch.setattr("menhir.services.context_builder.ContextBuilderService", _Inert)

    services = build_hook_services(settings=_settings(lane, authority, lane, authority))

    assert services.context_builder is not None, "full recall path was not built"
    assert captured["scalar_view_authority_enabled"] is (lane and authority)
    assert captured["event_history_authority_enabled"] is (lane and authority)
