"""Two-tier self identity, Phase 2: recall-time tier labelling, duplicate-fact collapse, wiring
guards and the MCP wire shape. Offline only."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

import menhir.services.recall_service as recall_service_mod
from menhir.domain.event_history import TypedEventAssertion
from menhir.domain.namespace import stamped_namespace
from menhir.domain.recall import RecallResult, ScoredMemory, TemporalFact
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.domain.self_identity import inferred_self_uuid_for_namespace, self_uuid_for_namespace
from menhir.mcp.formatters import _compact_scored_item
from menhir.services.recall_pipeline import _inferred_self_on
from menhir.services.recall_policies import label_and_collapse_self_tiers
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.unit

ANCHORED = "anchored-uuid"
INFERRED = "inferred-uuid"


def _bd() -> RelevanceBreakdown:
    return RelevanceBreakdown(
        semantic_similarity=0.5, adjacency_bonus=0.0, recency_bonus=0.0, prominence_bonus=0.0,
        conflict_bonus=0.0, type_boost=0.0, preset="KNOWLEDGE", alpha=0.2, beta=0.1, gamma=0.1,
        delta=0.0,
    )


def _tf(text, **kw) -> TemporalFact:
    return TemporalFact(
        fact=text, valid_at=None, invalid_at=None, created_at=None, expired_at=None,
        is_current_belief=True, temporal_role="current_belief", **kw,
    )


def _sm(uuid: str, facts=(), **kw) -> ScoredMemory:
    return ScoredMemory(
        uuid=uuid, name=uuid, content="c", scope="g", memory_type="Entity", final_score=0.5,
        breakdown=_bd(), temporal_facts=tuple(facts), **kw,
    )


def _collapse(anchored_facts, inferred_facts):
    results = [_sm(ANCHORED, [_tf(f) for f in anchored_facts]),
               _sm(INFERRED, [_tf(f) for f in inferred_facts])]
    out = label_and_collapse_self_tiers(results, anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    return out[0], out[1]


# ----------------------------------------------------------------- 1. label_and_collapse_self_tiers


def test_neither_self_present_returns_results_unchanged():
    results = [_sm("a", [_tf("x y")]), _sm("b")]
    out = label_and_collapse_self_tiers(results, anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    assert [sm.uuid for sm in out] == ["a", "b"]
    assert all(o is r for o, r in zip(out, results))
    assert all(sm.self_tier is None for sm in out)


def test_labels_each_tier_and_leaves_others_untouched_in_order():
    other = _sm("other")
    results = [other, _sm(INFERRED), _sm(ANCHORED)]
    out = label_and_collapse_self_tiers(results, anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    assert [sm.uuid for sm in out] == ["other", INFERRED, ANCHORED]
    assert [sm.self_tier for sm in out] == [None, "inferred", "anchored"]
    assert out[0] is other


def test_inferred_alone_keeps_all_facts_and_is_labelled():
    facts = [_tf("user sent 12 postcards"), _tf("user likes tea")]
    out = label_and_collapse_self_tiers(
        [_sm(INFERRED, facts), _sm("other")], anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    assert out[0].self_tier == "inferred"
    assert [tf.fact for tf in out[0].temporal_facts] == ["user sent 12 postcards", "user likes tea"]
    assert out[1].self_tier is None


def test_anchored_alone_is_labelled_and_facts_not_corroborated():
    out = label_and_collapse_self_tiers(
        [_sm(ANCHORED, [_tf("user likes tea")])], anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    assert out[0].self_tier == "anchored"
    assert out[0].temporal_facts[0].corroborated is False


def test_duplicate_with_article_and_punctuation_collapses_and_corroborates():
    anchored, inferred = _collapse(["user sent 12 postcards"], ["The user sent 12 postcards."])
    assert inferred.temporal_facts == ()
    assert anchored.temporal_facts[0].corroborated is True
    assert anchored.self_tier == "anchored" and inferred.self_tier == "inferred"


def test_different_number_is_not_collapsed():
    # Jaccard {user,sent,12,postcards} vs {user,sent,3,postcards} = 3/5.
    anchored, inferred = _collapse(["user sent 12 postcards"], ["the user sent 3 postcards"])
    assert [tf.fact for tf in inferred.temporal_facts] == ["the user sent 3 postcards"]
    assert anchored.temporal_facts[0].corroborated is False


def test_jaccard_exactly_point_eight_is_not_a_match():
    # 4 shared tokens out of a 5-token union = 0.8 exactly -> strict > means kept.
    anchored, inferred = _collapse(["alpha beta gamma delta"], ["alpha beta gamma delta epsilon"])
    assert len(inferred.temporal_facts) == 1
    assert anchored.temporal_facts[0].corroborated is False


def test_jaccard_above_point_eight_is_a_match():
    # 5 shared of 6 = 0.833 > 0.8 -> collapsed.
    anchored, inferred = _collapse(["a1 b2 c3 d4 e5"], ["a1 b2 c3 d4 e5 f6"])
    assert inferred.temporal_facts == ()
    assert anchored.temporal_facts[0].corroborated is True


def test_none_and_blank_facts_never_match():
    results = [
        _sm(ANCHORED, [_tf(None), _tf("   "), _tf("the a an")]),
        _sm(INFERRED, [_tf(None), _tf(""), _tf("a")]),
    ]
    out = label_and_collapse_self_tiers(results, anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    assert len(out[1].temporal_facts) == 3
    assert not any(tf.corroborated for tf in out[0].temporal_facts)


def test_mixed_facts_only_matching_ones_dropped_and_corroborated():
    anchored, inferred = _collapse(
        ["user sent 12 postcards", "user owns a red bike", "user lives in Oslo"],
        ["the user sent 12 postcards", "user plays chess"],
    )
    assert [tf.fact for tf in inferred.temporal_facts] == ["user plays chess"]
    assert [tf.corroborated for tf in anchored.temporal_facts] == [True, False, False]


def test_collapse_does_not_mutate_inputs():
    a = _sm(ANCHORED, [_tf("user sent 12 postcards")])
    i = _sm(INFERRED, [_tf("the user sent 12 postcards")])
    results = [a, i]
    out = label_and_collapse_self_tiers(results, anchored_uuid=ANCHORED, inferred_uuid=INFERRED)
    assert out is not results
    assert results == [a, i] and results[0] is a and results[1] is i
    assert a.self_tier is None and i.self_tier is None
    assert a.temporal_facts[0].corroborated is False
    assert len(i.temporal_facts) == 1


# ----------------------------------------------------------------- 2. _inferred_self_on


def test_inferred_self_on_truth_table():
    assert _inferred_self_on(object()) is False
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode="off")) is False
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode="bogus")) is False
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode=None)) is False
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode="observe")) is True
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode="enforce")) is True
    # resolve_inferred_mode strips and lower-cases.
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode=" ENFORCE ")) is True
    assert _inferred_self_on(SimpleNamespace(inferred_self_mode="Observe")) is True


# ----------------------------------------------------------------- 4. event authority anchored-only


class _Repo:
    def __init__(self) -> None:
        self.subjects: list[str] = []

    def event_assertions_for_subject_predicate(self, subject_uuid, predicate, **kwargs):
        self.subjects.append(subject_uuid)
        return []


@pytest.mark.asyncio
async def test_event_authority_subject_is_anchored_even_with_inferred_mode_enforce(monkeypatch):
    async def _run_recall(*args, **kwargs):
        return RecallResult(query="", preset="KNOWLEDGE", results=[], candidates_evaluated=0,
                            nodes_touched=0)

    monkeypatch.setattr(recall_service_mod, "run_recall", _run_recall)
    repo = _Repo()
    svc = RecallService(
        graphiti_client=object(), graph_adapter=repo, scoring_service=ScoringService(),
        event_history_authority_enabled=True, inferred_self_mode="enforce",
    )
    await svc.recall("which notebook did I buy most recently?", namespace="alice")

    assert repo.subjects, "event authority lookup did not run"
    assert set(repo.subjects) == {self_uuid_for_namespace("alice")}
    assert inferred_self_uuid_for_namespace("alice") not in repo.subjects
    # keep the imports honest: the stamped form is what the repo is keyed on elsewhere
    assert stamped_namespace("alice") and TypedEventAssertion


# ----------------------------------------------------------------- 5. MCP formatter


@pytest.mark.parametrize("compact", [True, False])
def test_formatter_self_tier_only_when_set(compact):
    assert "self_tier" not in _compact_scored_item(_sm("x"), compact=compact)
    got = _compact_scored_item(_sm("x", self_tier="inferred"), compact=compact)
    assert got["self_tier"] == "inferred"


@pytest.mark.parametrize("compact", [True, False])
def test_formatter_corroborated_only_when_true_dataclass_facts(compact):
    sm = _sm("x", [_tf("yes", corroborated=True), _tf("no")])
    facts = _compact_scored_item(sm, compact=compact)["temporal_facts"]
    assert facts[0]["corroborated"] is True
    assert "corroborated" not in facts[1]


@pytest.mark.parametrize("compact", [True, False])
def test_formatter_corroborated_only_when_true_dict_facts(compact):
    # The MCP path hands the formatter SimpleNamespace(**asdict(scored)); facts are dicts.
    sm = _sm("x", [_tf("yes", corroborated=True), _tf("no")], self_tier="anchored")
    wire = SimpleNamespace(**asdict(sm))
    assert isinstance(wire.temporal_facts[0], dict)
    got = _compact_scored_item(wire, compact=compact)
    assert got["self_tier"] == "anchored"
    assert got["temporal_facts"][0]["corroborated"] is True
    assert "corroborated" not in got["temporal_facts"][1]
