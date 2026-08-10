"""Pure tests for the shadow-only compositional scalar identity sidecar."""

from dataclasses import replace

import pytest

from menhir.services.compositional_scalar_identity import (
    COMPOSITION_VERSION,
    ScalarIdentityProvenance,
    compose_scalar_identity,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal


def _proposal(**overrides):
    values = {
        "subject_text": "user",
        "attribute": "custom_collectible_count",
        "scope": "Blue shelf",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 37,
        "stated_span": "I have 37 hand-painted tokens",
        "episode_uuid": "episode-1",
        "span_start": 0,
        "span_end": 29,
        "when": None,
    }
    values.update(overrides)
    return TypedScalarProposal(**values)


def test_fallback_preserves_arbitrary_open_target_and_scope_without_registry():
    identity = compose_scalar_identity(_proposal())

    assert identity.relation_type == "state"
    assert identity.target_or_scope == ("custom_collectible_count", "blue shelf")
    assert identity.provenance.rule_id == "fallback_state"
    assert identity.provenance.derivation_version == COMPOSITION_VERSION


def test_caller_supplied_relation_and_open_target_are_compositional():
    identity = compose_scalar_identity(
        _proposal(),
        relation_type="quantity",
        target_or_scope=("hand-painted tokens", "display case"),
        derivation_kind="structural_grammar",
        rule_id="quantity_possession_v1",
    )

    assert identity.relation_type == "quantity"
    assert identity.target_or_scope == ("hand-painted tokens", "display case")
    assert identity.provenance.derivation_kind == "structural_grammar"
    assert identity.provenance.rule_id == "quantity_possession_v1"


def test_scalar_value_normalization_and_semantic_keys_are_stable():
    integer = compose_scalar_identity(_proposal(value=37))
    decimal = compose_scalar_identity(_proposal(value=37.0))

    assert integer.value == decimal.value == "37"
    assert integer.semantic_key == decimal.semantic_key
    assert integer.claim_key == decimal.claim_key
    assert compose_scalar_identity(_proposal()) == integer


def test_rule_receipt_version_does_not_override_composition_hash_version():
    first = compose_scalar_identity(_proposal(), derivation_version="fallback-rule-v1")
    second = compose_scalar_identity(_proposal(), derivation_version="fallback-rule-v2")

    assert first.provenance.derivation_version != second.provenance.derivation_version
    assert first.composition_version == second.composition_version == COMPOSITION_VERSION
    assert first.semantic_key == second.semantic_key
    assert first.claim_key == second.claim_key


def test_provenance_changes_claim_key_but_not_semantic_key():
    first = compose_scalar_identity(_proposal())
    moved = compose_scalar_identity(replace(
        _proposal(), episode_uuid="episode-2", span_start=10, span_end=39))

    assert first.semantic_key == moved.semantic_key
    assert first.provenance.source_key != moved.provenance.source_key
    assert first.claim_key != moved.claim_key


def test_canonical_self_is_explicit_opt_in():
    raw = compose_scalar_identity(_proposal(subject_text="I"))
    canonical = compose_scalar_identity(_proposal(subject_text="I"), canonical_self=True)
    already_canonical = compose_scalar_identity(
        _proposal(subject_text="the user"), canonical_self=True)

    assert raw.subject == "i"
    assert canonical.subject == "user"
    assert canonical.semantic_key == already_canonical.semantic_key


def test_missing_effective_time_stays_none_and_never_uses_wall_clock():
    undated = compose_scalar_identity(_proposal(when=None))
    dated = compose_scalar_identity(_proposal(when="2026-01-03T00:00:00+00:00"))

    assert undated.effective_time is None
    assert dated.effective_time == "2026-01-03T00:00:00+00:00"
    assert undated.semantic_key != dated.semantic_key


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("relation_type", "balance"),
        ("target_or_scope", ("different target", "blue shelf")),
    ],
)
def test_relation_or_target_change_semantic_identity(change, value):
    base = compose_scalar_identity(_proposal())
    kwargs = {change: value}
    changed = compose_scalar_identity(
        _proposal(),
        derivation_kind="test_structural_rule",
        rule_id="test_rule_v1",
        **kwargs,
    )

    assert base.semantic_key != changed.semantic_key


def test_value_unit_operation_and_effective_time_change_semantic_identity():
    base_proposal = _proposal()
    base = compose_scalar_identity(base_proposal)

    variants = [
        replace(base_proposal, value=38),
        replace(base_proposal, unit="items"),
        replace(base_proposal, operation="delta"),
        replace(base_proposal, when="2026-01-03T00:00:00+00:00"),
    ]
    assert all(compose_scalar_identity(item).semantic_key != base.semantic_key for item in variants)


def test_invalid_relation_and_offsets_fail_closed():
    with pytest.raises(ValueError, match="relation_type"):
        compose_scalar_identity(_proposal(), relation_type="benchmark_specific_relation")

    with pytest.raises(ValueError, match="grounded offsets"):
        compose_scalar_identity(_proposal(span_start=-1, span_end=-1))


def test_custom_semantics_require_an_explicit_derivation_receipt():
    with pytest.raises(ValueError, match="explicit derivation_kind and rule_id"):
        compose_scalar_identity(
            _proposal(), relation_type="quantity", target_or_scope=("tokens", ""))


def test_direct_provenance_rejects_a_forged_source_key():
    with pytest.raises(ValueError, match="source_key does not match"):
        ScalarIdentityProvenance(
            episode_uuid="episode-1",
            span_start=0,
            span_end=29,
            claim_ordinal=0,
            source_key="forged",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("subject", "User", "canonical"),
        ("target_or_scope", ("", ""), "non-blank target or scope"),
        ("value_kind", "invented_kind", "value_kind"),
        ("operation", "multiply", "operation"),
        ("effective_time", "", "effective_time"),
    ],
)
def test_direct_identity_construction_rejects_malformed_components(field, value, message):
    identity = compose_scalar_identity(_proposal())

    with pytest.raises(ValueError, match=message):
        replace(identity, **{field: value})
