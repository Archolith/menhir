"""Generic projection rebuild/reconciliation mechanics for the mutation-kernel spike.

The lifecycle layer does not know what an assertion means. It knows only how to select one exact
slot, apply kernel supersession, ask an extension fold for the current answer, validate that the fold
did not escape the requested slot, and atomically replace the disposable persisted projection.

This makes the assertion write and projection write intentionally separable: a crash after durable
assertion storage may leave a stale projection, but deterministic reconciliation can repair it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .kernel import Abstention, Assertion, current_assertions
from .neo4j_store import ProjectionOutcome


@dataclass(frozen=True)
class ProjectionSlot:
    """The exact assertion/projection slot to rebuild."""

    assertion_type: str
    view_type: str
    subject_id: str
    scope: str
    key: str
    dimensions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("assertion_type", "view_type", "subject_id", "key"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"ProjectionSlot.{name} must be non-empty")
        canonical = tuple(sorted((str(name), str(value)) for name, value in self.dimensions))
        names = [name for name, _ in canonical]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ValueError("ProjectionSlot.dimensions must have unique non-empty names")
        if self.dimensions != canonical:
            raise ValueError("ProjectionSlot.dimensions must be canonical")


class ProjectionStore(Protocol):
    def load_assertions(
        self,
        *,
        subject_id: str | None = None,
        assertion_type: str | None = None,
        scope: str | None = None,
        key: str | None = None,
        dimensions: tuple[tuple[str, str], ...] | None = None,
    ) -> list[Assertion]: ...

    def record_outcome(self, outcome: ProjectionOutcome) -> dict[str, object]: ...


FoldFunction = Callable[[list[Assertion]], ProjectionOutcome]


@dataclass(frozen=True)
class ReconcileResult:
    """One deterministic rebuild and the persistence change it caused."""

    outcome: ProjectionOutcome
    changed: bool
    assertion_count: int
    current_assertion_count: int
    previous_hash: str | None
    projection_hash: str


def rebuild_projection(
    store: ProjectionStore,
    slot: ProjectionSlot,
    fold: FoldFunction,
) -> ReconcileResult:
    """Rebuild one current projection from durable assertion history.

    The kernel applies explicit supersession before handing rows to the extension fold. If
    supersession leaves no current evidence, the lifecycle layer persists an explicit abstention
    instead of leaving a stale View behind.
    """
    assertions = store.load_assertions(
        subject_id=slot.subject_id,
        assertion_type=slot.assertion_type,
        scope=slot.scope,
        key=slot.key,
        dimensions=slot.dimensions,
    )
    if not assertions:
        raise ValueError("cannot rebuild projection without persisted assertions")

    current = current_assertions(assertions)
    if current:
        outcome = fold(current)
    else:
        outcome = Abstention(
            view_type=slot.view_type,
            subject_id=slot.subject_id,
            scope=slot.scope,
            key=slot.key,
            reason="no_current_evidence",
            assertion_ids=tuple(row.assertion_id for row in assertions),
            dimensions=slot.dimensions,
        )

    _validate_outcome_slot(outcome, slot)
    write = store.record_outcome(outcome)
    return ReconcileResult(
        outcome=outcome,
        changed=bool(write["changed"]),
        assertion_count=len(assertions),
        current_assertion_count=len(current),
        previous_hash=(
            None if write.get("previous_hash") is None else str(write["previous_hash"])
        ),
        projection_hash=str(write["projection_hash"]),
    )


def _validate_outcome_slot(outcome: ProjectionOutcome, slot: ProjectionSlot) -> None:
    actual = (
        outcome.view_type,
        outcome.subject_id,
        outcome.scope,
        outcome.key,
        outcome.dimensions,
    )
    expected = (
        slot.view_type,
        slot.subject_id,
        slot.scope,
        slot.key,
        slot.dimensions,
    )
    if actual != expected:
        raise ValueError(
            "fold returned an outcome for a different projection slot: "
            f"expected={expected!r} actual={actual!r}"
        )
