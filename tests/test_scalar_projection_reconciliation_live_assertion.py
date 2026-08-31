from types import SimpleNamespace

from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.services.scalar_projection_reconciliation import ScalarProjectionReconciler


class _Lifecycle(ProjectionLifecycleRepository):
    def __init__(self) -> None:
        self.targets = ()

    def dirty_targets(self, _definition, targets, *, reason):
        self.targets = tuple(targets)
        return ()


class _Materializer:
    def __call__(self, _tx, _token):
        raise AssertionError("no materialization expected")


def test_dirty_assertions_accepts_live_typed_scalar_shape() -> None:
    lifecycle = _Lifecycle()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=_Materializer())
    assertion = SimpleNamespace(
        subject_uuid="entity-1",
        namespace="default",
        attribute="Height",
        scope="",
        value_kind="Number",
        unit="CM",
        operation="absolute",
    )

    assert reconciler.dirty_assertions([assertion]) == ()
    assert len(lifecycle.targets) == 1
    target = lifecycle.targets[0]
    assert target.subject_id == "entity-1"
    assert target.key == ("height", "", "number", "cm")
