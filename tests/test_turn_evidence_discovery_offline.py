"""#170 item 2, offline: namespace stamping, cursor keys, and the event-lead foundation guard."""

from __future__ import annotations

import pytest

import menhir.services.recall_service as recall_service_mod
from menhir.domain.event_history import TypedEventAssertion
from menhir.domain.namespace import (
    TURN_EVIDENCE_NULL_NAMESPACE_KEY,
    stamped_namespace,
    turn_evidence_cursor_keys,
)
from menhir.domain.recall import RecallResult
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.infrastructure.turn_evidence_repository import (
    TurnEvidenceRepository,
    derive_turn_key,
)
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


class _RecNeo4j:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        return self.rows


@pytest.mark.unit
@pytest.mark.parametrize("ns", [None, "", "default"])
def test_default_silo_has_three_cursor_keys(ns):
    assert turn_evidence_cursor_keys(ns) == ["default", "", TURN_EVIDENCE_NULL_NAMESPACE_KEY]


@pytest.mark.unit
def test_named_namespace_has_one_cursor_key():
    assert turn_evidence_cursor_keys("proj") == ["proj"]


@pytest.mark.unit
def test_unspecified_namespace_is_stamped_default_but_turn_key_is_unchanged():
    fake = _RecNeo4j(rows=[{"turn_id": "t1", "created": True, "recorded_at": "x"}])
    TurnEvidenceRepository(fake).record_turn_evidence(
        text="I have 3 bikes", role="user", namespace=None, source_kind="agent_quoted",
        session_id="s1")
    params = fake.calls[-1][1]
    assert params["namespace"] == "default"
    # A pre-stamp retry of the same turn must still merge onto its node.
    assert params["turn_key"] == derive_turn_key(
        source_kind="agent_quoted", session_id="s1", text="I have 3 bikes", namespace=None)


# ---- event leads must rest on captured user words ---------------------------------------------

def _assertion(object_key: str, valid_at: str, turn_id: str) -> TypedEventAssertion:
    return TypedEventAssertion(
        subject_uuid=self_uuid_for_namespace("alice"), subject_display="Alpha",
        predicate="acquired", object_key=object_key, object_display="Notebook",
        domain=None, namespace=stamped_namespace("alice"),
        valid_at=valid_at, learned_at="2026-03-01T00:00:00Z",
        stated_span="an exact stated span", episode_uuid="ep-1", turn_evidence_uuid=turn_id,
        span_start=10, span_end=30, claim_ordinal=0)


class _Adapter:
    def __init__(self, evidence=None, *, raise_on_fetch=False):
        self.evidence = evidence
        self.raise_on_fetch = raise_on_fetch
        self.fetched: list[str] = []

    def event_assertions_for_subject_predicate(self, subject_uuid, predicate, **_kw):
        # The selector needs two distinct notebooks to scope "most recently"; ev-1 is the newest.
        return [_assertion("notebook-a", "2026-01-05T00:00:00Z", "ev-0"),
                _assertion("notebook-b", "2026-02-05T00:00:00Z", "ev-1")]

    def fetch_turn_evidence(self, turn_id):
        self.fetched.append(turn_id)
        if self.raise_on_fetch:
            raise RuntimeError("graph down")
        return self.evidence


async def _verdict(monkeypatch, adapter):
    async def _run_recall(*_a, **_kw):
        return RecallResult(query="", preset="KNOWLEDGE", results=[], candidates_evaluated=0,
                            nodes_touched=0, note="synthetic")

    monkeypatch.setattr(recall_service_mod, "run_recall", _run_recall)
    svc = RecallService(graphiti_client=object(), graph_adapter=adapter,
                        scoring_service=ScoringService(), event_history_authority_enabled=True)
    result = await svc.recall("which notebook did I buy most recently?", namespace="alice")
    return result.event_authority_layer[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_captured_event_still_leads(monkeypatch):
    adapter = _Adapter({"declarant": "user", "source_kind": "claude_code_hook"})
    verdict = await _verdict(monkeypatch, adapter)
    assert verdict.status == "leads" and adapter.fetched == ["ev-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_quoted_event_is_advisory(monkeypatch):
    verdict = await _verdict(monkeypatch, _Adapter(
        {"declarant": "user", "source_kind": "agent_quoted"}))
    assert verdict.status == "advisory" and verdict.gate == "foundation"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [
    _Adapter(None),
    _Adapter({"declarant": "assistant", "source_kind": "claude_code_hook"}),
    _Adapter(raise_on_fetch=True),
])
async def test_unverifiable_event_foundation_fails_closed(monkeypatch, adapter):
    verdict = await _verdict(monkeypatch, adapter)
    assert verdict.status == "advisory" and verdict.gate == "foundation"
