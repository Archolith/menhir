"""MCP tool surface for work artifacts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.api.routes_support import _BACKEND_METHODS, _required_tier_for_operation
from menhir.domain.work_artifact import ArtifactStatus, ArtifactType
from menhir.mcp.tools import ALL_TOOLS
from tests.mcp.error_contract import text_of

_ARTIFACT_OPS = {
    "get_artifact",
    "list_artifacts",
    "list_artifact_questions",
    "get_artifact_relationships",
    "link_artifacts",
    "supersede_artifact",
    "transition_artifact_status",
}


def _run(coro):
    return asyncio.run(coro)


def _tool(cls, **backend_methods):
    tool = cls()
    backend = MagicMock()
    for name, value in backend_methods.items():
        setattr(backend, name, AsyncMock(return_value=value))
    tool.get_backend = MagicMock(return_value=backend)
    return tool, backend


# ---------------------------------------------------------------------------
# Registration and policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_artifact_tools_are_registered() -> None:
    names = {t.name for t in ALL_TOOLS}
    assert {
        "get_artifact", "list_artifacts", "list_artifact_questions",
        "get_artifact_relationships", "link_artifacts", "supersede_artifact",
        "transition_artifact",
    } <= names


@pytest.mark.unit
def test_every_artifact_op_is_dispatchable() -> None:
    """A tool whose backend op is not allowlisted fails only at call time."""
    assert _ARTIFACT_OPS <= _BACKEND_METHODS


@pytest.mark.unit
@pytest.mark.parametrize(
    "operation,expected",
    [
        ("get_artifact", "readonly"),
        ("list_artifacts", "readonly"),
        ("list_artifact_questions", "readonly"),
        ("get_artifact_relationships", "readonly"),
        ("link_artifacts", "agent"),
        ("supersede_artifact", "agent"),
        ("transition_artifact_status", "agent"),
    ],
)
def test_artifact_op_tiers(operation: str, expected: str) -> None:
    """Reads are readonly; asserting engineering history is not."""
    assert _required_tier_for_operation(operation) == expected


@pytest.mark.unit
def test_list_artifact_description_matches_its_renderer() -> None:
    """The list renderer omits paths, so its description must point at get_artifact."""
    tool = {t.name: t for t in ALL_TOOLS}["list_artifacts"]()
    desc = tool.registered_description()

    assert "summaries" in desc
    assert "get_artifact" in desc
    assert "locations" not in desc


@pytest.mark.unit
def test_get_artifact_description_matches_its_renderer() -> None:
    """The detail renderer includes the current status and latest status change."""
    tool = {t.name: t for t in ALL_TOOLS}["get_artifact"]()
    desc = tool.registered_description()

    assert "current status" in desc
    assert "status-change date" in desc
    assert "code locations" in desc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_artifact_surfaces_an_unmapped_status_rather_than_hiding_it() -> None:
    from menhir.mcp.tools.ops.get_artifact import GetArtifactTool

    tool, _ = _tool(
        GetArtifactTool,
        get_artifact={
            "artifact_uuid": "a1", "artifact_type": "plan", "title": "T",
            "status": "PROPOSED", "namespace": "menhir",
            "status_raw": "ACTIVE - supersedes the ad-hoc phase",
            "status_unresolved_reason": "unrecognized_status",
            "embodiments": [], "locations": [],
        },
    )

    out = _run(tool.endpoint(artifact_uuid="a1"))

    assert "status unmapped: unrecognized_status" in out
    assert "ACTIVE - supersedes the ad-hoc phase" in out


@pytest.mark.unit
def test_get_artifact_reports_missing_rather_than_empty() -> None:
    from menhir.mcp.tools.ops.get_artifact import GetArtifactTool

    tool, _ = _tool(GetArtifactTool, get_artifact=None)

    assert "not found" in text_of(_run(tool.execute(artifact_uuid="nope")))


@pytest.mark.unit
def test_list_artifacts_flags_unmapped_statuses() -> None:
    from menhir.mcp.tools.ops.list_artifacts import ListArtifactsTool

    tool, _ = _tool(
        ListArtifactsTool,
        list_artifacts=[
            {
                "artifact_uuid": "a1",
                "artifact_type": "plan",
                "title": "Real",
                "status": "APPROVED",
                "embodiments": [{"locator_path": "docs/plan.md"}],
            },
            {"artifact_uuid": "a2", "artifact_type": "plan", "title": "Unknown",
             "status": "PROPOSED", "status_unresolved_reason": "no_status_header"},
        ],
    )

    out = _run(tool.endpoint())

    assert "artifacts (2)" in out
    assert "1 artifact(s) hold their type's initial status" in out


@pytest.mark.unit
def test_list_artifacts_renders_summaries_without_paths() -> None:
    from menhir.mcp.tools.ops.list_artifacts import ListArtifactsTool

    tool, _ = _tool(
        ListArtifactsTool,
        list_artifacts=[
            {"artifact_uuid": "a1", "artifact_type": "plan", "title": "Real",
             "status": "APPROVED"},
        ],
    )

    out = _run(tool.endpoint())

    assert "[plan] APPROVED  uuid=a1" in out
    assert "Real" in out
    assert "docs/plan.md" not in out


@pytest.mark.unit
def test_get_artifact_renders_status_change_date_and_locations() -> None:
    from menhir.mcp.tools.ops.get_artifact import GetArtifactTool

    tool, _ = _tool(
        GetArtifactTool,
        get_artifact={
            "artifact_uuid": "a1", "artifact_type": "plan", "title": "T",
            "status": "APPROVED", "namespace": "menhir",
            "created_at": "2026-01-02T03:04:05",
            "status_changed_at": "2026-02-03T04:05:06",
            "embodiments": [
                {"medium": "markdown", "locator_path": "docs/plan.md",
                 "locator_repository": "menhir"},
            ],
            "locations": [
                {"resolution_status": "resolved", "project": "menhir",
                 "path": "src/x.py"},
            ],
            "shape_status": "ok",
        },
    )

    out = _run(tool.endpoint(artifact_uuid="a1"))

    assert "status=APPROVED" in out
    assert "status changed: 2026-02-03" in out
    assert "menhir:docs/plan.md" in out
    assert "discusses: menhir:src/x.py" in out


@pytest.mark.unit
def test_empty_relationships_says_none_declared_not_none_exist() -> None:
    """Absence of a declared edge is not absence of a connection."""
    from menhir.mcp.tools.ops.get_artifact_relationships import ArtifactRelationshipsTool

    tool, _ = _tool(
        ArtifactRelationshipsTool,
        get_artifact_relationships={"outgoing": [], "incoming": [], "subjects": [], "todos": []},
    )

    out = _run(tool.endpoint(artifact_uuid="a1"))

    assert "no declared relationships" in out
    assert "never inferred" in out


@pytest.mark.unit
def test_relationships_render_both_directions() -> None:
    from menhir.mcp.tools.ops.get_artifact_relationships import ArtifactRelationshipsTool

    tool, _ = _tool(
        ArtifactRelationshipsTool,
        get_artifact_relationships={
            "outgoing": [{"relation": "IMPLEMENTS", "target_title": "The Plan",
                          "target_type": "plan"}],
            "incoming": [{"relation": "REVIEWS", "source_title": "The Review",
                          "source_type": "review"}],
            "subjects": [{"name": "OAuth"}],
            "todos": [{"todo_uuid": "t1", "status": "open"}],
        },
    )

    out = _run(tool.endpoint(artifact_uuid="a1"))

    assert "-> IMPLEMENTS: The Plan" in out
    assert "<- REVIEWS: The Review" in out
    assert "ABOUT: OAuth" in out
    assert "REFERENCES_TODO: t1" in out


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_link_refusal_explains_the_type_rule() -> None:
    from menhir.mcp.tools.ops.link_artifacts import LinkArtifactsTool

    tool, _ = _tool(
        LinkArtifactsTool,
        link_artifacts={"linked": False, "reason": "illegal_source_type"},
    )

    out = text_of(_run(tool.execute(source_uuid="a1", target_uuid="a2", relation="reviews")))

    assert "cannot be the source" in out


@pytest.mark.unit
def test_link_points_supersedes_at_the_lifecycle_command() -> None:
    """An edge alone must never imply a status change it did not make."""
    from menhir.mcp.tools.ops.link_artifacts import LinkArtifactsTool

    tool, _ = _tool(
        LinkArtifactsTool,
        link_artifacts={"linked": False, "reason": "unsupported_relation"},
    )

    out = text_of(_run(tool.execute(source_uuid="a1", target_uuid="a2", relation="supersedes")))

    assert "supersede_artifact" in out


@pytest.mark.unit
def test_transition_refusal_names_what_is_legal() -> None:
    """A bare refusal makes the caller probe states one at a time."""
    from menhir.mcp.tools.ops.transition_artifact import TransitionArtifactTool

    tool, _ = _tool(
        TransitionArtifactTool,
        transition_artifact_status={
            "applied": False, "reason": "illegal_transition",
            "from_status": ArtifactStatus.PROPOSED, "to_status": ArtifactStatus.IMPLEMENTED,
            "artifact_type": ArtifactType.PLAN,
            "valid_transitions": [ArtifactStatus.DEFERRED, ArtifactStatus.REVIEWED],
        },
    )

    out = text_of(_run(tool.execute(artifact_uuid="a1", to_status=ArtifactStatus.IMPLEMENTED)))

    assert "not legal for a plan" in out
    assert "REVIEWED" in out


@pytest.mark.unit
def test_transition_tool_routes_supersession_to_dedicated_action() -> None:
    from menhir.mcp.tools.ops.transition_artifact import TransitionArtifactTool

    tool, _ = _tool(
        TransitionArtifactTool,
        transition_artifact_status={"applied": False, "reason": "supersession_requires_replacement"},
    )
    out = text_of(_run(tool.execute(artifact_uuid="a1", to_status=ArtifactStatus.SUPERSEDED)))
    assert "supersede_artifact(new_uuid, old_uuid)" in out


@pytest.mark.unit
def test_transition_tool_reports_stale_conflict() -> None:
    from menhir.mcp.tools.ops.transition_artifact import TransitionArtifactTool

    tool, _ = _tool(
        TransitionArtifactTool,
        transition_artifact_status={"applied": False, "reason": "stale_transition"},
    )
    out = text_of(_run(tool.execute(artifact_uuid="a1", to_status=ArtifactStatus.REVIEWED)))
    assert "changed during this transition" in out


@pytest.mark.unit
def test_successful_transition_reports_both_ends() -> None:
    from menhir.mcp.tools.ops.transition_artifact import TransitionArtifactTool

    tool, _ = _tool(
        TransitionArtifactTool,
        transition_artifact_status={
            "applied": True, "from_status": "PROPOSED", "to_status": "REVIEWED"
        },
    )

    assert "PROPOSED -> REVIEWED" in _run(
        tool.endpoint(artifact_uuid="a1", to_status="REVIEWED")
    )


@pytest.mark.unit
def test_questions_render_in_author_order_with_addressable_uuids() -> None:
    from menhir.mcp.tools.ops.list_artifact_questions import ArtifactQuestionsTool

    tool, backend = _tool(
        ArtifactQuestionsTool,
        list_artifact_questions=[
            {"ordinal": 0, "text": "First?", "question_uuid": "q1",
             "artifact_title": "The Plan"},
            {"ordinal": 1, "text": "Second?", "question_uuid": "q2",
             "artifact_title": "The Plan"},
        ],
    )

    out = _run(tool.endpoint())

    assert "#1 First?" in out
    assert "#2 Second?" in out
    assert "question_uuid=q1" in out
    assert backend.list_artifact_questions.await_args.kwargs["status"] == "open"
