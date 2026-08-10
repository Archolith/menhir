from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.repair_lme_scalar_source_times import (
    RepairRefusal,
    build_repair_plan,
    load_source_time_index,
    verify_backup_proof,
)


def _write_fixture(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "question_id": "postcards",
                    "haystack_sessions": [[{"role": "user", "content": "I have 20 postcards."}]],
                    "haystack_dates": ["2023/08/11 (Fri) 20:17"],
                    "haystack_session_ids": ["answer_cards_1"],
                },
                {
                    "question_id": "fallback",
                    "haystack_sessions": [[{"role": "user", "content": "I have 25 postcards."}]],
                    "haystack_dates": ["2023/11/30 (Thu) 20:25"],
                },
            ]
        ),
        encoding="utf-8",
    )
    return path


class _Executor:
    def __init__(self, evidence: list[dict], assertions: list[dict]) -> None:
        self.evidence = evidence
        self.assertions = assertions

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        if "MATCH (a:TypedAssertion)" in query:
            return self.assertions
        if "MATCH (t:TurnEvidence)" in query:
            return self.evidence
        raise AssertionError(query)


def test_fixture_index_matches_namespace_qualified_and_fallback_session_ids(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixture.json")
    index = load_source_time_index(fixture, "lme-")

    assert index.session_times[
        ("lme-postcards", "lme-postcards-answer_cards_1")
    ] == datetime(2023, 8, 11, 20, 17, tzinfo=timezone.utc)
    assert index.session_times[
        ("lme-fallback", "lme-fallback-s0")
    ] == datetime(2023, 11, 30, 20, 25, tzinfo=timezone.utc)
    assert index.fixture_sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()


def test_plan_repairs_evidence_and_assertion_without_changing_identity(tmp_path: Path) -> None:
    index = load_source_time_index(_write_fixture(tmp_path / "fixture.json"), "lme-")
    executor = _Executor(
        evidence=[
            {
                "turn_id": "turn-20",
                "namespace": "lme-postcards",
                "session_id": "lme-postcards-answer_cards_1",
                "occurred_at": None,
            }
        ],
        assertions=[
            {
                "assertion_id": "assert-20",
                "namespace": "lme-postcards",
                "episode_uuid": "turn-20",
                "subject_uuid": "entity-user",
                "binding_pending": False,
                "valid_at": "2026-07-29T14:00:00Z",
                "founded_turn_ids": ["turn-20"],
            }
        ],
    )

    plan = build_repair_plan(executor, index, "lme-")

    assert plan.evidence_updates == (
        {
            "turn_id": "turn-20",
            "old_occurred_at": None,
            "target": "2023-08-11T20:17:00+00:00",
        },
    )
    assert plan.assertion_updates == (
        {
            "assertion_id": "assert-20",
            "old_valid_at": "2026-07-29T14:00:00+00:00",
            "target": "2023-08-11T20:17:00+00:00",
        },
    )
    assert plan.rebuild_targets == (("entity-user", "lme-postcards"),)


def test_plan_is_idempotent_and_does_not_rebuild_unbound_subject(tmp_path: Path) -> None:
    index = load_source_time_index(_write_fixture(tmp_path / "fixture.json"), "lme-")
    executor = _Executor(
        evidence=[
            {
                "turn_id": "turn-20",
                "namespace": "lme-postcards",
                "session_id": "lme-postcards-answer_cards_1",
                "occurred_at": "2023-08-11T20:17:00Z",
            }
        ],
        assertions=[
            {
                "assertion_id": "assert-20",
                "namespace": "lme-postcards",
                "episode_uuid": "turn-20",
                "subject_uuid": "unbound:abc",
                "binding_pending": True,
                "valid_at": "2023-08-11T20:17:00Z",
                "founded_turn_ids": ["turn-20"],
            }
        ],
    )

    plan = build_repair_plan(executor, index, "lme-")

    assert plan.evidence_updates == ()
    assert plan.assertion_updates == ()
    assert plan.rebuild_targets == ()
    assert plan.evidence_already_correct == 1
    assert plan.assertions_already_correct == 1


def test_plan_refuses_non_null_conflicting_evidence_time(tmp_path: Path) -> None:
    index = load_source_time_index(_write_fixture(tmp_path / "fixture.json"), "lme-")
    executor = _Executor(
        evidence=[
            {
                "turn_id": "turn-20",
                "namespace": "lme-postcards",
                "session_id": "lme-postcards-answer_cards_1",
                "occurred_at": "2022-01-01T00:00:00Z",
            }
        ],
        assertions=[],
    )

    with pytest.raises(RepairRefusal, match="conflicting source time"):
        build_repair_plan(executor, index, "lme-")


def test_plan_refuses_assertion_without_exact_evidence_foundation(tmp_path: Path) -> None:
    index = load_source_time_index(_write_fixture(tmp_path / "fixture.json"), "lme-")
    executor = _Executor(
        evidence=[
            {
                "turn_id": "turn-20",
                "namespace": "lme-postcards",
                "session_id": "lme-postcards-answer_cards_1",
                "occurred_at": None,
            }
        ],
        assertions=[
            {
                "assertion_id": "assert-missing",
                "namespace": "lme-postcards",
                "episode_uuid": "turn-missing",
                "subject_uuid": "entity-user",
                "binding_pending": False,
                "valid_at": "2026-07-29T14:00:00Z",
                "founded_turn_ids": [],
            }
        ],
    )

    with pytest.raises(RepairRefusal, match="do not map exactly to TurnEvidence"):
        build_repair_plan(executor, index, "lme-")


def test_backup_proof_verifies_tarball_sha256(tmp_path: Path) -> None:
    snapshot = tmp_path / "graph-snapshot.tar.gz"
    snapshot.write_bytes(b"preserved graph")
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    metadata = tmp_path / "graph-snapshot.json"
    metadata.write_text(
        json.dumps(
            {
                "snapshot_type": "neo4j-volume-tarball",
                "file": snapshot.name,
                "sha256": digest,
            }
        ),
        encoding="utf-8",
    )

    proof = verify_backup_proof(metadata)

    assert proof["sha256"] == digest


def test_backup_proof_refuses_hash_mismatch(tmp_path: Path) -> None:
    snapshot = tmp_path / "graph-snapshot.tar.gz"
    snapshot.write_bytes(b"preserved graph")
    metadata = tmp_path / "graph-snapshot.json"
    metadata.write_text(
        json.dumps(
            {
                "snapshot_type": "neo4j-volume-tarball",
                "file": snapshot.name,
                "sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RepairRefusal, match="SHA-256 mismatch"):
        verify_backup_proof(metadata)
