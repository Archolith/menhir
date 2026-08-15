from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from menhir.domain.typed_assertion import TypedAssertion
from menhir.services import typed_scalar_persistence
from menhir.services.typed_scalar_persistence import bind_and_persist_typed_scalars
from menhir.services.typed_scalar_rules import extract_typed_scalars_once, gate_typed_scalars

from spikes.mutation_kernel.kernel import Abstention, EvidenceRef, Retirement, View, make_assertion
from spikes.mutation_kernel.scalar_adapter import (
    SCALAR_VIEW,
    fold_typed_assertions,
    scalar_dimensions,
    typed_assertion_to_kernel,
    typed_assertion_to_row,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "scalar_ingest_fixture.json"


@dataclass(frozen=True)
class Episode:
    uuid: str
    content: str
    reference_time: str


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _episodes(data: dict) -> list[Episode]:
    return [Episode(**row) for row in data["episodes"]]


def _extract_fixture_samples(data: dict, key: str) -> list[list]:
    episodes = _episodes(data)
    samples = []
    for capture in data[key]:
        drops: list[str] = []
        proposals = extract_typed_scalars_once(
            episodes,
            lambda _system, _user, capture=capture: json.dumps(capture),
            on_drop=drops.append,
        )
        assert drops == []
        samples.append(proposals)
    return samples


def _typed(
    *,
    episode_uuid: str,
    value: object,
    valid_at: str,
    operation: str = "absolute",
    evidence_tier: str = "user",
    attribute: str = "token_balance",
    value_kind: str = "count",
    unit: str = "",
    scope: str = "",
) -> TypedAssertion:
    span = f"{operation}:{attribute}:{value!r}"
    return TypedAssertion(
        subject_uuid="fixture-user-uuid",
        subject_display="user",
        attribute=attribute,
        scope=scope,
        value_kind=value_kind,
        unit=unit,
        operation=operation,
        value=value,
        stated_span=span,
        episode_uuid=episode_uuid,
        valid_at=valid_at,
        learned_at=valid_at,
        span_start=0,
        span_end=len(span),
        time_basis="episode_reference",
        evidence_tier=evidence_tier,
        perceiver_version="v1",
    )


def test_frozen_model_capture_runs_real_scalar_ingest_boundary(monkeypatch) -> None:
    """Raw prose -> production parse/gate/bind/assertion/fold -> generic View.

    The only mocked boundary is the external model: its three responses are frozen in the fixture.
    Menhir's real deterministic post-model ingest code runs unchanged.
    """
    data = _fixture()
    episodes = _episodes(data)
    samples = _extract_fixture_samples(data, "success_samples")
    decisions = gate_typed_scalars(samples, threshold=1.0, canonical_self=True)

    assert len(decisions) == 2
    assert all(decision.committed for decision in decisions)

    # The persistence function emits audit telemetry as a side effect. The spike is testing ingest
    # semantics, not the configured telemetry sink, so keep that external effect inert.
    monkeypatch.setattr(typed_scalar_persistence._audit, "audit", lambda *args, **kwargs: None)

    rows: list[dict] = []
    rebuilds: list[object] = []
    reference_times = {episode.uuid: episode.reference_time for episode in episodes}

    def record_assertion(assertion: TypedAssertion) -> dict:
        row = typed_assertion_to_row(assertion)
        rows.append(row)
        return {
            "assertion_id": row["assertion_id"],
            "binding_pending": False,
            "binding_mismatch": False,
        }

    def rebuild_scalar_state(subject_uuid: str) -> dict:
        current = [row for row in rows if row["subject_uuid"] == subject_uuid]
        native, adapted = fold_typed_assertions(
            [
                _typed(
                    episode_uuid=row["episode_uuid"],
                    value=row["value"],
                    valid_at=row["valid_at"],
                    operation=row["operation"],
                    evidence_tier=row["evidence_tier"],
                    attribute=row["attribute"],
                    value_kind=row["value_kind"],
                    unit=row["unit"],
                    scope=row["scope"],
                )
                for row in current
            ]
        )
        rebuilds.append((native, adapted))
        return {"complete": True}

    result = bind_and_persist_typed_scalars(
        decisions,
        linked_entities_for_episode=lambda _episode_uuid: [],
        record_assertion=record_assertion,
        rebuild_scalar_state=rebuild_scalar_state,
        episode_reference_time=reference_times.get,
        namespace=data["namespace"],
        perceiver_version="v1",
        now=lambda: "2026-02-01T00:00:00+00:00",
        resolve_self_subject=lambda _namespace: (data["subject_uuid"], "user"),
    )

    assert result["persisted"] == 2
    assert result["bound"] == 2
    assert result["advisory"] == 0
    assert result["rebuilt"] == 2
    assert len(rows) == 2
    assert len(rebuilds) == 2

    # Run the production fold over the exact persisted row shape, then adapt only its result.
    assertions = []
    for row in rows:
        assertions.append(TypedAssertion(
            subject_uuid=row["subject_uuid"], subject_display=row["subject_display"],
            attribute=row["attribute"], scope=row["scope"], value_kind=row["value_kind"],
            unit=row["unit"], operation=row["operation"], value=row["value"],
            stated_span=row["stated_span"], episode_uuid=row["episode_uuid"],
            valid_at=row["valid_at"], learned_at=row["learned_at"],
            span_start=row["span_start"], span_end=row["span_end"],
            claim_ordinal=row["claim_ordinal"], time_basis=row["time_basis"],
            evidence_tier=row["evidence_tier"], perceiver_version=row["perceiver_version"],
            namespace=row["namespace"], absolute_semantics=row["absolute_semantics"],
        ))
    native, adapted = fold_typed_assertions(assertions)

    assert len(native.states) == 1
    state = native.states[0]
    assert state.value == 13
    assert state.anchor_value == 10
    assert state.delta_total == 3
    assert state.effective_tier == "agent"

    assert len(adapted) == 1
    view = adapted[0]
    assert isinstance(view, View)
    assert view.view_type == SCALAR_VIEW
    assert view.subject_id == data["subject_uuid"]
    assert view.scope == ""  # scalar's blank scope is a valid generic scope
    assert view.key == "token_balance"
    assert view.value == state.value
    assert view.contributor_ids == tuple(state.contributor_ids)
    assert view.effective_authority == state.effective_tier
    assert view.dimensions == scalar_dimensions("count", "")


def test_real_gate_abstains_on_conflicted_frozen_samples() -> None:
    data = _fixture()
    samples = _extract_fixture_samples(data, "conflict_samples")
    decisions = gate_typed_scalars(samples, threshold=1.0, canonical_self=True)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.committed is False
    assert decision.proposal is None
    assert decision.agreement == 2 / 3


def test_scalar_adapter_preserves_native_fold_math_and_weakest_authority() -> None:
    base = _typed(
        episode_uuid="base", value=10, valid_at="2026-01-01T00:00:00+00:00",
        evidence_tier="manual",
    )
    delta = _typed(
        episode_uuid="delta", value=3, valid_at="2026-01-02T00:00:00+00:00",
        operation="delta", evidence_tier="agent",
    )

    native, adapted = fold_typed_assertions([delta, base])
    assert native.states[0].value == 13
    assert native.states[0].effective_tier == "agent"
    assert len(adapted) == 1
    view = adapted[0]
    assert isinstance(view, View)
    assert view.value == 13
    assert view.effective_authority == "agent"
    assert view.contributor_ids == tuple(native.states[0].contributor_ids)

    # Input order must not change either native or adapted state.
    native_reversed, adapted_reversed = fold_typed_assertions([base, delta])
    assert native_reversed.states == native.states
    assert adapted_reversed == adapted


def test_scalar_ambiguity_stays_abstention_not_a_guessed_view() -> None:
    left = _typed(
        episode_uuid="left", value=10, valid_at="2026-01-01T00:00:00+00:00",
    )
    right = _typed(
        episode_uuid="right", value=11, valid_at="2026-01-01T00:00:00+00:00",
    )

    native, adapted = fold_typed_assertions([left, right])
    assert native.states == []
    assert len(native.abstentions) == 1
    assert len(adapted) == 1
    outcome = adapted[0]
    assert isinstance(outcome, Abstention)
    assert outcome.reason == native.abstentions[0].reason
    assert outcome.dimensions == scalar_dimensions("count", "")


def test_scalar_expiry_maps_to_retirement_not_abstention() -> None:
    base = _typed(
        episode_uuid="base", value=10, valid_at="2026-01-01T00:00:00+00:00",
    )
    expired = _typed(
        episode_uuid="expired", value=10, valid_at="2026-01-02T00:00:00+00:00",
        operation="expire",
    )

    native, adapted = fold_typed_assertions([base, expired])
    assert native.states == []
    assert native.abstentions == []
    assert len(native.expiries) == 1
    assert len(adapted) == 1
    outcome = adapted[0]
    assert isinstance(outcome, Retirement)
    assert outcome.reason == "expired"
    assert outcome.retired_value == 10
    assert outcome.effective_at == native.expiries[0].valid_at


def test_extension_dimensions_are_part_of_generic_identity() -> None:
    source = EvidenceRef("source-1", "test")
    count = make_assertion(
        source=source,
        assertion_type="example",
        subject_id="subject",
        scope="",
        key="value",
        value=1,
        valid_at="2026-01-01T00:00:00+00:00",
        dimensions=(("value_kind", "count"), ("unit", "")),
    )
    money = make_assertion(
        source=source,
        assertion_type="example",
        subject_id="subject",
        scope="",
        key="value",
        value=1,
        valid_at="2026-01-01T00:00:00+00:00",
        dimensions=(("unit", "usd"), ("value_kind", "money")),
    )

    assert count.assertion_id != money.assertion_id
    assert count.scope == money.scope == ""


def test_typed_assertion_adapter_preserves_production_identity_and_slot_axes() -> None:
    scalar = _typed(
        episode_uuid="scalar", value=12, valid_at="2026-01-01T00:00:00+00:00",
        value_kind="count", unit="items", scope="",
    )
    generic = typed_assertion_to_kernel(scalar)

    assert generic.assertion_id == scalar.assertion_key
    assert generic.source_key == scalar.source_key
    assert generic.subject_id == scalar.subject_uuid
    assert generic.scope == ""
    assert generic.key == scalar.attribute
    assert generic.dimensions == scalar_dimensions("count", "items")
    assert generic.evidence[0].source_id == scalar.episode_uuid
    assert generic.evidence[0].note == scalar.stated_span
