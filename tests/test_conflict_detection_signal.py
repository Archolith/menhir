"""Conflict detection: similarity floor, shared-episode veto, list_conflicts.

Plan: menhir-conflict-similarity-floor-plan-2026-11-05. No database and no network: the repository
is a spec'd mock and tests/fixtures/conflict_pairs.json carries each pair's real name cosine.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.telemetry.recorders import telemetry_store
from menhir.services.correlation_service import (
    CORRELATION_SIMILARITY_FLOOR,
    CorrelationService,
)
from menhir.services.correlation_service_signals import (
    attribute_tokens,
    attributes_overlap,
    pair_cosine,
)
from menhir.services.lifecycle_consolidation import LifecycleConsolidationMixin

pytestmark = pytest.mark.unit

EVENT = "menhir.services.correlation_service.record_mcp_event"
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "conflict_pairs.json").read_text(encoding="utf-8")
)
PAIRS = FIXTURE["pairs"]

# Real two-lane RRF scores are 1/a or 1/a + 1/b, so none lands in [0.85, 0.95): production reaches
# review through MERGE_ROUTE. CONFLICT_ROUTE covers the branch for a future scale change.
CONFLICT_ROUTE = 0.90
MERGE_ROUTE = 1.0
RELATED_ROUTE = 0.75
BOTH_ROUTES = pytest.mark.parametrize("score", [CONFLICT_ROUTE, MERGE_ROUTE], ids=["conflict", "merge"])


def _vectors(cosine: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Two unit vectors with this cosine, so the real ``pair_cosine`` runs."""
    return (1.0, 0.0), (cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine)))


BELOW_FLOOR = _vectors(0.10)


def _repo(*, co_mention: bool = False, names=("Roboto", "Inter"), scopes=("PERSISTENT", "PERSISTENT"),
          vectors=((1.0, 0.0), (1.0, 0.0)), spec=CorrelationRepository) -> MagicMock:
    repo = MagicMock(spec=spec)
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


# --- fixture: what each hand-written pair does today -------------------------


@BOTH_ROUTES
@pytest.mark.parametrize("pair", PAIRS, ids=[p["id"] for p in PAIRS])
def test_fixture_pair_routes_as_recorded(pair: dict, score: float) -> None:
    # expected_action "conflict" means the pair reaches review. On the merge route the judge is
    # stubbed to decline, so a pair the judge would confirm merges instead of queueing.
    repo = _repo(
        co_mention=pair["same_episode"],
        names=(pair["a"]["name"], pair["b"]["name"]),
        vectors=_vectors(pair["name_cosine"]),
    )
    svc = _svc(repo)
    with patch.object(svc, "_handle_merge_proposal", new=AsyncMock(return_value="conflict")):
        action, _ = _run(svc.classify_pair("src", "tgt", score))
    assert action == pair["expected_action"]


def test_fixture_is_stamped_with_real_cosines() -> None:
    assert FIXTURE["embedder"]
    assert len({p["id"] for p in PAIRS}) == len(PAIRS)
    assert all(isinstance(p["name_cosine"], float | int) for p in PAIRS)


def test_every_wrong_answer_in_the_fixture_is_a_named_limit() -> None:
    wrong = [p for p in PAIRS if (p["expected_action"] == "conflict") != p["should_look"]]
    assert wrong
    assert [p["id"] for p in wrong if not p.get("known_limit")] == []


def test_floor_is_under_every_pair_that_must_be_looked_at() -> None:
    lowest = min(p["name_cosine"] for p in PAIRS if p["should_look"])
    assert CORRELATION_SIMILARITY_FLOOR < lowest


# --- similarity floor --------------------------------------------------------


def test_below_floor_conflict_is_dropped_and_recorded() -> None:
    repo = _repo(vectors=BELOW_FLOOR)
    with patch(EVENT) as event:
        result = _run(_svc(repo).classify_pair("src", "tgt", CONFLICT_ROUTE))
    assert result == ("none", {"reason": "below_similarity_floor"})
    calls = [c for c in event.call_args_list if c.kwargs["operation"] == "correlation_similarity_floor"]
    assert len(calls) == 1
    assert calls[0].kwargs["payload"]["action"] == "conflict"
    assert calls[0].kwargs["payload"]["rrf_score"] == CONFLICT_ROUTE
    assert calls[0].kwargs["payload"]["cosine"] == pytest.approx(0.10)


