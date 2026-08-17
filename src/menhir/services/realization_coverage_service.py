"""Read-only orchestration for Realization Coverage v1."""

from __future__ import annotations

from typing import Protocol, Sequence

from menhir.domain.projection import ProjectionDefinition, ProjectionOutcome, ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessAssessment,
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
)
from menhir.domain.realization_coverage import (
    RealizationCoverageReport,
    build_realization_coverage_report,
)


class RealizationLifecycleSource(Protocol):
    """Read side required from the durable projection lifecycle."""

    def targets_for_definition(self, definition_id: str) -> Sequence[ProjectionWorkToken]: ...

    def assess_freshness(
        self,
        *,
        definition_id: str,
        target: ProjectionTarget,
        current_projection_hash: str | None,
    ) -> ProjectionFreshnessAssessment: ...


class ProjectionStateHashSource(Protocol):
    """Projection-adapter hook for hashing the exact currently installed target state.

    The adapter owns the canonical hash semantics for both present and absent/retired states. T7
    never interprets View payloads or manufactures a generic hash contract.
    """

    def current_projection_hash(
        self,
        *,
        definition: ProjectionDefinition,
        target: ProjectionTarget,
        target_present: bool,
    ) -> str | None: ...


class RealizationCoverageService:
    """Join complete lifecycle membership, adapter state hashes, and T5 freshness proof."""

    def __init__(
        self,
        lifecycle: RealizationLifecycleSource,
        state_hashes: ProjectionStateHashSource,
    ) -> None:
        self._lifecycle = lifecycle
        self._state_hashes = state_hashes

    def audit_definition(
        self,
        definition: ProjectionDefinition,
        outcomes: Sequence[ProjectionOutcome],
    ) -> RealizationCoverageReport:
        """Audit one complete desired-outcome snapshot without mutating lifecycle or projections.

        ``outcomes`` must be the complete T4 outcome set for the definition at the caller's chosen
        evaluation time. The lifecycle source supplies every durable target ever reconciled for this
        definition, including targets now marked absent. Their union is assessed so neither a newly
        desired target nor a removed historical target can disappear from coverage.
        """

        if not isinstance(definition, ProjectionDefinition):
            raise TypeError("definition must be a ProjectionDefinition")

        desired_targets = [outcome.target for outcome in outcomes]
        if len(set(desired_targets)) != len(desired_targets):
            # Keep the service fail-closed before any adapter I/O. The pure builder enforces the same
            # invariant for callers that use it directly.
            raise ValueError("duplicate desired projection target")

        lifecycle_tokens = tuple(
            self._lifecycle.targets_for_definition(definition.definition_id)
        )
        token_by_target: dict[ProjectionTarget, ProjectionWorkToken] = {}
        for token in lifecycle_tokens:
            if not isinstance(token, ProjectionWorkToken):
                raise TypeError("lifecycle target snapshot must contain ProjectionWorkToken values")
            if token.definition_id != definition.definition_id:
                raise ProjectionLifecycleCorruptionError(
                    "lifecycle target snapshot contains a different projection definition"
                )
            if token.definition_version != definition.version:
                raise ProjectionLifecycleCorruptionError(
                    "lifecycle target snapshot is not on the requested definition version"
                )
            if token.target in token_by_target:
                raise ProjectionLifecycleCorruptionError(
                    "lifecycle target snapshot contains duplicate target identities"
                )
            token_by_target[token.target] = token

        all_targets = set(desired_targets) | set(token_by_target)
        assessments: list[ProjectionFreshnessAssessment] = []
        for target in sorted(all_targets, key=lambda item: item.sort_key):
            token = token_by_target.get(target)
            # A desired target not yet registered in lifecycle is expected-present for hash purposes;
            # assess_freshness will still report target_not_registered. Historical targets use their
            # durable membership bit so adapters can hash canonical absent/retired state correctly.
            target_present = True if token is None else token.target_present
            current_hash = self._state_hashes.current_projection_hash(
                definition=definition,
                target=target,
                target_present=target_present,
            )
            assessments.append(
                self._lifecycle.assess_freshness(
                    definition_id=definition.definition_id,
                    target=target,
                    current_projection_hash=current_hash,
                )
            )

        return build_realization_coverage_report(
            definition=definition,
            outcomes=outcomes,
            freshness=assessments,
        )
