"""Typed-scalar compatibility adapter for the generic projection contract.

The live ``ScalarStateService`` remains unchanged in this tranche. This adapter proves that its
existing deterministic fold can be expressed through the generic registration/evaluation seam
without moving scalar vocabulary into ``menhir.domain.projection``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from menhir.domain.namespace import normalize_namespace
from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionDefinition,
    ProjectionMaterialization,
    ProjectionOutcome,
    ProjectionRetirement,
    ProjectionTarget,
)
from menhir.domain.scalar_state_fold import fold_assertions

__all__ = ["SCALAR_STATE_PROJECTION"]

_SCALAR_INPUT_TYPES = frozenset(
    {
        "typed_scalar.absolute",
        "typed_scalar.delta",
        "typed_scalar.expire",
    }
)


def _assertion_id(assertion: object) -> str:
    row = _row(assertion)
    return str(row.get("assertion_id") or "")


def _assertion_type(assertion: object) -> str:
    row = _row(assertion)
    operation = str(row.get("operation") or "").strip().lower()
    return f"typed_scalar.{operation}"


def _target(assertion: object) -> ProjectionTarget:
    row = _row(assertion)
    return ProjectionTarget(
        subject_id=str(row.get("subject_uuid") or ""),
        namespace=normalize_namespace(row.get("namespace")),
        key=(
            str(row.get("attribute") or "").strip().lower(),
            str(row.get("scope") or "").strip().lower(),
            str(row.get("value_kind") or "").strip().lower(),
            str(row.get("unit") or "").strip().lower(),
        ),
    )


def _fold(
    target: ProjectionTarget,
    assertions: tuple[object, ...],
    as_of: datetime | None,
) -> ProjectionOutcome:
    rows = [_row(assertion) for assertion in assertions]
    result = fold_assertions(rows, as_of=as_of)
    outcome_count = len(result.states) + len(result.abstentions) + len(result.expiries)
    if outcome_count == 0:
        # Existing ScalarStateService treats an absent desired slot as non-current and reconciles any
        # stored View away. The common case here is a target whose only assertions are future-dated at
        # ``as_of``. Preserve that semantics as an explicit desired retirement rather than an error.
        return ProjectionRetirement(target=target, reason="no_active_assertions")
    if outcome_count != 1:
        raise ValueError(
            "scalar projection target must fold to at most one state, abstention, or expiry"
        )

    if result.states:
        state = result.states[0]
        return ProjectionMaterialization(
            target=target,
            view_kind="scalar_state",
            payload=state,
            contributor_ids=tuple(state.contributor_ids),
            valid_at=state.valid_at,
        )
    if result.abstentions:
        abstention = result.abstentions[0]
        return ProjectionAbstention(
            target=target,
            reason=abstention.reason,
        )

    expiry = result.expiries[0]
    return ProjectionRetirement(
        target=target,
        reason="expired",
        contributor_ids=tuple(expiry.contributor_ids),
    )


def _row(assertion: object) -> dict[str, Any]:
    if not isinstance(assertion, dict):
        raise TypeError("scalar projection expects TypedAssertion row dictionaries")
    return assertion


SCALAR_STATE_PROJECTION = ProjectionDefinition(
    definition_id="typed_scalar.current_state",
    version=1,
    input_assertion_types=_SCALAR_INPUT_TYPES,
    output_view_kind="scalar_state",
    assertion_id_resolver=_assertion_id,
    assertion_type_resolver=_assertion_type,
    target_resolver=_target,
    fold=_fold,
)
