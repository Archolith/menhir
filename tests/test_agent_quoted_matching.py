"""One definition of agent-quoted evidence at every reader (CF-287)."""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.domain.self_identity import _is_agent_quoted
from menhir.domain.truth.admission_gate import evaluate_user_tier_claim
from menhir.domain.turn_role import is_agent_quoted, resolve_turn_role

pytestmark = pytest.mark.unit

SPELLINGS = ("agent-quoted", "Agent_Quoted", " AGENT-QUOTED ", "agent_quoted")
TURN = "I moved to Lisbon two weeks ago."


@pytest.mark.parametrize("kind", SPELLINGS[:3])
def test_resolve_turn_role_ignores_non_canonical_agent_quoted_evidence(kind) -> None:
    claimed = {
        "content": TURN,
        "turn_evidence_count": 1,
        "turn_evidence_text": TURN,
        "turn_evidence_role": "user",
        "turn_evidence_source_kind": kind,
        "namespace": "ns",
        "turn_evidence_namespace": "ns",
        "turn_role": "assistant",
    }
    assert resolve_turn_role(claimed) == "assistant"


def test_admission_gate_denies_non_canonical_agent_quoted() -> None:
    verdict = evaluate_user_tier_claim(
        requested_source="user",
        turn_evidence={"turn_id": "t1", "role": "user", "text": TURN, "source_kind": "Agent-Quoted"},
        claimed_text=TURN,
        session_id=None,
        namespace=None,
    )
    assert verdict.granted is False
    assert "agent_quoted" in verdict.reason


def test_is_agent_quoted_matches_self_identity() -> None:
    for kind in SPELLINGS:
        assert is_agent_quoted(kind) is True
        assert _is_agent_quoted({"turn_evidence_source_kind": kind}) is True
    for kind in ("agent_quotedx", "", None):
        assert is_agent_quoted(kind) is False
        assert _is_agent_quoted({"turn_evidence_source_kind": kind}) is False


def test_scalar_view_queries_use_the_normalised_expression() -> None:
    import menhir.infrastructure.scalar_view_repository as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    expr = "replace(toLower(trim(coalesce(te.source_kind, ''))), '-', '_') <> 'agent_quoted'"
    assert src.count(expr) == 2
    assert "coalesce(te.source_kind, '') <> 'agent_quoted'" not in src
