"""Conflict-detection signal plan (2026-08-09): cosine shadow, shared-episode veto, list_conflicts.

New tests only. No database and no network: the repository is a spec'd mock.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.services.correlation_service import CorrelationService
from menhir.services.correlation_service_signals import (
    attribute_tokens,
    attributes_overlap,
    pair_cosine,
)

pytestmark = pytest.mark.unit

EVENT = "menhir.services.correlation_service.record_mcp_event"


def _repo(*, co_mention: bool = False, names=("Roboto", "Inter"), scopes=("PERSISTENT", "PERSISTENT"),
          vectors=((1.0, 0.0), (1.0, 0.0))) -> MagicMock:
    repo = MagicMock(spec=CorrelationRepository)
    repo.check_co_mention_veto.return_value = co_mention
    repo.check_ineligible_node_veto.return_value = False
    repo.check_anchor_project_veto.return_value = False
    repo.create_related_to_edge.return_value = True
    repo.fetch_entity_merge_metadata.return_value = [
        {"uuid": "src", "name": names[0], "scope": scopes[0]},
        {"uuid": "tgt", "name": names[1], "scope": scopes[1]},
    ]
    repo.fetch_pair_name_embeddings.return_value = vectors
    return repo


def _svc(repo, **kw) -> CorrelationService:
    return CorrelationService(repo, AsyncMock(), **kw)


def _run(coro):
    return asyncio.run(coro)


# --- pure helpers -----------------------------------------------------------


def test_pair_cosine_values_and_undefined_cases() -> None:
    assert pair_cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert pair_cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert pair_cosine(None, [1.0]) is None
    assert pair_cosine([1.0], [1.0, 2.0]) is None
    assert pair_cosine([0.0, 0.0], [1.0, 0.0]) is None


def test_attribute_overlap_rules() -> None:
    assert attribute_tokens("timeout 5s") == frozenset({"timeout"})
    assert attributes_overlap("retry limit 5", "retry limit 7")
    assert not attributes_overlap("Roboto", "Inter")
    assert attributes_overlap("", "Inter")  # undecidable counts as overlap (no veto)


# --- Phase 1: default behaviour byte-identical, shadow records only ----------


def test_default_off_never_reads_vectors_and_routes_as_before() -> None:
    repo = _repo()
    action, details = _run(_svc(repo).classify_pair("src", "tgt", 0.90))
    assert (action, details) == ("conflict", {"reason": "strong_overlap_pending_review"})
    repo.fetch_pair_name_embeddings.assert_not_called()
    repo.check_co_mention_veto.assert_not_called()


def test_shadow_records_old_vs_new_and_does_not_change_result() -> None:
    # RRF 0.90 -> legacy "conflict"; orthogonal vectors -> cosine 0.0 -> "none".
    repo = _repo(vectors=((1.0, 0.0), (0.0, 1.0)))
    with patch(EVENT) as ev:
        action, details = _run(_svc(repo, cosine_shadow=True).classify_pair("src", "tgt", 0.90))
    assert (action, details) == ("conflict", {"reason": "strong_overlap_pending_review"})
    shadow = [c for c in ev.call_args_list if c.kwargs["operation"] == "correlation_cosine_shadow"]
    assert len(shadow) == 1
    assert shadow[0].kwargs["result"] == {
        "legacy_action": "conflict", "cosine_action": "none", "differs": True,
    }
    assert shadow[0].kwargs["payload"]["rrf_score"] == 0.90


def test_shadow_failure_and_missing_vectors_never_affect_routing() -> None:
    repo = _repo()
    repo.fetch_pair_name_embeddings.side_effect = RuntimeError("db down")
    assert _run(_svc(repo, cosine_shadow=True).classify_pair("src", "tgt", 0.90))[0] == "conflict"
    repo = _repo(vectors=(None, None))
    with patch(EVENT) as ev:
        assert _run(_svc(repo, cosine_shadow=True).classify_pair("src", "tgt", 0.90))[0] == "conflict"
    assert ev.call_args_list[0].kwargs["result"]["cosine_action"] == "no_signal"


# --- Phase 2: shared-episode veto counterexamples ---------------------------


def test_fonts_style_pair_is_vetoed_before_queueing() -> None:
    repo = _repo(co_mention=True, names=("Roboto", "Inter"))
    action, details = _run(_svc(repo, shared_episode_veto=True).classify_pair("src", "tgt", 0.90))
    assert (action, details) == ("none", {"reason": "shared_episode_veto"})


def test_fonts_style_pair_blocked_merge_path_is_vetoed_too() -> None:
    # RRF 1.0 -> merge_proposed -> co-mention veto -> used to return "conflict" and be queued.
    repo = _repo(co_mention=True)
    action, details = _run(_svc(repo, shared_episode_veto=True).classify_pair("src", "tgt", 1.0))
    assert (action, details) == ("none", {"reason": "shared_episode_veto"})
    off = _run(_svc(_repo(co_mention=True)).classify_pair("src", "tgt", 1.0))
    assert off == ("conflict", {"reason": "merge_proposal_blocked_by_veto_or_judge"})


def test_same_episode_correction_pair_is_not_vetoed() -> None:
    repo = _repo(co_mention=True, names=("retry limit 5", "retry limit 7"))
    action, details = _run(_svc(repo, shared_episode_veto=True).classify_pair("src", "tgt", 0.90))
    assert (action, details) == ("conflict", {"reason": "strong_overlap_pending_review"})


def test_veto_off_and_other_edge_cases_leave_behaviour_unchanged() -> None:
    # Flag off: shared episode and disjoint names still queue (today's behaviour).
    off = _svc(_repo(co_mention=True))
    assert _run(off.classify_pair("src", "tgt", 0.90))[0] == "conflict"
    # Different episodes: no veto.
    assert _run(_svc(_repo(co_mention=False), shared_episode_veto=True)
                .classify_pair("src", "tgt", 0.90))[0] == "conflict"
    # A PROMOTED member is never dropped.
    promoted = _repo(co_mention=True, scopes=("PROMOTED", "PERSISTENT"))
    assert _run(_svc(promoted, shared_episode_veto=True)
                .classify_pair("src", "tgt", 0.90))[0] == "conflict"
    # Fail open on a read error.
    broken = _repo(co_mention=True)
    broken.check_co_mention_veto.side_effect = RuntimeError("db down")
    assert _run(_svc(broken, shared_episode_veto=True)
                .classify_pair("src", "tgt", 0.90))[0] == "conflict"


def test_veto_does_not_touch_related_branch() -> None:
    repo = _repo(co_mention=True)
    action, _ = _run(_svc(repo, shared_episode_veto=True).classify_pair("src", "tgt", 0.75))
    assert action == "related"
    repo.check_co_mention_veto.assert_not_called()


# --- Phase 3: list_conflicts scope + origin note ------------------------------


def _list(members: list[dict]) -> dict:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[
        {"group_id": "g1", "created_at": "2026-03-29T12:00:00Z", "members": members},
    ])
    tool = ListConflictsTool()
    tool.get_backend = MagicMock(return_value=backend)
    return json.loads(asyncio.run(tool.endpoint(status="unresolved", limit=5)))["groups"][0]


def test_list_conflicts_surfaces_scope_and_origin_hint() -> None:
    group = _list([
        {"uuid": "a", "name": "A", "content": "x", "status": "unresolved", "scope": "PROMOTED"},
        {"uuid": "b", "name": "B", "content": "y", "status": "unresolved", "scope": "PERSISTENT"},
    ])
    assert [m["scope"] for m in group["members"]] == ["PROMOTED", "PERSISTENT"]
    assert group["origin"]["recorded"] is False
    assert group["origin"]["promoted_bypass_possible"] is True

    plain = _list([
        {"uuid": "a", "name": "A", "content": "x", "status": "unresolved", "scope": "PERSISTENT"},
        {"uuid": "b", "name": "B", "content": "y", "status": "unresolved"},
    ])
    assert plain["origin"]["promoted_bypass_possible"] is False
    assert plain["members"][1]["scope"] == "unknown"


def test_repository_fetches_pair_embeddings_in_requested_order() -> None:
    neo4j = MagicMock()
    neo4j.execute.return_value = [
        {"uuid": "b", "embedding": [0.0, 1.0]}, {"uuid": "a", "embedding": [1.0, 0.0]},
    ]
    repo = CorrelationRepository(neo4j)
    assert repo.fetch_pair_name_embeddings("a", "b") == ([1.0, 0.0], [0.0, 1.0])
