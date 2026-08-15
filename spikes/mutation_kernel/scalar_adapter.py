"""Adapter proving Menhir's production scalar fold can sit behind the mutation-kernel contract.

The rule of this spike is deliberately strict: scalar semantics remain owned by production Menhir.
This module does NOT reproduce absolute/delta/expire arithmetic, temporal ordering, ambiguity rules,
or authority calculation. It converts durable ``TypedAssertion`` objects to the generic assertion
envelope, calls the production ``fold_assertions`` implementation, then converts its outcomes to
kernel ``View`` / ``Abstention`` / ``Retirement`` envelopes.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Iterable

from menhir.domain.scalar_state_fold import (
    Abstention as ScalarAbstention,
    Expiry as ScalarExpiry,
    FoldResult,
    FoldedScalarState,
    fold_assertions,
)
from menhir.domain.typed_assertion import TypedAssertion

from .kernel import Abstention, Assertion, EvidenceRef, Retirement, View


SCALAR_ASSERTION = "scalar.observation"
SCALAR_VIEW = "scalar.state_view"


def scalar_dimensions(value_kind: str, unit: str) -> tuple[tuple[str, str], ...]:
    """Extra scalar slot axes that core preserves but does not interpret."""
    return tuple(sorted((
        ("unit", str(unit or "").strip().lower()),
        ("value_kind", str(value_kind).strip().lower()),
    )))


def typed_assertion_to_kernel(assertion: TypedAssertion) -> Assertion:
    """Wrap one production assertion without changing its durable identity or semantics."""
    evidence = EvidenceRef(
        source_id=assertion.episode_uuid,
        source_kind="menhir.episode",
        span_start=(assertion.span_start if assertion.span_start >= 0 else None),
        span_end=(assertion.span_end if assertion.span_end >= 0 else None),
        note=assertion.stated_span,
    )
    return Assertion(
        # Preserve Menhir's authoritative interpretation identity rather than recomputing a parallel
        # one in the spike. The generic kernel is an envelope, not a second scalar identity system.
        assertion_id=assertion.assertion_key,
        source_key=assertion.source_key,
        assertion_type=SCALAR_ASSERTION,
        subject_id=assertion.subject_uuid,
        scope=assertion.scope,
        key=assertion.attribute,
        value=assertion.value,
        valid_at=assertion.valid_at,
        learned_at=assertion.learned_at,
        authority=assertion.evidence_tier,
        confidence=1.0,
        evidence=(evidence,),
        interpreter_version=assertion.perceiver_version,
        metadata=tuple(sorted((
            ("absolute_semantics", assertion.absolute_semantics),
            ("operation", assertion.operation),
            ("time_basis", assertion.time_basis),
        ))),
        dimensions=scalar_dimensions(assertion.value_kind, assertion.unit),
    )


def typed_assertion_to_row(assertion: TypedAssertion) -> dict:
    """Repository-row shape accepted by the production scalar fold."""
    row = asdict(assertion)
    row["assertion_id"] = assertion.assertion_key
    row["source_key"] = assertion.source_key
    return row


def _state_to_view(state: FoldedScalarState) -> View:
    return View(
        view_type=SCALAR_VIEW,
        subject_id=state.subject_uuid,
        scope=state.scope,
        key=state.attribute,
        value=state.value,
        confidence=1.0,
        contributor_ids=tuple(state.contributor_ids),
        effective_authority=state.effective_tier,
        rationale=(
            f"anchor={state.anchor_value!r}",
            f"delta_total={state.delta_total!r}",
        ),
        dimensions=scalar_dimensions(state.value_kind, state.unit),
    )


def _abstention_to_kernel(abstention: ScalarAbstention) -> Abstention:
    subject_uuid, attribute, scope, value_kind, unit = abstention.slot_key
    return Abstention(
        view_type=SCALAR_VIEW,
        subject_id=subject_uuid,
        scope=scope,
        key=attribute,
        reason=abstention.reason,
        # Production ScalarStateFold currently does not carry the offending assertion IDs on an
        # abstention. The adapter deliberately does not invent them.
        assertion_ids=(),
        dimensions=scalar_dimensions(value_kind, unit),
    )


def _expiry_to_retirement(expiry: ScalarExpiry) -> Retirement:
    _subject_uuid, attribute, scope, value_kind, unit = expiry.slot_key
    return Retirement(
        view_type=SCALAR_VIEW,
        subject_id=expiry.subject_uuid,
        scope=scope,
        key=attribute,
        reason="expired",
        effective_at=expiry.valid_at,
        contributor_ids=tuple(expiry.contributor_ids),
        retired_value=expiry.expired_value,
        dimensions=scalar_dimensions(value_kind, unit),
    )


def adapt_fold_result(result: FoldResult) -> list[View | Abstention | Retirement]:
    """Loss-minimizing conversion of every production scalar fold outcome."""
    out: list[View | Abstention | Retirement] = []
    out.extend(_state_to_view(state) for state in result.states)
    out.extend(_abstention_to_kernel(item) for item in result.abstentions)
    out.extend(_expiry_to_retirement(item) for item in result.expiries)
    return sorted(
        out,
        key=lambda item: (
            item.subject_id,
            item.scope,
            item.key,
            item.dimensions,
            item.__class__.__name__,
        ),
    )


def fold_typed_assertions(
    assertions: Iterable[TypedAssertion], *, as_of: datetime | None = None,
) -> tuple[FoldResult, list[View | Abstention | Retirement]]:
    """Run the real scalar fold and expose the same result through the generic kernel."""
    rows = [typed_assertion_to_row(assertion) for assertion in assertions]
    native = fold_assertions(rows, as_of=as_of)
    return native, adapt_fold_result(native)
