"""Unit tests for the pure recall-provenance derivation (who said a recall result)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from menhir.domain import recall_provenance, self_identity
from menhir.domain.recall_provenance import (
    RecallProvenance,
    derive_recall_provenance,
    provenance_label,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sources", "said_by"),
    [
        (["user"], "user"),
        (["manual"], "user"),
        ([" User "], "user"),
        (["USER", "Manual"], "user"),
        (["agent_inference"], "agent"),
        (["claude-code"], "agent"),
        (["document-ingest"], "agent"),
        (["user", "claude-code"], "mixed"),
        (["manual", "agent_inference", "document-ingest"], "mixed"),
        ([None], "unknown"),
        ([], "unknown"),
        (["", None], "unknown"),
        (["   "], "unknown"),
        # Graphiti's placeholder episode type is not a Menhir stamp (review M1)
        (["message"], "unknown"),
        (["message", "user"], "user"),
        (["Message", "claude-code"], "agent"),
    ],
)
def test_said_by_classification(sources, said_by) -> None:
    assert derive_recall_provenance({"sources": sources}).said_by == said_by


@pytest.mark.unit
def test_missing_sources_key_is_unknown() -> None:
    assert derive_recall_provenance({}).said_by == "unknown"
    assert derive_recall_provenance({"sources": None}).said_by == "unknown"


@pytest.mark.unit
def test_sources_are_sorted_distinct_non_empty_originals() -> None:
    got = derive_recall_provenance(
        {"sources": ["zeta", None, "user", "alpha", "user", "", "zeta"]}
    )
    # originals (not lowercased/stripped), sorted, deduped, blanks and None dropped
    assert got.sources == ("alpha", "user", "zeta")


@pytest.mark.unit
def test_turn_evidence_ids_sorted_distinct_capped_at_five() -> None:
    ids = ["t9", "t3", "t1", "t3", None, "t7", "t2", "t5", "t4", "t1"]
    got = derive_recall_provenance({"sources": ["user"], "turn_evidence_ids": ids})
    # distinct non-null = t1 t2 t3 t4 t5 t7 t9 -> first five after sorting
    assert got.turn_evidence_ids == ("t1", "t2", "t3", "t4", "t5")


@pytest.mark.unit
def test_turn_evidence_ids_under_cap_all_kept_and_none_is_empty() -> None:
    got = derive_recall_provenance({"turn_evidence_ids": ["b", "a", "b"]})
    assert got.turn_evidence_ids == ("a", "b")
    assert derive_recall_provenance({}).turn_evidence_ids == ()
    assert derive_recall_provenance({"turn_evidence_ids": None}).turn_evidence_ids == ()


@pytest.mark.unit
def test_first_seen_passthrough_string_datetime_and_missing() -> None:
    assert derive_recall_provenance({"first_seen_at": "2026-01-02T03:04:05Z"}).first_seen_at == (
        "2026-01-02T03:04:05Z"
    )
    moment = datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    assert derive_recall_provenance({"first_seen_at": moment}).first_seen_at == moment.isoformat()
    assert derive_recall_provenance({}).first_seen_at is None
    assert derive_recall_provenance({"first_seen_at": ""}).first_seen_at is None


@pytest.mark.unit
def test_to_dict_shape_is_json_ready() -> None:
    d = derive_recall_provenance(
        {"sources": ["user", "claude-code"], "first_seen_at": "x", "turn_evidence_ids": ["t1"]}
    ).to_dict()
    assert d == {
        "said_by": "mixed",
        "sources": ["claude-code", "user"],
        "first_seen_at": "x",
        "turn_evidence_ids": ["t1"],
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("said_by", "label"),
    [
        ("user", None),
        ("agent", "agent-recorded"),
        ("mixed", "user + agent"),
        ("unknown", "provenance unknown"),
    ],
)
def test_provenance_label_dataclass_and_dict_agree(said_by, label) -> None:
    assert provenance_label(RecallProvenance(said_by=said_by)) == label
    assert provenance_label({"said_by": said_by}) == label


@pytest.mark.unit
def test_provenance_label_none_and_unrecognised() -> None:
    assert provenance_label(None) is None
    assert provenance_label({}) is None
    assert provenance_label({"said_by": "martian"}) is None


@pytest.mark.unit
def test_human_set_is_the_gate_set_not_a_copy(monkeypatch) -> None:
    # Identity: the derivation module must use the very object the intake gate owns.
    assert recall_provenance.GATE_APPROVED_HUMAN_SOURCES is self_identity.GATE_APPROVED_HUMAN_SOURCES
    # Behaviour: the gate set is read at call time through the module global, so widening the
    # gate's human set widens "user" here. A hard-coded {"user", "manual"} copy would stay agent.
    assert derive_recall_provenance({"sources": ["voice-dictation"]}).said_by == "agent"
    monkeypatch.setattr(
        recall_provenance,
        "GATE_APPROVED_HUMAN_SOURCES",
        frozenset(self_identity.GATE_APPROVED_HUMAN_SOURCES | {"voice-dictation"}),
    )
    assert derive_recall_provenance({"sources": ["voice-dictation"]}).said_by == "user"


@pytest.mark.unit
def test_every_gate_approved_source_classifies_as_user() -> None:
    for source in self_identity.GATE_APPROVED_HUMAN_SOURCES:
        assert derive_recall_provenance({"sources": [source]}).said_by == "user", source