def _stored_preview(repo, operation: str) -> dict:
    """Run a pair through the real recorder and return what it would persist."""
    with patch.object(telemetry_store, "record") as record:
        _run(_svc(repo).classify_pair("src", "tgt", CONFLICT_ROUTE))
    rows = [c.kwargs for c in record.call_args_list if c.kwargs["operation"] == operation]
    assert len(rows) == 1
    assert rows[0]["node_uuid"] == "src"
    return json.loads(rows[0]["payload_preview"])


def test_stored_floor_event_keeps_the_decision() -> None:
    preview = _stored_preview(_repo(vectors=BELOW_FLOOR), "correlation_similarity_floor")
    assert preview["action"] == "conflict"
    assert preview["cosine"] == pytest.approx(0.10)


@pytest.mark.parametrize(
    ("names", "vetoed"),
    [(("Roboto", "Inter"), True), (("retry limit 5", "retry limit 7"), False)],
    ids=["vetoed", "kept"],
)
def test_stored_veto_event_keeps_the_decision(names, vetoed: bool) -> None:
    preview = _stored_preview(_repo(co_mention=True, names=names), "conflict_shared_episode_veto")
    assert preview["vetoed"] is vetoed
    assert preview["attribute_overlap"] is not vetoed
    assert preview["phase"] == "conflict"


def test_below_floor_merge_candidate_never_reaches_vetoes_or_judge() -> None:
    repo = _repo(vectors=BELOW_FLOOR)
    svc = _svc(repo)
    with patch.object(svc, "_handle_merge_proposal", new=AsyncMock()) as proposal:
        result = _run(svc.classify_pair("src", "tgt", MERGE_ROUTE))
    assert result == ("none", {"reason": "below_similarity_floor"})
    proposal.assert_not_called()
    repo.check_co_mention_veto.assert_not_called()


def test_above_floor_merge_candidate_still_goes_to_review() -> None:
    svc = _svc(_repo())
    with patch.object(svc, "_handle_merge_proposal", new=AsyncMock(return_value="conflict")) as proposal:
        result = _run(svc.classify_pair("src", "tgt", MERGE_ROUTE))
    assert result == ("conflict", {"reason": "merge_proposal_blocked_by_veto_or_judge"})
    proposal.assert_awaited_once()


def test_cosine_equal_to_floor_is_kept() -> None:
    repo = _repo(vectors=((1.0, 0.0), (1.0, 0.0)))
    assert _run(_svc(repo, similarity_floor=1.0).classify_pair("src", "tgt", CONFLICT_ROUTE))[0] == "conflict"


@pytest.mark.parametrize(
    "vectors",
    [(None, None), (None, (1.0, 0.0)), ((1.0,), (1.0, 0.0)), ((0.0, 0.0), (1.0, 0.0))],
    ids=["both-missing", "one-missing", "dimension-mismatch", "zero-vector"],
)
@BOTH_ROUTES
def test_no_signal_keeps_todays_route(vectors, score: float) -> None:
    result = _run(_svc(_repo(vectors=vectors)).classify_pair("src", "tgt", score))
    assert result[0] == "conflict"
    assert result[1]["reason"] != "below_similarity_floor"


def _kept(repo, score: float) -> bool:
    """The floor kept the pair: the merge handler still ran, or the conflict was queued."""
    svc = _svc(repo)
    with patch.object(svc, "_handle_merge_proposal", new=AsyncMock(return_value="conflict")) as proposal:
        action, _ = _run(svc.classify_pair("src", "tgt", score))
    return action == "conflict" and proposal.await_count == (1 if score == MERGE_ROUTE else 0)


@BOTH_ROUTES
def test_embedding_read_error_keeps_todays_route(score: float) -> None:
    repo = _repo()
    repo.fetch_pair_name_embeddings.side_effect = RuntimeError("db down")
    assert _kept(repo, score)


def test_metadata_read_error_keeps_todays_route() -> None:
    # On the merge route the same read failing later in the handler is today's behaviour.
    repo = _repo(vectors=BELOW_FLOOR)
    repo.fetch_entity_merge_metadata.side_effect = RuntimeError("db down")
    assert _kept(repo, CONFLICT_ROUTE)


