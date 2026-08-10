"""Tests for the bounded, deterministic OracleExecutor (R4)."""

from __future__ import annotations

import asyncio

import pytest

from menhir.domain.oracles import CandidateMemory, OracleResult, QueryContext
from menhir.services.oracle_executor import OracleExecutor


class _ScoreOracle:
    """A deterministic oracle scoring by a metadata key."""

    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self.key = key

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        return OracleResult(oracle=self.name, probability=float(candidate.metadata.get(self.key, 0.0)))


class _SlowOracle:
    name = "slow"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        import time
        time.sleep(0.5)
        return OracleResult(oracle=self.name, probability=1.0)


class _BoomOracle:
    name = "boom"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        raise RuntimeError("kaboom")


def _candidates() -> list[CandidateMemory]:
    return [
        CandidateMemory(id="b", content="x", metadata={"rel": 0.2}),
        CandidateMemory(id="a", content="x", metadata={"rel": 0.9}),
        CandidateMemory(id="c", content="x", metadata={"rel": 0.5}),
    ]


def test_executor_requires_oracles() -> None:
    with pytest.raises(ValueError):
        OracleExecutor([])


@pytest.mark.asyncio
async def test_deterministic_grouping_and_order() -> None:
    ex = OracleExecutor([_ScoreOracle("rel", "rel"), _ScoreOracle("dup", "rel")])
    q = QueryContext(text="t")
    cands = _candidates()
    out1 = await ex.evaluate(q, cands)
    out2 = await ex.evaluate(q, list(reversed(cands)))
    # candidate iteration order is sorted + stable regardless of input order
    assert list(out1) == ["a", "b", "c"] == list(out2)
    # per-candidate oracle order follows executor priority (rel before dup)
    assert [r.oracle for r in out1["a"]] == ["rel", "dup"]
    assert out1 == out2


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty() -> None:
    ex = OracleExecutor([_ScoreOracle("rel", "rel")])
    assert await ex.evaluate(QueryContext(text="t"), []) == {}


@pytest.mark.asyncio
async def test_timeout_degrades_to_neutral() -> None:
    ex = OracleExecutor([_SlowOracle()], per_oracle_timeout_s=0.05)
    out = await ex.evaluate(QueryContext(text="t"), [CandidateMemory(id="m", content="x")])
    r = out["m"][0]
    assert r.note == "timeout" and r.confidence == 0.0


@pytest.mark.asyncio
async def test_failing_oracle_degrades_to_neutral_not_crash() -> None:
    ex = OracleExecutor([_BoomOracle()])
    out = await ex.evaluate(QueryContext(text="t"), [CandidateMemory(id="m", content="x")])
    assert out["m"][0].note.startswith("error:")


@pytest.mark.asyncio
async def test_concurrency_bound_is_respected() -> None:
    """A semaphore of 2 must never allow >2 concurrent oracle evaluations."""
    active = 0
    peak = 0

    class _Tracking:
        name = "track"
        def evaluate(self, query, candidate):  # noqa: ANN001
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            import time
            time.sleep(0.02)
            active -= 1
            return OracleResult(oracle=self.name, probability=0.5)

    ex = OracleExecutor([_Tracking()], max_concurrency=2)
    cands = [CandidateMemory(id=str(i), content="x") for i in range(8)]
    await ex.evaluate(QueryContext(text="t"), cands)
    assert peak <= 2


@pytest.mark.asyncio
async def test_prefetch_seam_enriches_candidates() -> None:
    async def prefetch(query, candidates):  # noqa: ANN001
        return [CandidateMemory(id=c.id, content=c.content, metadata={"rel": 1.0}) for c in candidates]

    ex = OracleExecutor([_ScoreOracle("rel", "rel")], prefetch=prefetch)
    out = await ex.evaluate(QueryContext(text="t"), [CandidateMemory(id="m", content="x", metadata={"rel": 0.0})])
    assert out["m"][0].probability == 1.0   # prefetch overrode the metadata
