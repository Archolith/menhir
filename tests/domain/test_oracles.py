"""Tests for the RetrievalOracle interface (R4) — immutability + read-only guarantee."""

from __future__ import annotations

import pytest

from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    QueryContext,
    RetrievalOracle,
)


def test_candidate_metadata_is_immutable() -> None:
    """The R4 read-only guarantee: an oracle cannot mutate the candidate it is handed."""
    c = CandidateMemory(id="m1", content="x", metadata={"repo": "menhir"})
    with pytest.raises(TypeError):
        c.metadata["repo"] = "other"   # MappingProxyType is read-only
    assert c.metadata["repo"] == "menhir"


def test_candidate_metadata_snapshot_is_decoupled_from_source() -> None:
    src = {"k": 1}
    c = CandidateMemory(id="m", content="x", metadata=src)
    src["k"] = 2  # mutating the original dict must not affect the frozen candidate
    assert c.metadata["k"] == 1


def test_query_context_and_result_are_frozen() -> None:
    q = QueryContext(text="t")
    with pytest.raises(Exception):
        q.text = "u"  # type: ignore[misc]
    r = OracleResult(oracle="o", probability=0.7)
    with pytest.raises(Exception):
        r.probability = 0.1  # type: ignore[misc]


def test_neutral_result_is_missing_not_falsity() -> None:
    n = OracleResult.neutral("o", note="timeout")
    assert n.polarity is OraclePolarity.MISSING
    assert n.probability == 0.5 and n.confidence == 0.0


def test_oracle_protocol_is_structural() -> None:
    class _Stub:
        name = "stub"
        def evaluate(self, query, candidate):  # noqa: ANN001
            return OracleResult(oracle="stub", probability=0.5)

    assert isinstance(_Stub(), RetrievalOracle)