@BOTH_ROUTES
def test_member_missing_from_metadata_is_not_dropped(score: float) -> None:
    repo = _repo(vectors=BELOW_FLOOR)
    repo.fetch_entity_merge_metadata.return_value = [{"uuid": "src", "name": "x", "scope": "PERSISTENT"}]
    assert _kept(repo, score)


@BOTH_ROUTES
@pytest.mark.parametrize("scopes", [("PROMOTED", "PERSISTENT"), ("PERSISTENT", "PROMOTED")])
def test_promoted_member_is_never_dropped_by_the_floor(scopes, score: float) -> None:
    assert _kept(_repo(vectors=BELOW_FLOOR, scopes=scopes), score)


def test_related_and_none_routes_never_read_vectors() -> None:
    repo = _repo(vectors=BELOW_FLOOR)
    assert _run(_svc(repo).classify_pair("src", "tgt", RELATED_ROUTE))[0] == "related"
    repo.create_related_to_edge.assert_called_once()
    assert _run(_svc(repo).classify_pair("src", "tgt", 0.10)) == ("none", None)
    repo.fetch_pair_name_embeddings.assert_not_called()


# --- both production builders get the floor and the veto ----------------------


def test_enrichment_path_applies_floor_and_veto() -> None:
    # enrichment_steps builds CorrelationService with defaults and calls check_correlation.
    for repo in (_repo(vectors=BELOW_FLOOR), _repo(co_mention=True)):
        svc = _svc(repo)
        svc._graphiti.search_scored.return_value = [("tgt", "Inter", CONFLICT_ROUTE)]
        result = _run(svc.check_correlation("src", "Roboto"))
        assert result.conflicts == 0
        assert result.results == []


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"vectors": BELOW_FLOOR}, "below_similarity_floor"), ({"co_mention": True}, "shared_episode_veto")],
)
def test_lifecycle_builder_applies_floor_and_veto_on_the_graph_adapter(kwargs, reason) -> None:
    adapter = _repo(spec=MemoryGraphAdapter, **kwargs)
    host = SimpleNamespace(graph_adapter=adapter, graphiti_client=AsyncMock(), llm=None)
    svc = LifecycleConsolidationMixin._build_correlation_service(host)
    assert _run(svc.classify_pair("src", "tgt", CONFLICT_ROUTE)) == ("none", {"reason": reason})


def test_adapter_delegate_forwards_the_vector_read() -> None:
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._correlation = MagicMock(spec=CorrelationRepository)
    adapter._correlation.fetch_pair_name_embeddings.return_value = ([1.0], [2.0])
    assert adapter.fetch_pair_name_embeddings("a", "b") == ([1.0], [2.0])
    adapter._correlation.fetch_pair_name_embeddings.assert_called_once_with("a", "b")


# --- a drop must not un-gate later hits ---------------------------------------


class _Host(LifecycleConsolidationMixin):
    """The real consolidation loop over a spec'd adapter; the judge always declines."""

    def __init__(self, adapter, hits) -> None:
        self.graph_adapter = adapter
        self.graphiti_client = AsyncMock()
        self.graphiti_client.search_scored.return_value = hits
        self.llm = None
        self.settings = SimpleNamespace(conflict_cooldown_days=30)


def _scan_adapter(*, low_cosine=(), co_mention=False) -> MagicMock:
    adapter = _repo(spec=MemoryGraphAdapter, co_mention=co_mention)
    adapter.fetch_pair_name_embeddings.side_effect = (
        lambda _a, b: BELOW_FLOOR if b in low_cosine else ((1.0, 0.0), (1.0, 0.0))
    )
    adapter.fetch_entity_merge_metadata.side_effect = (
        lambda uuids: [{"uuid": u, "name": u, "scope": "PERSISTENT"} for u in uuids]
    )
    adapter.set_conflict.return_value = ("group", 2)
    return adapter


def _scan(adapter, hits) -> int:
    host = _Host(adapter, hits)
    with patch("menhir.services.lifecycle_consolidation.telemetry_store") as store:
        store.is_pair_resolved.return_value = False
        return _run(host._check_contradictions_batch([{"uuid": "src", "name": "source"}]))


HITS = [("x", "X", MERGE_ROUTE), ("y", "Y", MERGE_ROUTE), ("z", "Z", RELATED_ROUTE)]


