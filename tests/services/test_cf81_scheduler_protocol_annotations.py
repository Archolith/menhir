"""CF-81: ``SchedulerLifecycleService`` return-type annotations resolve to the real classes.

``ConsolidationResult`` and ``DecayResult`` were referenced as return types in the Protocol but
never imported, so a type checker could not resolve them and the Protocol under-specified its own
contract. They are now imported under TYPE_CHECKING from ``services/lifecycle_models``, matching
how ``ProjectScanResult`` is already handled in the same file.
"""

from __future__ import annotations

import typing

import pytest

import menhir.infrastructure.project_scanner as _project_scanner
import menhir.infrastructure.schema as _schema  # noqa: F401  (bootstrap import smoke)
import menhir.services.lifecycle_models as _lifecycle
import menhir.services.scheduler_protocols as m
from menhir.services.lifecycle_models import ConsolidationResult, DecayResult

pytestmark = pytest.mark.unit


def test_lifecycle_annotations_resolve_to_real_classes() -> None:
    """The finding: the Protocol's ``recover_orphans`` and ``apply_decay`` annotations resolve
    without raising, and the resolved types are the real ``ConsolidationResult`` / ``DecayResult``
    classes -- proving the import names/paths are correct and cycle-free."""
    recover = typing.get_type_hints(
        m.SchedulerLifecycleService.recover_orphans,
        globalns=dict(vars(_lifecycle)),
    )
    decay = typing.get_type_hints(
        m.SchedulerLifecycleService.apply_decay,
        globalns=dict(vars(_lifecycle)),
    )
    assert recover["return"] is ConsolidationResult
    assert decay["return"] is DecayResult


def test_project_scan_result_still_resolves() -> None:
    """POSITIVE CONTROL: the existing ``ProjectScanResult`` reference (already imported under
    TYPE_CHECKING) still resolves -- so the test exercises real resolution, not vacuous success."""
    from menhir.infrastructure.project_scanner import ProjectScanResult

    write = typing.get_type_hints(
        m.SchedulerGraphAdapter.write_project_structure,
        globalns=dict(vars(_project_scanner)),
    )
    assert write["scan"] is ProjectScanResult
