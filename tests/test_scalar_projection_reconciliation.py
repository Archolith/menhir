from __future__ import annotations

from typing import Any

import pytest

from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessCertificate,
    ProjectionWorkToken,
)
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION
from menhir.services.scalar_projection_reconciliation import ScalarProjectionReconciler


class _FakeLifecycle(ProjectionLifecycleRepository):
    def __init__(self) -> None:
        self.generation = 0
        self.dirty_calls: list[tuple[Any, tuple[Any, ...], str]] = []
        self.commit_calls: list[tuple[ProjectionWorkToken, str]] = []
        self.pending_tokens: list[ProjectionWorkToken] = []
        self.fail_work_key: str | None = None

    def dirty_targets(self, definition: Any, targets: Any, *, reason: str):
        targets = tuple(targets)
        self.dirty_calls.append((definition, targets, reason))
        tokens = []
        for target in targets:
            self.generation += 1
            token = ProjectionWorkToken(
                work_key=f"work-{self.generation}",
                definition_id=definition.definition_id,
                definition_version=definition.version,
                target=target,
                generation=self.generation,
                target_present=True,
                reason=reason,
            )
            tokens.append(token)
            self.pending_tokens.append(token)
        return tuple(tokens)

    def commit(self, token: ProjectionWorkToken, *, derivation_id: str, materialize: Any):
        self.commit_calls.append((token, derivation_id))
        if token.work_key == self.fail_work_key:
            raise RuntimeError("materialization failed")
        projection_hash = materialize(object(), token)
        self.pending_tokens.remove(token)
        return ProjectionFreshnessCertificate(
            definition_id=token.definition_id,
            definition_version=token.definition_version,
            target=token.target,
            generation=token.generation,
            target_present=token.target_present,
            projection_hash=projection_hash,
            derivation_id=derivation_id,
        )

    def pending(self, *, limit: int = 100):
        return tuple(self.pending_tokens[:limit])


class _Materializer:
    def __init__(self) -> None:
        self.tokens: list[ProjectionWorkToken] = []

    def __call__(self, _tx: Any, token: ProjectionWorkToken) -> str:
        self.tokens.append(token)
        return f"hash-{token.work_key}"


def _row(*, attribute: str = "height", namespace: str | None = "alpha") -> dict[str, Any]:
    return {
        "assertion_id": f"a-{attribute}",
        "subject_uuid": "entity-1",
        "namespace": namespace,
        "attribute": attribute,
        "scope": "",
        "value_kind": "number",
        "unit": "cm",
        "operation": "absolute",
    }


def test_reconcile_assertions_dirties_unique_exact_slots_then_certifies() -> None:
    lifecycle = _FakeLifecycle()
    materializer = _Materializer()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=materializer)

    result = reconciler.reconcile_assertions(
        [_row(), _row(), _row(attribute="weight")],
        operation_id="ingest-42",
    )

    assert len(lifecycle.dirty_calls) == 1
    definition, targets, reason = lifecycle.dirty_calls[0]
    assert definition is SCALAR_STATE_PROJECTION
    assert reason == "typed_scalar_mutation"
    assert [target.key[0] for target in targets] == ["height", "weight"]
    assert len(result) == 2
    assert materializer.tokens == [item.token for item in result]
    assert all(item.certificate.target == item.token.target for item in result)
    assert lifecycle.pending_tokens == []


def test_failed_materialization_leaves_generation_pending_for_recovery() -> None:
    lifecycle = _FakeLifecycle()
    materializer = _Materializer()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=materializer)
    tokens = reconciler.dirty_assertions([_row()])
    lifecycle.fail_work_key = tokens[0].work_key

    with pytest.raises(RuntimeError, match="materialization failed"):
        reconciler.commit_tokens(tokens, operation_id="ingest-43")

    assert lifecycle.pending_tokens == [tokens[0]]

    lifecycle.fail_work_key = None
    recovered = reconciler.drain_pending(operation_id="repair-1")
    assert [item.token for item in recovered] == [tokens[0]]
    assert lifecycle.pending_tokens == []


def test_drain_pending_ignores_other_projection_definitions() -> None:
    lifecycle = _FakeLifecycle()
    materializer = _Materializer()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=materializer)
    scalar = reconciler.dirty_assertions([_row()])[0]
    other = ProjectionWorkToken(
        work_key="other-work",
        definition_id="other.projection",
        definition_version=1,
        target=scalar.target,
        generation=1,
        target_present=True,
        reason="other",
    )
    lifecycle.pending_tokens.append(other)

    recovered = reconciler.drain_pending(operation_id="repair-2")

    assert [item.token for item in recovered] == [scalar]
    assert lifecycle.pending_tokens == [other]


def test_derivation_id_is_stable_for_same_operation_and_generation() -> None:
    lifecycle = _FakeLifecycle()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=_Materializer())
    token = reconciler.dirty_assertions([_row(namespace=None)])[0]

    first = reconciler.commit_tokens([token], operation_id="same-op")[0]
    # Reinsert the same generation to model an idempotent certification replay in a repository fake.
    lifecycle.pending_tokens.append(token)
    second = reconciler.commit_tokens([token], operation_id="same-op")[0]

    assert first.certificate.derivation_id == second.certificate.derivation_id
