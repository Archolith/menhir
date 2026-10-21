"""Scalar authority heading for history verdicts never prints "= None"."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from menhir.domain.recall import (
    RecallResult,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
)
from menhir.services.context_builder import ContextBuilderService


def _contributor(n: int) -> ScalarAuthorityContributor:
    return ScalarAuthorityContributor(
        assertion_id=f"a{n}", relation="HISTORY_ENTRY", operation="delta", value=n,
        stated_span=f"{n} postcards", valid_at="2023-08-11T00:00:00+00:00",
        evidence_tier="user", episode_uuid=f"ep{n}",
    )


def _verdict(**overrides) -> ScalarAuthorityVerdict:
    base = {
        "kind": "history", "status": "advisory", "subject_uuid": "ent-p",
        "attribute": "postcard_count", "scope": "", "value_kind": "count",
        "unit": "postcards", "value": None, "valid_at": "2023-11-30T00:00:00+00:00",
        "view_uuid": "hv-1", "has_foundation": False,
        "contributors": (_contributor(1), _contributor(2)), "contributors_total": 2,
    }
    base.update(overrides)
    return ScalarAuthorityVerdict(**base)


async def _context(verdict: ScalarAuthorityVerdict) -> str:
    result = RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(verdict,),
    )
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=result)
    built = await ContextBuilderService(recall_service=recall).build_context("q", max_tokens=2000)
    return built.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_verdict_heading_uses_entry_count() -> None:
    text = await _context(_verdict())
    assert (
        "[Scalar authority: ADVISORY] postcard_count history, 2 entries "
        "(history, valid 2023-11-30T00:00:00+00:00)"
    ) in text
    assert "= None" not in text
    assert "HISTORY_ENTRY: 1 postcards" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_verdict_count_falls_back_to_contributors() -> None:
    text = await _context(
        _verdict(contributors=(_contributor(1),), contributors_total=0, valid_at=None))
    assert "[Scalar authority: ADVISORY] postcard_count history, 1 entries (history)" in text
    assert "= None" not in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_current_verdict_heading_unchanged() -> None:
    text = await _context(_verdict(
        kind="current", status="leads", value=37, has_foundation=True,
        contributors=(), contributors_total=0,
    ))
    assert (
        "[Scalar authority: LEADS] postcard_count = 37 "
        "(current, valid 2023-11-30T00:00:00+00:00)"
    ) in text
