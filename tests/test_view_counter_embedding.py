"""Counter-bridge policy + view-embedder provider fallback.

The telemetry bridges (failure/instability) now write :Metric instrumentation through the
MetricWriteCoordinator saga, NOT embedded :Entity counters -- Metrics never rank in recall, so there
is nothing to embed. The former "bridge embeds its retrieval surface" tests are therefore gone;
embedding is now exercised only on the FACT path (event_fold / verifier_sync), whose tests live with
those modules. What remains here: the instability bridge must still exclude lifecycle churn from the
fold, and make_view_embedder must degrade to None on an unsupported provider.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.services.instability_counter_bridge import LIFECYCLE_FIELDS, sync_instability_counters


class _RevisionStore:
    def __init__(self):
        self.seen_exclude = None

    def fold_revisions_bounded(self, *, cutoff_id=None, after_id=0, min_count=2, exclude_fields=()):
        self.seen_exclude = exclude_fields
        return {"cutoff_id": 10, "groups": [
            {"node_uuid": "n1", "field": "status", "count": 2, "row_ids": [1, 2], "latest_value": "x"},
        ]}


class _CoordinatorSpy:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def record_telemetry_absolute(self, **kwargs):
        self.calls.append(kwargs)
        return {"uuid": "m", "view_key": "k", "op_id": "op"}


@pytest.mark.unit
def test_instability_fold_excludes_lifecycle_scope_by_default() -> None:
    """The instability bridge folds belief corrections, not lifecycle churn: it must pass the
    scope-excluding policy down to the fold so namespace promotions never become counters."""
    store = _RevisionStore()
    sync_instability_counters(store=store, coordinator=_CoordinatorSpy())

    assert "scope" in LIFECYCLE_FIELDS
    assert store.seen_exclude == LIFECYCLE_FIELDS


@pytest.mark.unit
def test_instability_bridge_writes_metric_via_coordinator() -> None:
    """The bridge routes to record_telemetry_absolute (:Metric saga), not record_counter."""
    coord = _CoordinatorSpy()
    sync_instability_counters(store=_RevisionStore(), coordinator=coord, namespace="ns")

    assert len(coord.calls) == 1
    call = coord.calls[0]
    assert call["source_table"] == "memory_revisions"
    assert call["counter"] == "status_revised"
    assert call["absolute_count"] == 2.0
    assert call["row_ids"] == [1, 2]


@pytest.mark.unit
def test_make_view_embedder_returns_none_for_unsupported_provider(monkeypatch) -> None:
    """No OpenAI-compatible embed provider -> None (fact counters BM25-only), not an error."""
    from menhir.infrastructure import view_embedder

    class _Provider:
        kind = type("K", (), {"value": "local-non-openai"})()
        embed_model = ""

        def supports_graphiti_openai_contract(self):
            return False

    monkeypatch.setattr(
        view_embedder.ProviderConfig, "for_graphiti_embedder",
        classmethod(lambda cls, settings: _Provider()),
    )
    assert view_embedder.make_view_embedder(object()) is None


@pytest.mark.unit
def test_make_view_embedder_records_provider_reported_tokens(monkeypatch) -> None:
    from menhir.infrastructure import view_embedder
    from menhir.infrastructure.observability import (
        LLMUsageEvent,
        reset_llm_usage_callback,
        set_llm_usage_callback,
    )

    class _Provider:
        kind = type("K", (), {"value": "openai"})()
        embed_model = "embed-test"
        api_key = "test"
        base_url = "http://localhost:1234/v1"

        def supports_graphiti_openai_contract(self):
            return True

    class _Embeddings:
        def create(self, **kwargs):
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2])],
                usage=SimpleNamespace(prompt_tokens=3, total_tokens=3),
            )

    class _OpenAI:
        def __init__(self, **kwargs):
            self.embeddings = _Embeddings()

    monkeypatch.setattr(
        view_embedder.ProviderConfig,
        "for_graphiti_embedder",
        classmethod(lambda cls, settings: _Provider()),
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_OpenAI))

    seen: list[LLMUsageEvent] = []
    token = set_llm_usage_callback(seen.append)
    try:
        embed = view_embedder.make_view_embedder(object())
        assert embed is not None
        assert embed("hello") == [0.1, 0.2]
    finally:
        reset_llm_usage_callback(token)

    assert [event.phase for event in seen] == ["started", "completed"]
    assert seen[1].operation == "view_embed"
    assert seen[1].input_tokens == 3
    assert seen[1].total_tokens == 3
