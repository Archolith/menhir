"""Tests for the IntentOracle + affinity matrix (menhir port, pure domain).

These exercise the IntentOracle in services/retrieval_oracles.py and the matrix in
domain/intent_affinity.py. Pure domain — no graph, no embedder, no infra."""

from __future__ import annotations

from menhir.domain.artifact_role import ContentRole
from menhir.domain.intent_affinity import (
    Affinity,
    affinity,
    resolve_affinity,
    task_intents_to_lens,
)
from menhir.domain.oracles import CandidateMemory, OraclePolarity, OracleTarget, QueryContext
from menhir.domain.query_intent import TaskIntent
from menhir.services.retrieval_oracles import IntentOracle, default_oracles


# --- matrix -----------------------------------------------------------------

def test_matrix_load_bearing_cells() -> None:
    assert affinity(TaskIntent.DEBUG_FAILURE, ContentRole.FAILURE) is Affinity.PREFER
    assert affinity(TaskIntent.EXPLAIN_DECISION, ContentRole.DECISION) is Affinity.PREFER
    assert affinity(TaskIntent.PLAN_NEXT_ACTION, ContentRole.PLAN) is Affinity.PREFER
    assert affinity(TaskIntent.UNDERSTAND_SYSTEM, ContentRole.REFERENCE) is Affinity.PREFER
    assert affinity(TaskIntent.UNDERSTAND_SYSTEM, ContentRole.INCIDENT) is Affinity.PENALIZE


def test_resolve_affinity_max_over_cross_product() -> None:
    aff, _, _ = resolve_affinity(
        [TaskIntent.DEBUG_FAILURE, TaskIntent.UNDERSTAND_SYSTEM],
        {ContentRole.FAILURE, ContentRole.REFERENCE},
    )
    assert aff is Affinity.PREFER  # debug prefers failure; max wins


def test_lens_history_wins_on_conflict_and_verify_is_neutral() -> None:
    # AVOID wants history, DEBUG wants current -> history wins.
    assert task_intents_to_lens([TaskIntent.AVOID_REPEAT, TaskIntent.DEBUG_FAILURE]) == "historical"
    assert task_intents_to_lens([TaskIntent.DEBUG_FAILURE]) == "current"
    # verify routes to the NEUTRAL lens (the bench fix), never historical.
    assert task_intents_to_lens([TaskIntent.VERIFY_CURRENTNESS]) == "any"


# --- oracle -----------------------------------------------------------------

def test_oracle_prefers_matching_role() -> None:
    ctx = QueryContext(text="why is the floor failing")
    failure = CandidateMemory(id="f", content="x", metadata={"artifact_type": "failure"})
    decision = CandidateMemory(id="d", content="x", metadata={"artifact_type": "decision"})
    assert IntentOracle().evaluate(ctx, failure).probability > IntentOracle().evaluate(ctx, decision).probability


def test_oracle_neutral_on_low_confidence() -> None:
    ctx = QueryContext(text="floor recall vector")  # no intent cue
    res = IntentOracle().evaluate(ctx, CandidateMemory(id="f", content="x", metadata={"artifact_type": "failure"}))
    assert res.polarity is OraclePolarity.NEUTRAL


def test_oracle_targets_relevance_and_never_contradicts() -> None:
    ctx = QueryContext(text="how does the floor work")
    res = IntentOracle().evaluate(ctx, CandidateMemory(id="i", content="x", metadata={"artifact_type": "incident"}))
    assert res.target is OracleTarget.RELEVANCE
    assert res.polarity in (OraclePolarity.SUPPORT, OraclePolarity.NEUTRAL)  # pure relevance


def test_oracle_read_only_metadata() -> None:
    cand = CandidateMemory(id="f", content="x", metadata={"artifact_type": "failure"})
    IntentOracle().evaluate(QueryContext(text="why is it failing"), cand)
    assert cand.metadata["artifact_type"] == "failure"  # immutable mapping, untouched


def test_intent_oracle_in_default_set() -> None:
    # Graduated into the production oracle set after the real-embedder bench gate passed.
    assert "intent" in {o.name for o in default_oracles()}