def test_scan_without_a_drop_queues_the_first_hit_and_stops() -> None:
    adapter = _scan_adapter()
    assert _scan(adapter, HITS) == 1
    assert adapter.set_conflict.call_args.args[:2] == ("src", "x")
    adapter.create_related_to_edge.assert_not_called()


@pytest.mark.parametrize(
    "kwargs", [{"low_cosine": {"x"}}, {"co_mention": True}], ids=["floor", "veto"],
)
def test_drop_ends_the_node_scan(kwargs) -> None:
    # Reaching y or z here would be a queue write or edge the scan never made before the drop.
    adapter = _scan_adapter(**kwargs)
    assert _scan(adapter, HITS) == 0
    adapter.set_conflict.assert_not_called()
    adapter.create_related_to_edge.assert_not_called()
    assert {c.args[1] for c in adapter.fetch_pair_name_embeddings.call_args_list} == {"x"}


def test_plain_none_still_moves_on_to_the_next_hit() -> None:
    adapter = _scan_adapter()
    assert _scan(adapter, [("low", "L", 0.10), ("x", "X", MERGE_ROUTE)]) == 1
    assert adapter.set_conflict.call_args.args[:2] == ("src", "x")


def test_related_edge_before_the_drop_is_kept() -> None:
    adapter = _scan_adapter(low_cosine={"x"})
    assert _scan(adapter, [("z", "Z", RELATED_ROUTE), ("x", "X", MERGE_ROUTE), ("y", "Y", MERGE_ROUTE)]) == 0
    adapter.create_related_to_edge.assert_called_once()
    adapter.set_conflict.assert_not_called()


def test_batch_check_stops_at_a_drop_too() -> None:
    adapter = _scan_adapter(low_cosine={"x"})
    svc = _svc(adapter)
    svc._graphiti.search_scored.return_value = HITS
    result = _run(svc.check_correlation_batch([{"uuid": "src", "name": "source"}]))
    assert (result.conflicts, result.related, result.merged) == (0, 0, 0)
    adapter.create_related_to_edge.assert_not_called()


# --- shared-episode veto ------------------------------------------------------


def test_fonts_style_pair_is_vetoed_before_queueing() -> None:
    repo = _repo(co_mention=True, names=("Roboto", "Inter"))
    action, details = _run(_svc(repo).classify_pair("src", "tgt", CONFLICT_ROUTE))
    assert (action, details) == ("none", {"reason": "shared_episode_veto"})


def test_fonts_style_pair_blocked_merge_path_is_vetoed_too() -> None:
    # RRF 1.0 -> merge_proposed -> co-mention veto -> used to return "conflict" and be queued.
    repo = _repo(co_mention=True)
    action, details = _run(_svc(repo).classify_pair("src", "tgt", MERGE_ROUTE))
    assert (action, details) == ("none", {"reason": "shared_episode_veto"})


def test_same_episode_correction_pair_is_not_vetoed() -> None:
    repo = _repo(co_mention=True, names=("retry limit 5", "retry limit 7"))
    action, details = _run(_svc(repo).classify_pair("src", "tgt", CONFLICT_ROUTE))
    assert (action, details) == ("conflict", {"reason": "strong_overlap_pending_review"})


def test_veto_edge_cases_keep_todays_route() -> None:
    # Different episodes: no veto.
    assert _run(_svc(_repo(co_mention=False)).classify_pair("src", "tgt", CONFLICT_ROUTE))[0] == "conflict"
    # A PROMOTED member is never dropped.
    promoted = _repo(co_mention=True, scopes=("PROMOTED", "PERSISTENT"))
    assert _run(_svc(promoted).classify_pair("src", "tgt", CONFLICT_ROUTE))[0] == "conflict"
    # Fail open on a read error.
    broken = _repo(co_mention=True)
    broken.check_co_mention_veto.side_effect = RuntimeError("db down")
    assert _run(_svc(broken).classify_pair("src", "tgt", CONFLICT_ROUTE))[0] == "conflict"


def test_veto_does_not_touch_related_branch() -> None:
    repo = _repo(co_mention=True)
    action, _ = _run(_svc(repo).classify_pair("src", "tgt", RELATED_ROUTE))
    assert action == "related"
    repo.check_co_mention_veto.assert_not_called()


# --- list_conflicts scope + origin note ---------------------------------------


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
