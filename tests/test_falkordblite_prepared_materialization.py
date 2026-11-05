"""Offline contract checks for the isolated prepared-scalar experiment."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepared_scalar_proof", ROOT / "scripts" / "falkordblite_prepared_materialization.py"
)
assert SPEC is not None and SPEC.loader is not None
prepared = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prepared
SPEC.loader.exec_module(prepared)


def _row() -> dict[str, object]:
    return {
        "assertion_id": "a1",
        "subject_uuid": "subject",
        "subject_display": "Alice",
        "namespace": "tenant-a",
        "attribute": "owned",
        "scope": "",
        "value_kind": "count",
        "unit": "count",
        "operation": "absolute",
        "value": 10,
        "valid_at": "2026-01-01T00:00:00+00:00",
        "learned_at": "2026-01-01T00:00:00+00:00",
        "evidence_tier": "user",
        "episode_uuid": "episode-a1",
        "absolute_semantics": "ordinary",
    }


def _plan(*, present: bool = True):
    from menhir.domain.projection_lifecycle import ProjectionWorkToken

    target = prepared.scalar_projection_target(_row())
    definition_id = prepared.SCALAR_STATE_PROJECTION.definition_id
    token = ProjectionWorkToken(
        definition_id=definition_id,
        definition_version=1,
        target=target,
        work_key=prepared._work_key(definition_id, target),
        generation=1,
        target_present=present,
        reason="test",
    )
    snapshot = [
        [{"definition_id": definition_id, "current_version": 1, "semantic_hash": "proof-v1"}],
        [
            {
                "work_key": token.work_key,
                "definition_id": definition_id,
                "definition_version": 1,
                "target_json": prepared._target_json(target),
                "generation": 1,
                "target_present": present,
                "applied_generation": 0,
            }
        ],
        [_row()],
        [{"work_key": token.work_key, "namespace": "tenant-a", "subject_uuid": "subject", "payload_json": "old"}],
        [],
        int(datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp() * 1000),
    ]
    return prepared.PreparedScalar(token, "derive", json.dumps(snapshot))


def test_preparation_uses_real_fold_and_certifies_canonical_payload() -> None:
    params = _plan().parameters()
    payload = json.loads(params["after_view"]["payload_json"])
    assert payload["value"] == "10"
    assert payload["contributor_ids"] == ["a1"]
    assert payload["episode_uuids"] == ["episode-a1"]
    assert params["after_view"]["projection_hash"] == "e05c6e3a187e3ed040031d69ece34e302f386d61d1b4891b02c1a710afb49f48"
    assert params["after_work"]["certified_projection_hash"] == params["after_receipt"]["projection_hash"]
    assert params["after_work"]["certified_definition_version"] == 1
    assert params["after_work"]["applied_projection_hash"] == params["after_view"]["projection_hash"]


def test_retirement_certifies_absence_instead_of_old_value() -> None:
    params = _plan(present=False).parameters()
    assert params["after_view"]["present"] is False
    assert json.loads(params["after_view"]["payload_json"]) == {"present": False}
    assert params["after_view"]["projection_hash"] == "0f1e96d4f4f9a0dda2609a28d3531661535629abfd80e0ae1d9488b8fecc3379"


def test_future_assertion_sets_deadline_and_is_not_folded_early() -> None:
    plan = _plan()
    data = json.loads(plan.snapshot_json)
    activation = data[-1] + 250
    future = {
        **_row(),
        "assertion_id": "a2",
        "value": 20,
        "valid_at": datetime.fromtimestamp(activation / 1000, tz=UTC).isoformat(),
    }
    data[2].append(future)
    before = replace(plan, snapshot_json=json.dumps(data)).parameters()
    assert before["valid_until_ms"] == activation
    assert json.loads(before["after_view"]["payload_json"])["value"] == "10"
    data[-1] = activation + 1
    after = replace(plan, snapshot_json=json.dumps(data)).parameters()
    assert json.loads(after["after_view"]["payload_json"])["value"] == "20"
    assert after["after_view"]["projection_hash"] != before["after_view"]["projection_hash"]


def test_parameter_mutation_cannot_change_later_dispatch() -> None:
    plan = _plan()
    params = plan.parameters()
    params["after_view"]["payload_json"] = "forged"
    params["after_receipt"]["projection_hash"] = "forged"
    assert plan.parameters()["after_view"]["payload_json"] != "forged"
    assert plan.parameters()["after_receipt"]["projection_hash"] != "forged"
    assert params["valid_until_ms"] - params["as_of_ms"] == prepared.MAX_AGE_MS


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_definition",
        "duplicate_view",
        "existing_receipt",
        "foreign_input",
        "foreign_view",
        "oversize",
        "duplicate_input",
        "stale_generation",
        "bad_clock",
    ],
)
def test_invalid_snapshot_refuses_before_dispatch(case: str) -> None:
    plan = _plan()
    data = json.loads(plan.snapshot_json)
    if case == "duplicate_definition":
        data[0] *= 2
    elif case == "duplicate_view":
        data[3] *= 2
    elif case == "existing_receipt":
        data[4] = [{"receipt_key": "conflicting"}]
    elif case == "foreign_input":
        data[2][0]["namespace"] = "tenant-b"
    elif case == "foreign_view":
        data[3][0]["namespace"] = "tenant-b"
    elif case == "oversize":
        data[2] = [{**_row(), "assertion_id": str(i)} for i in range(prepared.MAX_INPUTS + 1)]
    elif case == "duplicate_input":
        data[2] *= 2
    elif case == "stale_generation":
        data[1][0]["generation"] += 1
    elif case == "bad_clock":
        data[-1] = True
    with pytest.raises(ValueError):
        replace(plan, snapshot_json=json.dumps(data)).parameters()


def test_target_cannot_be_rebound_to_another_namespace() -> None:
    plan = _plan()
    foreign = replace(plan.token.target, namespace="tenant-b")
    with pytest.raises(ValueError, match="work key/target"):
        replace(plan, token=replace(plan.token, target=foreign)).parameters()


def test_unknown_fold_outcome_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        prepared,
        "SCALAR_STATE_PROJECTION",
        replace(
            prepared.SCALAR_STATE_PROJECTION,
            fold=lambda *_args: object(),
        ),
    )
    with pytest.raises(TypeError, match="unsupported scalar fold"):
        _plan().parameters()


def test_blank_canonical_hash_cannot_be_certified(monkeypatch) -> None:
    monkeypatch.setattr(prepared, "scalar_projection_present_hash", lambda *_a, **_kw: "")
    with pytest.raises(ValueError, match="invalid prepared projection hash"):
        _plan().parameters()


def test_preparation_folds_anchor_and_later_delta() -> None:
    plan = _plan()
    data = json.loads(plan.snapshot_json)
    data[2].append(
        {
            **_row(),
            "assertion_id": "d1",
            "operation": "delta",
            "value": 2,
            "valid_at": "2026-01-02T00:00:00+00:00",
            "episode_uuid": "episode-d1",
        }
    )
    payload = json.loads(replace(plan, snapshot_json=json.dumps(data)).parameters()["after_view"]["payload_json"])
    assert payload["value"] == "12"
    assert payload["contributor_ids"] == ["a1", "d1"]


def test_candidate_slot_is_accepted_by_production_view_kind() -> None:
    from menhir.infrastructure.view_models import ScalarStateKind

    # The fold accepts broader inputs than the persisted View schema. Check that boundary too.
    row = _row()
    kind = ScalarStateKind()
    discriminator = kind.key_discriminator(row)
    props = kind.write_props("Alice", discriminator, row)
    assert props["ss_kind"] == "count"
    assert props["ss_value"] == "10"


@pytest.mark.parametrize("value_kind", ["boolean", "number"])
def test_unsupported_scalar_kind_is_not_silently_certified(value_kind: str) -> None:
    plan = _plan()
    target = replace(plan.token.target, key=("owned", "", value_kind, "count"))
    with pytest.raises(ValueError, match="numeric scalar targets only"):
        replace(plan, token=replace(plan.token, target=target)).parameters()


def _source_plan():
    plan = _plan()
    data = json.loads(plan.snapshot_json)
    data[2] = {
        "sources": [
            {
                "assertion": {**_row(), "episode_uuid": "turn-a"},
                "assertion_node": 1,
                "turn": {
                    "turn_id": "turn-a",
                    "namespace": "tenant-a",
                    "evidence_finalized": True,
                    "evidence_generation": 0,
                },
                "turn_node": 2,
                "episode": {"uuid": "episode-a1", "group_id": "tenant-a"},
                "episode_node": 3,
                "admission_node": 4,
                "grounding": {
                    "turn_id": "turn-a",
                    "namespace": "tenant-a",
                    "evidence_finalized": True,
                    "evidence_generation": 0,
                },
                "grounding_node": 2,
                "grounding_edge": 4,
                "direct_node": None,
                "alias_node": 3,
                "collision_node": None,
            }
        ],
        "fences": [{"namespace_key": "tenant-a", "generation": 0}],
    }
    return prepared.PreparedProductionScalar(plan.token, plan.derivation_id, json.dumps(data))


def test_source_snapshot_derives_hash_from_admitted_episode() -> None:
    plan = _source_plan()
    params = plan.parameters()
    assert params["after_view"] == _plan().parameters()["after_view"]
    assert params["inputs"] == json.loads(plan.snapshot_json)[2]
    # A stored hydrated value wins over the compatibility numeric mirror, as in the real read.
    data = json.loads(plan.snapshot_json)
    data[2]["sources"][0]["assertion"].update(value=99, value_json="10")
    assert replace(plan, snapshot_json=json.dumps(data)).parameters()["after_view"] == params["after_view"]


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("quarantine", "not live"),
        ("unfinalized", "not live"),
        ("fence", "stale source evidence fence"),
        ("foreign", "foreign source evidence"),
        ("direct", "ambiguous source evidence identity"),
        ("collision", "ambiguous source evidence identity"),
        ("grounding", "ambiguous source evidence identity"),
        ("missing_turn", "turn-backed admission"),
        ("duplicate", "ambiguous source provenance"),
        ("duplicate_fence", "missing/duplicate evidence fence"),
        ("overflow", "source closure bound"),
        ("finalized_episode", "unfinalized episode bridge"),
        ("invalid_flag", "invalid source eligibility flag"),
        ("invalid_evidence_flag", "invalid source evidence flag"),
    ],
)
def test_source_preparation_refuses_ineligible_or_ambiguous_evidence(mutation: str, message: str) -> None:
    plan = _source_plan()
    data = json.loads(plan.snapshot_json)
    bundle = data[2]
    source = bundle["sources"][0]
    if mutation == "quarantine":
        source["turn"]["evidence_quarantined"] = True
    elif mutation == "unfinalized":
        source["turn"]["evidence_finalized"] = False
    elif mutation == "fence":
        bundle["fences"][0]["generation"] = 1
    elif mutation == "foreign":
        source["episode"]["group_id"] = "tenant-b"
    elif mutation in ("direct", "collision", "grounding"):
        source[mutation + "_node"] = 999
    elif mutation == "missing_turn":
        source["turn"] = None
    elif mutation == "duplicate":
        bundle["sources"].append(source)
    elif mutation == "duplicate_fence":
        bundle["fences"].append(bundle["fences"][0])
    elif mutation == "overflow":
        bundle["sources"] *= prepared.MAX_INPUTS + 1
    elif mutation == "finalized_episode":
        source["episode"]["evidence_finalized"] = True
    elif mutation == "invalid_flag":
        source["assertion"]["binding_pending"] = "false"
    elif mutation == "invalid_evidence_flag":
        source["turn"]["evidence_quarantined"] = 0
    with pytest.raises(ValueError, match=message):
        replace(plan, snapshot_json=json.dumps(data)).parameters()


@pytest.mark.parametrize("flag", ["superseded", "binding_pending"])
def test_source_snapshot_excludes_ineligible_assertions_but_guards_their_properties(flag: str) -> None:
    plan = _source_plan()
    data = json.loads(plan.snapshot_json)
    data[2]["sources"][0]["assertion"][flag] = True
    params = replace(plan, snapshot_json=json.dumps(data)).parameters()
    assert params["after_view"]["present"] is False
    assert params["inputs"]["sources"][0]["assertion"][flag] is True
