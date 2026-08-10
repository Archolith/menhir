"""Unit tests for CandidateService (approve/reject of CANDIDATE-tier nodes)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.services.candidate_service import CandidateService


@dataclass
class _FakeAdapter:
    """Records candidate-transition calls; behaviour driven by attributes."""

    candidate: dict[str, Any] | None = None
    promote_result: bool = True
    reject_result: bool = True
    calls: list[str] = field(default_factory=list)

    def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        self.calls.append(f"fetch:{uuid}")
        return self.candidate

    def promote_candidate(self, uuid: str) -> bool:
        self.calls.append(f"promote:{uuid}")
        return self.promote_result

    def reject_candidate(self, uuid: str) -> bool:
        self.calls.append(f"reject:{uuid}")
        return self.reject_result

    def list_candidates(self, *, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.calls.append(f"list:{source}:{limit}")
        return [{"uuid": "c1"}]


@dataclass
class _FakeLifecycle:
    """Stub LifecycleService exposing only the contradiction-check seam."""

    conflicts: int = 0
    raise_error: bool = False
    batches: list[list[dict[str, Any]]] = field(default_factory=list)

    async def _check_contradictions_batch(self, candidates: list[dict[str, Any]]) -> int:
        self.batches.append(candidates)
        if self.raise_error:
            raise RuntimeError("search down")
        return self.conflicts


def _service(adapter: _FakeAdapter, lifecycle: _FakeLifecycle) -> CandidateService:
    return CandidateService(graph_adapter=adapter, lifecycle_service=lifecycle)  # type: ignore[arg-type]


@pytest.mark.unit
def test_approve_promotes_then_runs_contradiction_check() -> None:
    adapter = _FakeAdapter(candidate={"uuid": "c1", "content": "x", "name": "n", "cluster_id": "P1"})
    lifecycle = _FakeLifecycle(conflicts=2)
    result = asyncio.run(_service(adapter, lifecycle).approve("c1"))

    assert result["status"] == "approved"
    assert result["scope"] == "PERSISTENT"
    assert result["conflicts_detected"] == 2
    assert result["cluster_id"] == "P1"
    # Order matters: promote happens before the contradiction check.
    assert adapter.calls == ["fetch:c1", "promote:c1"]
    assert lifecycle.batches == [[{"uuid": "c1", "content": "x", "name": "n"}]]


@pytest.mark.unit
def test_approve_missing_candidate_is_not_found_and_does_not_promote() -> None:
    adapter = _FakeAdapter(candidate=None)
    lifecycle = _FakeLifecycle()
    result = asyncio.run(_service(adapter, lifecycle).approve("missing"))

    assert result["status"] == "not_found"
    assert "promote:missing" not in adapter.calls
    assert lifecycle.batches == []


@pytest.mark.unit
def test_approve_check_failure_does_not_block_promotion() -> None:
    adapter = _FakeAdapter(candidate={"uuid": "c1", "content": "x", "name": "n"})
    lifecycle = _FakeLifecycle(raise_error=True)
    result = asyncio.run(_service(adapter, lifecycle).approve("c1"))

    assert result["status"] == "approved"
    assert result["conflicts_detected"] == 0
    assert "promote:c1" in adapter.calls


@pytest.mark.unit
def test_reject_deletes_candidate() -> None:
    adapter = _FakeAdapter(candidate={"uuid": "c1", "cluster_id": "P1"})
    result = asyncio.run(_service(adapter, _FakeLifecycle()).reject("c1"))

    assert result["status"] == "rejected"
    assert result["cluster_id"] == "P1"
    assert adapter.calls == ["fetch:c1", "reject:c1"]


@pytest.mark.unit
def test_reject_missing_candidate_is_not_found() -> None:
    adapter = _FakeAdapter(candidate=None)
    result = asyncio.run(_service(adapter, _FakeLifecycle()).reject("missing"))
    assert result["status"] == "not_found"
    assert "reject:missing" not in adapter.calls


@pytest.mark.unit
def test_list_candidates_delegates_to_adapter() -> None:
    adapter = _FakeAdapter()
    out = asyncio.run(_service(adapter, _FakeLifecycle()).list_candidates(source="painscan-friction", limit=5))
    assert out == [{"uuid": "c1"}]
    assert adapter.calls == ["list:painscan-friction:5"]
