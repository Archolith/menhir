"""Scalar lane is evidence-only (issue #95).

Discovery/batch/cursor must read the :TurnEvidence repository unconditionally — the legacy
Episodic `user:`-prefix fallback is removed. Also covers the one-per-process warning fired by
`select_scalar_targets` when the graph holds no user evidence at all.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services import scalar_consolidation as sc


class _PassiveNeo4j:
    def execute(self, query, params=None):  # pragma: no cover - never called
        raise AssertionError("unexpected Neo4j execute: " + query)


class _SpyLegacyRepo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(
                f"legacy PersonalMemoryRepository.{name} must not be called for the scalar lane"
            )

        return _record


class _FakeEvidenceRepo:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.calls: list[str] = []

    def evidence_exists(self) -> bool:
        return self.exists

    def list_scalar_dirty_evidence_namespaces(self, *, perceiver_version, limit):
        self.calls.append("list")
        assert (perceiver_version, limit) == ("v7", 3)
        return ["ns-a"]

    def load_next_scalar_evidence_batch(self, namespace, *, perceiver_version, limit):
        self.calls.append("load")
        assert (namespace, perceiver_version, limit) == ("ns-a", "v7", 5)
        return [{"uuid": "t1", "cursor_at": "2026-01-01T00:00:00Z",
                 "valid_at": "2026-01-01T00:00:00Z", "content": "hello"}]

    def advance_scalar_cursor_proxy(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError


def _adapter(exists: bool) -> tuple[MemoryGraphAdapter, _FakeEvidenceRepo, _SpyLegacyRepo]:
    adapter = MemoryGraphAdapter(_PassiveNeo4j())
    evidence = _FakeEvidenceRepo(exists)
    legacy = _SpyLegacyRepo()
    adapter._turn_evidence = evidence
    adapter._personal_memory = legacy
    return adapter, evidence, legacy


class _WarningGraphStub:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.evidence_calls = 0

    def evidence_exists(self) -> bool:
        self.evidence_calls += 1
        return self.exists

    def list_scalar_dirty_namespaces(self, *, perceiver_version, limit):
        return []


@pytest.fixture(autouse=True)
def _reset_warn_flag():
    sc._warned_no_scalar_evidence = False
    yield
    sc._warned_no_scalar_evidence = False


@pytest.mark.unit
def test_scalar_discovery_uses_evidence_repo_even_without_evidence():
    adapter, evidence, legacy = _adapter(exists=False)
    out = adapter.list_scalar_dirty_namespaces(perceiver_version="v7", limit=3)
    assert out == ["ns-a"]
    assert evidence.calls == ["list"]
    assert legacy.calls == []


@pytest.mark.unit
def test_scalar_batch_uses_evidence_repo_even_without_evidence():
    adapter, evidence, legacy = _adapter(exists=False)
    rows = adapter.load_next_scalar_batch("ns-a", perceiver_version="v7", limit=5)
    assert rows and rows[0]["uuid"] == "t1"
    assert evidence.calls == ["load"]
    assert legacy.calls == []


@pytest.mark.unit
def test_scalar_cursor_uses_shared_watermark_store():
    """advance_scalar_cursor stays on the :ScalarConsolidationWatermark store that the evidence
    discovery/batch queries read (PersonalMemoryRepository.advance_scalar_cursor)."""
    adapter = MemoryGraphAdapter(_PassiveNeo4j())
    recorded: dict = {}

    class _CursorRepo:
        def advance_scalar_cursor(self, namespace, **kwargs):
            recorded["ns"] = namespace
            recorded.update(kwargs)

    adapter._personal_memory = _CursorRepo()
    adapter._turn_evidence = _FakeEvidenceRepo(True)
    adapter.advance_scalar_cursor(
        "ns-a", cursor_at="2026-01-02T00:00:00Z", cursor_uuid="t2",
        perceiver_version="v7", at="2026-01-03T00:00:00Z")
    assert recorded["ns"] == "ns-a"
    assert recorded["cursor_at"] == "2026-01-02T00:00:00Z"
    assert recorded["cursor_uuid"] == "t2"
    assert recorded["perceiver_version"] == "v7"


@pytest.mark.unit
def test_warning_fires_once_when_no_evidence(caplog):
    stub = _WarningGraphStub(exists=False)
    with caplog.at_level(logging.WARNING, logger="menhir.services.scalar_consolidation"):
        first = asyncio.run(sc.select_scalar_targets(stub, namespaces=None,
                                                     perceiver_version="v1", max_namespaces=10))
        second = asyncio.run(sc.select_scalar_targets(stub, namespaces=None,
                                                      perceiver_version="v1", max_namespaces=10))
    assert first == [] and second == []
    warnings = [r for r in caplog.records if "no role='user' TurnEvidence" in r.message]
    assert len(warnings) == 1
    assert "add_memory(user_statement=...)" in warnings[0].message
    assert "TurnEvidence producer hook" in warnings[0].message
    # Once-per-process: after the first warn the flag short-circuits further checks.
    assert stub.evidence_calls == 1


@pytest.mark.unit
def test_no_warning_when_evidence_exists(caplog):
    stub = _WarningGraphStub(exists=True)
    with caplog.at_level(logging.WARNING, logger="menhir.services.scalar_consolidation"):
        asyncio.run(sc.select_scalar_targets(stub, namespaces=None,
                                             perceiver_version="v1", max_namespaces=10))
    assert not [r for r in caplog.records if "no role='user' TurnEvidence" in r.message]


@pytest.mark.unit
def test_warning_path_never_raises(caplog):
    class _Boom:
        def evidence_exists(self) -> bool:
            raise RuntimeError("boom")

        def list_scalar_dirty_namespaces(self, *, perceiver_version, limit):
            return []

    with caplog.at_level(logging.ERROR, logger="menhir.services.scalar_consolidation"):
        out = asyncio.run(sc.select_scalar_targets(_Boom(), namespaces=None,
                                                   perceiver_version="v1", max_namespaces=10))
    assert out == []
