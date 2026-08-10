"""Offline contract tests for the bounded scalar sample freezer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from menhir.services.deterministic_scalar_extractor import DeterministicScalarExtractor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from freeze_scalar_samples import (  # noqa: E402
    LEGACY_EPISODIC_Q,
    TURN_EVIDENCE_EXISTS_Q,
    TURN_EVIDENCE_Q,
    FreezeInputError,
    _load_graph_namespaces,
    _load_static_namespaces,
    main,
)


STATIC_FIXTURE_PAYLOAD = {
    "schema_version": 1,
    "fixture_id": "deterministic_scalar_heldout_v1",
    "non_lme": True,
    "namespaces": {
        "heldout-v1-fully-covered": {
            "purpose": "fully_covered",
            "episodes": [
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000001",
                    "content": "[2026-01-01] I have 37 coins.",
                },
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000002",
                    "content": "[2026-01-02] My savings balance is $500.",
                },
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000003",
                    "content": "[2026-01-03] I wake up at 7:30.",
                },
            ],
        },
        "heldout-v1-fallback-adversarial": {
            "purpose": "fallback_adversarial",
            "episodes": [
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000101",
                    "content": "[2026-01-01] My car is red.",
                },
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000102",
                    "content": "[2026-01-02] I have finished reading Dune.",
                },
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000103",
                    "content": "[2026-01-03] My day off is Wednesday.",
                },
                {
                    "uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000104",
                    "content": "[2026-01-04] I paid $250 for my headphones.",
                },
            ],
        },
    },
}


def _write_static_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "deterministic_scalar_heldout_v1.json"
    fixture.write_text(json.dumps(STATIC_FIXTURE_PAYLOAD), encoding="utf-8")
    return fixture


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return list(self._rows)


class _Session:
    def __init__(self, *, turn_evidence: bool, rows_by_query):
        self.turn_evidence = turn_evidence
        self.rows_by_query = rows_by_query
        self.calls = []

    def run(self, query, params=None):
        self.calls.append((query, params or {}))
        if query == TURN_EVIDENCE_EXISTS_Q:
            return _Result([{"c": 1 if self.turn_evidence else 0}])
        return _Result(self.rows_by_query.get(query, []))


def test_static_fixture_is_versioned_non_lme_and_preserves_exact_rows(tmp_path):
    namespaces, metadata = _load_static_namespaces(_write_static_fixture(tmp_path))

    assert metadata["fixture_id"] == "deterministic_scalar_heldout_v1"
    assert metadata["schema_version"] == 1
    assert set(namespaces) == {
        "heldout-v1-fully-covered",
        "heldout-v1-fallback-adversarial",
    }
    assert [episode.uuid for episode in namespaces["heldout-v1-fully-covered"]] == [
        "9d0f6b4e-0b3a-4f86-9f0f-000000000001",
        "9d0f6b4e-0b3a-4f86-9f0f-000000000002",
        "9d0f6b4e-0b3a-4f86-9f0f-000000000003",
    ]
    assert [episode.content for episode in namespaces["heldout-v1-fully-covered"]] == [
        "[2026-01-01] I have 37 coins.",
        "[2026-01-02] My savings balance is $500.",
        "[2026-01-03] I wake up at 7:30.",
    ]
    assert [episode.content for episode in namespaces["heldout-v1-fallback-adversarial"]] == [
        "[2026-01-01] My car is red.",
        "[2026-01-02] I have finished reading Dune.",
        "[2026-01-03] My day off is Wednesday.",
        "[2026-01-04] I paid $250 for my headphones.",
    ]


def test_static_fixture_partitions_router_eligibility_without_event_history(tmp_path):
    namespaces, _metadata = _load_static_namespaces(_write_static_fixture(tmp_path))
    extractor = DeterministicScalarExtractor()

    fully_covered = extractor.extract(namespaces["heldout-v1-fully-covered"])
    fallback = extractor.extract(namespaces["heldout-v1-fallback-adversarial"])

    assert len(fully_covered.episode_receipts) == 3
    assert all(receipt.fully_covered for receipt in fully_covered.episode_receipts)
    assert len(fallback.episode_receipts) == 4
    assert all(not receipt.fully_covered for receipt in fallback.episode_receipts)
    assert all(
        not (proposal.attribute == "event_history")
        for proposal in fully_covered.proposals + fallback.proposals
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("schema_version"),
        lambda payload: payload.__setitem__("non_lme", False),
        lambda payload: payload["namespaces"].pop("heldout-v1-fallback-adversarial"),
        lambda payload: payload["namespaces"]["heldout-v1-fully-covered"]["episodes"].append(
            {"uuid": "9d0f6b4e-0b3a-4f86-9f0f-000000000001", "content": "duplicate"}
        ),
    ],
    ids=["missing_version", "lme_input", "missing_fallback_namespace", "duplicate_uuid"],
)
def test_static_input_rejects_unsafe_schema(tmp_path, mutation):
    payload = json.loads(json.dumps(STATIC_FIXTURE_PAYLOAD))
    mutation(payload)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FreezeInputError):
        _load_static_namespaces(path)


def test_invalid_static_input_fails_closed_before_output_or_llm_key(tmp_path, monkeypatch):
    input_path = tmp_path / "invalid.json"
    input_path.write_text("{}", encoding="utf-8")
    output_path = tmp_path / "capture.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["freeze_scalar_samples.py", "--episodes-json", str(input_path), "--out", str(output_path)],
    )

    assert main() == 2
    assert not output_path.exists()


def test_graph_turn_evidence_source_matches_scheduler_and_uses_exact_namespace():
    session = _Session(
        turn_evidence=True,
        rows_by_query={
            TURN_EVIDENCE_Q: [
                {"uuid": "turn-1", "valid_at": "2026-08-01T00:00:00Z", "content": "I have 2 coins."}
            ]
        },
    )

    loaded, metadata = _load_graph_namespaces(session, ["heldout-exact"])

    assert metadata["source"] == "turn_evidence"
    assert loaded["heldout-exact"][0].uuid == "turn-1"
    assert loaded["heldout-exact"][0].content == "[2026-08-01] I have 2 coins."
    assert [query for query, _params in session.calls] == [TURN_EVIDENCE_EXISTS_Q, TURN_EVIDENCE_Q]
    assert session.calls[-1][1]["ns"] == "heldout-exact"
    assert "ENDS WITH" not in session.calls[-1][0]


def test_graph_legacy_source_matches_scheduler_and_uses_exact_namespace():
    session = _Session(
        turn_evidence=False,
        rows_by_query={
            LEGACY_EPISODIC_Q: [
                {
                    "uuid": "episode-1",
                    "valid_at": "2026-08-01T00:00:00Z",
                    "content": "user: I have 2 coins.",
                }
            ]
        },
    )

    loaded, metadata = _load_graph_namespaces(session, ["exact-group-id"])

    assert metadata["source"] == "episodic"
    assert loaded["exact-group-id"][0].content == "[2026-08-01] I have 2 coins."
    assert [query for query, _params in session.calls] == [TURN_EVIDENCE_EXISTS_Q, LEGACY_EPISODIC_Q]
    assert session.calls[-1][1]["ns"] == "exact-group-id"
    assert "ENDS WITH" not in session.calls[-1][0]


def test_graph_missing_exact_namespace_fails_closed():
    session = _Session(turn_evidence=False, rows_by_query={LEGACY_EPISODIC_Q: []})

    with pytest.raises(FreezeInputError, match="exact namespace"):
        _load_graph_namespaces(session, ["missing-exact"])


def test_graph_duplicate_namespace_selection_fails_closed():
    session = _Session(turn_evidence=False, rows_by_query={})

    with pytest.raises(FreezeInputError, match="duplicate exact namespaces"):
        _load_graph_namespaces(session, ["same", "same"])
