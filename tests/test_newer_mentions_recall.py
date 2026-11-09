"""#564 part 2 through the recall pipeline: a newer mention keeps a leading money View advisory."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from menhir.api.routes_support import ScalarAuthorityVerdictResponse
from menhir.domain.recall import NewerMention
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_approximate_recall_listing import _context, _verdict
from tests.test_scalar_history_recall import _setup_observation_lane, _setup_search_and_metadata

NS = "proj"
SELF = self_uuid_for_namespace(NS)
QUERY = "how much was I pre-approved for"
ASKED = "Do you remember when I got pre-approved for $400,000 from Wells Fargo?"
STATED_AT = "2023-08-11T05:59:00+00:00"


def _hit(subject=SELF):
    return [{
        "assertion_id": "obs-pre-1", "stated_span": "pre-approved for $350,000", "cosine": 0.95,
        "subject_uuid": subject, "subject_display": "user",
        "attribute": "mortgage_preapproval_amount", "scope": "", "value_kind": "money",
        "unit": "usd", "namespace": NS, "evidence_tier": "user", "operation": "absolute",
        "value": "350000", "valid_at": STATED_AT,
    }]


def _view(subject=SELF):
    return {
        "uuid": "sv-pre", "value": "350000", "name": "mortgage_preapproval_amount: 350000",
        "view_key": "vk-pre", "attribute": "mortgage_preapproval_amount",
        "subject_uuid": subject, "namespace": NS, "valid_at": STATED_AT, "recall_eligible": True,
    }


def _episode(content=ASKED, *, when="2023-11-30T12:13:00Z", source="user"):
    return {"uuid": "ep-asked", "content": content, "reference_time": when, "source": source,
            "cosine": 0.8}


async def _recall(client, adapter, *, flag=True, episodes=(), subject=SELF, search_fails=False):
    _setup_observation_lane(adapter, obs_hit=_hit(subject), history_view=None,
                            state_view=_view(subject))
    _setup_search_and_metadata(client, adapter)
    adapter.scalar_view_has_user_foundation = lambda *, view_uuid, namespace=None: True
    adapter.counting_match_enabled = False
    calls: list[dict] = []

    def search(vec, **kwargs):
        calls.append(kwargs)
        if search_fails:
            raise RuntimeError("search down")
        return list(episodes)

    adapter.search_episode_embeddings = search
    svc = RecallService(graphiti_client=client, graph_adapter=adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True,
                        counting_enabled=False)
    tuning = RetrievalTuningConfig(scalar_newer_mentions=flag, scalar_verdict_min_similarity=0.0,
                                   enable_source_memories=False)
    res = await svc.recall(QUERY, namespace=NS, tuning=tuning)
    verdict = next(v for v in res.authority_layer if v.attribute == "mortgage_preapproval_amount")
    return verdict, calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_newer_question_mention_is_listed_and_view_is_advisory(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    verdict, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                                   episodes=[_episode()])
    assert verdict.status == "advisory"
    assert verdict.value == "350000", "the mention never becomes the value"
    assert verdict.newer_mentions == (NewerMention(
        episode_uuid="ep-asked", value="400000", quote=ASKED, said_at="2023-11-30T12:13:00Z",
        mood="question"),)
    assert len(calls) == 1
    assert calls[0]["namespace"] == NS
    assert "subject_uuid" not in calls[0] and not calls[0].get("resolved_only")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_does_not_search_and_view_leads(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    verdict, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter, flag=False,
                                   episodes=[_episode()])
    assert verdict.status == "leads"
    assert verdict.newer_mentions == ()
    assert calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_matching_turn_leaves_the_view_leading(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    episodes = [_episode(source="assistant"), _episode(when="2023-08-01T00:00:00Z"),
                _episode("I paid $400,000 for the renovation.")]
    verdict, _ = await _recall(stub_graphiti_client, stub_memory_graph_adapter, episodes=episodes)
    assert verdict.status == "leads"
    assert verdict.newer_mentions == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_self_subject_is_not_checked(stub_graphiti_client, stub_memory_graph_adapter):
    verdict, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                                   episodes=[_episode()], subject="ent-dad")
    assert verdict.status == "leads"
    assert calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_failure_keeps_the_view_advisory_without_a_listing(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    verdict, _ = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                               episodes=[_episode()], search_fails=True)
    assert verdict.status == "advisory"
    assert verdict.newer_mentions == ()


MENTION = NewerMention("ep-asked", "400000", ASKED, "2023-11-30T12:13:00Z", "question")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_renderer_lists_the_mention():
    plain = await _context(_verdict())
    assert "newer mention" not in plain
    listed = await _context(_verdict(newer_mentions=(MENTION,)))
    assert f'  newer mention, in a question: 400000 ("{ASKED}", 2023-11-30T12:13:00Z)' in listed


@pytest.mark.unit
def test_response_model_carries_newer_mentions_and_defaults_empty():
    out = ScalarAuthorityVerdictResponse(**asdict(_verdict(newer_mentions=(MENTION,))))
    assert out.newer_mentions[0].value == "400000"
    assert out.newer_mentions[0].mood == "question"
    assert ScalarAuthorityVerdictResponse(**asdict(_verdict())).newer_mentions == []
