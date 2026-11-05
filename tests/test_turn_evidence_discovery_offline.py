"""#170 item 2, offline: namespace stamping and cursor keys."""

from __future__ import annotations

import pytest

from menhir.domain.namespace import (
    TURN_EVIDENCE_NULL_NAMESPACE_KEY,
    turn_evidence_cursor_keys,
)
from menhir.infrastructure.turn_evidence_repository import (
    TurnEvidenceRepository,
    derive_turn_key,
)


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
