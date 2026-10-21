"""Regression coverage for the Graphiti/structure-graph ownership boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_resolution_policy import (
    MenhirCandidateFilterHook,
    _is_structural_graphiti_candidate,
)
from graphiti_core.candidate_filter import CandidateFilterDecision


def _node(name: str, **attributes: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, attributes=attributes, uuid=f"uuid-{name}")


@pytest.mark.unit
def test_structure_role_marks_graphiti_candidate_as_ineligible() -> None:
    assert _is_structural_graphiti_candidate(
        _node("sample-app", structure_role="project", root_path=r"C:\projects\sample-app")
    )
    assert not _is_structural_graphiti_candidate(_node("sample-app", source="project-scan"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_candidate_filter_excludes_structural_and_view_nodes() -> None:
    structural = _node("sample-app", structure_role="project", structure_path=".")
    view = _node("alice's coins: 37", is_view=True)
    semantic = _node("sample-app", source="project-scan")
    extracted = _node("sample-app")

    hook = MenhirCandidateFilterHook()

    assert (
        await hook.filter_candidate(
            SimpleNamespace(extracted_node=extracted, candidate_node=structural)
        )
        is CandidateFilterDecision.EXCLUDE
    )
    assert (
        await hook.filter_candidate(
            SimpleNamespace(extracted_node=extracted, candidate_node=view)
        )
        is CandidateFilterDecision.EXCLUDE
    )
    assert (
        await hook.filter_candidate(
            SimpleNamespace(extracted_node=extracted, candidate_node=semantic)
        )
        is CandidateFilterDecision.INCLUDE
    )
