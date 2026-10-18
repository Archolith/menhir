"""Read-time event timing for counting occurrences: pure helper, hydration, render, Cypher shape."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from menhir.domain.recall import RecallResult, ScalarAuthorityContributor, ScalarAuthorityVerdict
from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.typed_assertion_write_repository import TypedAssertionWriteMixin
from menhir.services.context_builder import ContextBuilderService
from menhir.services.occurrence_event_time import occurrence_event_time
from menhir.services.recall_policies import _authority_contributors

MENTIONED = "2023-03-08T01:55:00Z"
CONDO = "I'm saving up. I viewed a 1-bedroom condo on February 10th, but it was too small."


def test_absolute_date_in_sentence_after_quote():
    out = occurrence_event_time("viewed a 1-bedroom condo", CONDO, MENTIONED)
    assert out.event_at is not None and out.event_at.startswith("2023-02-10")
    assert out.event_basis == "explicit"
    assert out.sentence is not None and "on February 10th" in out.sentence


def test_relative_supported():
    text = "Life update: I had a baby boy named Jasper two weeks ago."
    out = occurrence_event_time("had a baby boy named Jasper", text, "2023-05-13T10:00:00Z")
    assert out.event_at is not None and out.event_at.startswith("2023-04-29")
    assert "two weeks ago" in (out.sentence or "")


def test_relative_unsupported_keeps_sentence_without_event():
    text = "Life update: I had a baby boy named Jasper a few weeks ago."
    out = occurrence_event_time("had a baby boy named Jasper", text, "2023-05-13T10:00:00Z")
    assert out.event_at is None and out.event_basis is None
    assert "a few weeks ago" in (out.sentence or "")


def test_no_date_everything_none_and_render_has_no_valid():
    out = occurrence_event_time("went to the gym", "I went to the gym after work", MENTIONED)
    assert (out.sentence, out.event_at, out.event_basis) == (None, None, None)


def test_sentence_locality():
    text = "I went to the gym. Last week I visited the museum."
    out = occurrence_event_time("went to the gym", text, MENTIONED)
    assert (out.sentence, out.event_at, out.event_basis) == (None, None, None)


def test_quote_missing_or_ambiguous():
    for text in ("something else entirely", "I went to the gym. Later I went to the gym again."):
        out = occurrence_event_time("went to the gym", text, MENTIONED)
        assert (out.sentence, out.event_at, out.event_basis) == (None, None, None)
    assert occurrence_event_time("x", None, MENTIONED).event_at is None


def test_span_length_independence():
    short = occurrence_event_time("viewed a 1-bedroom condo", CONDO, MENTIONED)
    long_ = occurrence_event_time("viewed a 1-bedroom condo on February 10th", CONDO, MENTIONED)
    assert short.event_at == long_.event_at is not None
    assert long_.sentence is None  # the quote already carries the cue


def test_sentence_capped():
    text = "I viewed a condo " + "really " * 80 + "on February 10th."
    out = occurrence_event_time("viewed a condo", text, MENTIONED)
    assert out.sentence is not None and len(out.sentence) <= 240


def _verdict(*contributors):
    return ScalarAuthorityVerdict(
        kind="current", status="leads", subject_uuid="s", attribute="visits", scope="",
        value_kind="count", unit="", value=2, valid_at=None, view_uuid="v",
        has_foundation=True, contributors=tuple(contributors))


async def _context(verdict):
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(verdict,)))
    return (await ContextBuilderService(recall_service=recall).build_context(
        "q", max_tokens=2000)).context


def _row(**kw):
    base = {
        "assertion_id": "a1", "relation": "CONTRIBUTED_TO", "operation": "occurrence",
        "value": 1, "stated_span": "viewed a 1-bedroom condo", "valid_at": MENTIONED,
        "evidence_tier": "user", "episode_uuid": "e1", "time_basis": "episode_reference",
        "turn_text": CONDO,
    }
    base.update(kw)
    return base


def test_hydration_fills_event_fields_for_occurrence_only():
    (occ,) = _authority_contributors({"contributors": [_row()]})
    assert occ.event_at and occ.event_at.startswith("2023-02-10")
    assert occ.event_basis == "explicit" and occ.mentioned_at == MENTIONED
    assert "on February 10th" in occ.source_sentence
    (other,) = _authority_contributors({"contributors": [_row(operation="delta")]})
    assert (other.source_sentence, other.event_at, other.event_basis, other.mentioned_at) == (
        None, None, None, None)


def test_hydration_mentioned_at_only_for_episode_reference():
    (occ,) = _authority_contributors({"contributors": [_row(time_basis="explicit")]})
    assert occ.mentioned_at is None and occ.event_at is None


@pytest.mark.asyncio
async def test_render_event_resolved():
    (occ,) = _authority_contributors({"contributors": [_row()]})
    text = await _context(_verdict(occ))
    assert ('  - CONTRIBUTED_TO: viewed a 1-bedroom condo -- "' in text)
    assert "(event 2023-02-10; mentioned 2023-03-08)" in text
    assert "valid 2023" not in text.split("[Scalar authority")[1].split("\n", 1)[1]


@pytest.mark.asyncio
async def test_render_no_event_says_mentioned_never_valid():
    (occ,) = _authority_contributors({"contributors": [_row(
        stated_span="went to the gym", turn_text="I went to the gym after work")]})
    text = await _context(_verdict(occ))
    assert "  - CONTRIBUTED_TO: went to the gym (mentioned 2023-03-08)" in text
    assert "(valid 2023-03-08" not in text


@pytest.mark.asyncio
async def test_non_occurrence_renders_exactly_as_before():
    c = ScalarAuthorityContributor(
        assertion_id="a", relation="CURRENT_ANCHOR", operation="absolute", value=5,
        stated_span="I went five times", valid_at="2026-01-01", evidence_tier="user",
        episode_uuid="e")
    text = await _context(_verdict(c))
    assert "  - CURRENT_ANCHOR: I went five times (valid 2026-01-01)" in text


class _Recorder:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append(query)
        if "RETURN count(*) AS total" in query:
            return [{"total": 1}]
        return []


def test_view_contributor_query_joins_turn_text_without_multiplying_rows():
    class _Repo(ScalarViewRepositoryMixin):
        def __init__(self, n):
            self.neo4j = n

    rec = _Recorder()
    _Repo(rec).fetch_scalar_authority_contributors(view_uuid="v", namespace="ns")
    page = rec.calls[-1]
    assert "OPTIONAL MATCH (t:TurnEvidence {turn_id: a.episode_uuid})" in page
    assert "t.namespace = a.namespace" in page
    assert "RETURN t.text AS turn_text" in page and "LIMIT 1" in page
    assert page.index("LIMIT $limit") < page.index("CALL {") < page.rindex("collect(")
    assert "turn_text: turn_text" in page and "time_basis: a.time_basis" in page


def test_assertion_contributor_query_joins_turn_text():
    rec = _Recorder()
    repo = TypedAssertionWriteMixin(rec)
    repo.fetch_assertion_contributors(assertion_ids=["a1"])
    q = rec.calls[-1]
    assert "OPTIONAL MATCH (t:TurnEvidence {turn_id: a.episode_uuid})" in q
    assert "t.namespace = a.namespace" in q
    assert "turn_text AS turn_text" in q and "a.time_basis AS time_basis" in q
