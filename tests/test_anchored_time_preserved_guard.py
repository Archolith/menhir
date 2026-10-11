"""P3c follow-up: a Graphiti whose constructor cannot be inspected counts as lacking
``preserved_edge_attributes`` -- startup refuses with expiry on, like the edge-expiry hook guard.
"""
from __future__ import annotations

import logging

import pytest

import menhir.infrastructure.graphiti_client as graphiti_client

pytestmark = pytest.mark.unit


class _Graphiti:
    def __init__(self, uri=None, *, edge_expiry_hook=None, preserved_edge_attributes=None):
        pass


def _uninspectable(monkeypatch) -> None:
    def _raise(obj, *args, **kwargs):
        raise ValueError("no signature found")

    monkeypatch.setattr(graphiti_client.inspect, "signature", _raise)


def test_uninspectable_constructor_refuses_with_expiry_on(monkeypatch) -> None:
    _uninspectable(monkeypatch)
    for resolver in (False, True):
        with pytest.raises(RuntimeError, match=r"archolith-graphiti-core==0\.30\.2\.post"):
            graphiti_client.preserved_edge_attributes_kwargs(resolver, True, _Graphiti)


def test_uninspectable_constructor_warns_with_only_the_resolver(monkeypatch, caplog) -> None:
    _uninspectable(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=graphiti_client.logger.name):
        assert graphiti_client.preserved_edge_attributes_kwargs(True, False, _Graphiti) == {}
    assert "preserved_edge_attributes" in caplog.text


def test_uninspectable_constructor_with_both_flags_off_passes_nothing(monkeypatch) -> None:
    _uninspectable(monkeypatch)
    assert graphiti_client.preserved_edge_attributes_kwargs(False, False, _Graphiti) == {}


def test_uninspectable_constructor_matches_the_edge_expiry_hook_guard(monkeypatch) -> None:
    _uninspectable(monkeypatch)
    with pytest.raises(RuntimeError, match="edge_expiry_hook"):
        graphiti_client.edge_expiry_hook_kwargs(True, _Graphiti)
