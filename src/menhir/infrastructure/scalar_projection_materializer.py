"""Transaction-scoped materializer for one typed-scalar projection target.

This is deliberately slot-scoped: one T5 ProjectionWorkToken may mutate only its exact
ProjectionTarget. Entity-wide scalar rebuild remains a compatibility path until live cutover.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params
from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionMaterialization,
    ProjectionRetirement,
)
from menhir.domain.projection_lifecycle import (
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
)
from menhir.infrastructure.realization_coverage_repository import (
    ScalarStateProjectionHashSource,
)
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.view_repository import ViewRepository
from menhir.services.scalar_projection_definition import (
    SCALAR_STATE_PROJECTION,
    scalar_projection_target,
)
from menhir.services.scalar_projection_hash import (
    scalar_projection_absent_hash,
    scalar_projection_present_hash,
)

logger = logging.getLogger(__name__)

__all__ = ["ScalarStateProjectionMaterializer"]


def _require_scalar_target(token: ProjectionWorkToken) -> None:
    if token.definition_id != SCALAR_STATE_PROJECTION.definition_id:
        raise ValueError("scalar materializer received work for a different projection definition")
    if token.definition_version != SCALAR_STATE_PROJECTION.version:
        raise ValueError("scalar materializer received work for a different projection version")
    if len(token.target.key) != 4:
        raise ValueError("scalar projection target key must contain four slot parts")


def _current_view_keys(tx: Any, token: ProjectionWorkToken) -> tuple[str, ...]:
    attribute, scope, value_kind, unit = token.target.key
    rows = tx.execute(
        f"""
        MATCH (n:Entity {{view_kind:'scalar_state', view_subject_uuid:$subject_uuid,
                         ss_attribute:$attribute, ss_kind:$value_kind}})
        WHERE coalesce(n.view_current, true)
          AND coalesce(n.ss_scope, '') = $scope
          AND coalesce(n.ss_unit, '') = $unit
          AND {tenant_scope_cypher("n")}
        RETURN n.view_key AS view_key
        """,
        {
            "subject_uuid": token.target.subject_id,
            "attribute": attribute,
            "scope": scope,
            "value_kind": value_kind,
            "unit": unit,
            **tenant_scope_params(token.target.namespace),
        },
    )
    keys = tuple(str(row.get("view_key") or "") for row in rows)
    if len(keys) > 1:
        raise ProjectionLifecycleCorruptionError(
            "multiple current ScalarStateViews exist for one materialization target"
        )
    if any(not key for key in keys):
        raise ProjectionLifecycleCorruptionError("current ScalarStateView is missing view_key")
    return keys


class ScalarStateProjectionMaterializer:
    """Materialize exactly one scalar projection target inside a T5 fenced transaction."""

    def __init__(self, *, as_of: datetime | None = None, source: str = "scalar-state-lifecycle") -> None:
        self._as_of = as_of
        self._source = source

    def __call__(self, tx: Any, token: ProjectionWorkToken) -> str:
        _require_scalar_target(token)
        views = ViewRepository(tx)
        hashes = ScalarStateProjectionHashSource(tx)

        # Detect a corrupt duplicate-current surface before choosing a winner or mutating it.
        hashes.current_projection_hash(
            definition=SCALAR_STATE_PROJECTION,
            target=token.target,
            target_present=token.target_present,
        )

        expected_hash: str
        if not token.target_present:
            self._retire_slot(tx, views, token)
            expected_hash = scalar_projection_absent_hash(
                SCALAR_STATE_PROJECTION, token.target
            )
        else:
            assertions = TypedAssertionRepository(tx).materializable_assertions_for_entity(
                token.target.subject_id,
                namespace=token.target.namespace,
            )
            slot_rows = tuple(
                row for row in assertions if scalar_projection_target(row) == token.target
            )
            # This is a live orchestration boundary. ``as_of=None`` is a useful pure-fold seam in
            # the domain, but here it would activate future assertions prematurely.
            evaluation_time = self._as_of or datetime.now(timezone.utc)
            outcome = SCALAR_STATE_PROJECTION.fold(token.target, slot_rows, evaluation_time)
            if isinstance(outcome, ProjectionMaterialization):
                state = outcome.payload
                self._materialize_state(views, state, token)
                if len(_current_view_keys(tx, token)) != 1:
                    raise ProjectionLifecycleCorruptionError(
                        "scalar materialization did not leave exactly one current View"
                    )
                expected_hash = scalar_projection_present_hash(
                    SCALAR_STATE_PROJECTION,
                    token.target,
                    value=state.value,
                    valid_at=state.valid_at,
                    contributor_ids=state.contributor_ids,
                    effective_tier=state.effective_tier,
                    episode_uuids=state.episode_uuids,
                )
            elif isinstance(outcome, (ProjectionAbstention, ProjectionRetirement)):
                self._retire_slot(tx, views, token)
                expected_hash = scalar_projection_absent_hash(
                    SCALAR_STATE_PROJECTION, token.target
                )
            else:  # pragma: no cover - closed ProjectionOutcome union guard
                raise TypeError("unsupported scalar projection outcome")

        projection_hash = hashes.current_projection_hash(
            definition=SCALAR_STATE_PROJECTION,
            target=token.target,
            target_present=token.target_present,
        )
        if not isinstance(projection_hash, str) or not projection_hash.strip():
            raise ProjectionLifecycleCorruptionError("scalar materialization produced no projection hash")
        if projection_hash != expected_hash:
            raise ProjectionLifecycleCorruptionError(
                "installed ScalarStateView does not match the folded projection outcome"
            )
        return projection_hash

    @staticmethod
    def _retire_slot(tx: Any, views: ViewRepository, token: ProjectionWorkToken) -> None:
        keys = _current_view_keys(tx, token)
        if keys and not views.retire_scalar_state(view_key=keys[0]):
            raise ProjectionLifecycleCorruptionError("current ScalarStateView could not be retired")
        if _current_view_keys(tx, token):
            raise ProjectionLifecycleCorruptionError("scalar retirement left a current View installed")

    def _materialize_state(self, views: ViewRepository, state: Any, token: ProjectionWorkToken) -> None:
        audit = {
            "scalar_contributors": list(state.contributor_ids),
            "scalar_effective_tier": state.effective_tier,
            "scalar_anchor_value": str(state.anchor_value),
            "scalar_delta_total": state.delta_total,
        }
        result = views.record_scalar_state(
            subject=state.subject_display,
            subject_uuid=state.subject_uuid,
            attribute=state.attribute,
            scope=state.scope,
            value_kind=state.value_kind,
            unit=state.unit,
            value=state.value,
            display=None,
            namespace=token.target.namespace,
            valid_at=state.valid_at,
            source=self._source,
            audit=audit,
            episode_uuids=list(state.episode_uuids),
        )
        if result.get("stale_skipped"):
            raise ProjectionLifecycleCorruptionError(
                "authoritative scalar materialization was rejected as stale"
            )

        view_uuid = result.get("uuid")
        if view_uuid and state.anchor_id:
            try:
                views.draw_scalar_state_provenance_edges(
                    view_uuid=str(view_uuid),
                    anchor_id=state.anchor_id,
                    contributed_delta_ids=list(state.contributed_delta_ids),
                    superseded_anchor_ids=list(state.superseded_anchor_ids),
                )
            except Exception:  # noqa: BLE001 - matches existing advisory provenance behavior
                logger.exception(
                    "scalar_state provenance-edge draw failed for view %s (non-fatal)", view_uuid
                )
