"""Domain-neutral projection definition and evaluation contracts.

A projection turns immutable assertions into rebuildable current-state outcomes. Core owns the
registration/evaluation mechanics here; extensions own assertion vocabulary, target derivation,
fold semantics, and the meaning of the opaque materialization payload.

This tranche deliberately does *not* own persistence, dirty generations, scheduling, stale-writer
fencing, or removed-target discovery. Those lifecycle concerns need a separate transactional seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

__all__ = [
    "ProjectionAbstention",
    "ProjectionDefinition",
    "ProjectionMaterialization",
    "ProjectionOutcome",
    "ProjectionRegistry",
    "ProjectionRetirement",
    "ProjectionTarget",
    "evaluate_projection",
]


@dataclass(frozen=True)
class ProjectionTarget:
    """Extension-derived identity of one projection slot within one definition."""

    subject_id: str
    key: tuple[str, ...] = ()
    namespace: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ValueError("ProjectionTarget.subject_id must be non-blank")
        if self.namespace is not None:
            if not isinstance(self.namespace, str):
                raise TypeError("ProjectionTarget.namespace must be a string or None")
            if not self.namespace.strip():
                raise ValueError("ProjectionTarget.namespace must be non-blank when provided")
        if not isinstance(self.key, tuple) or any(not isinstance(part, str) for part in self.key):
            raise TypeError("ProjectionTarget.key must be a tuple of strings")

    @property
    def sort_key(self) -> tuple[int, str, str, tuple[str, ...]]:
        return (
            0 if self.namespace is None else 1,
            self.namespace or "",
            self.subject_id,
            self.key,
        )


@dataclass(frozen=True)
class ProjectionMaterialization:
    """One desired current View payload plus exact contributor lineage."""

    target: ProjectionTarget
    view_kind: str
    payload: object
    contributor_ids: tuple[str, ...]
    counterevidence_ids: tuple[str, ...] = ()
    valid_at: str | None = None

    def __post_init__(self) -> None:
        _require_token("view_kind", self.view_kind)
        _validate_ids("contributor_ids", self.contributor_ids, require_nonempty=True)
        _validate_ids("counterevidence_ids", self.counterevidence_ids)


@dataclass(frozen=True)
class ProjectionAbstention:
    """Deterministic refusal to materialize a current View for a target."""

    target: ProjectionTarget
    reason: str
    contributor_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_token("reason", self.reason)
        _validate_ids("contributor_ids", self.contributor_ids)


@dataclass(frozen=True)
class ProjectionRetirement:
    """Extension decision that a previously materialized target is no longer current."""

    target: ProjectionTarget
    reason: str
    contributor_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_token("reason", self.reason)
        _validate_ids("contributor_ids", self.contributor_ids)


ProjectionOutcome = ProjectionMaterialization | ProjectionAbstention | ProjectionRetirement

AssertionIdResolver = Callable[[object], str]
AssertionTypeResolver = Callable[[object], str]
TargetResolver = Callable[[object], ProjectionTarget | None]
ProjectionFold = Callable[
    [ProjectionTarget, tuple[object, ...], datetime | None],
    ProjectionOutcome,
]


@dataclass(frozen=True)
class ProjectionDefinition:
    """Immutable registration envelope for extension-owned projection semantics.

    The callable fields are trusted implementation hooks registered by the host application, not
    per-request payload. ``input_assertion_types`` and ``output_view_kind`` are opaque vocabulary;
    core validates their shape but does not interpret their names.
    """

    definition_id: str
    version: int
    input_assertion_types: frozenset[str]
    output_view_kind: str
    assertion_id_resolver: AssertionIdResolver
    assertion_type_resolver: AssertionTypeResolver
    target_resolver: TargetResolver
    fold: ProjectionFold

    def __post_init__(self) -> None:
        _require_token("definition_id", self.definition_id)
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("ProjectionDefinition.version must be an integer >= 1")
        if not isinstance(self.input_assertion_types, frozenset) or not self.input_assertion_types:
            raise ValueError("ProjectionDefinition.input_assertion_types must be a non-empty frozenset")
        for assertion_type in self.input_assertion_types:
            _require_token("input assertion type", assertion_type)
        _require_token("output_view_kind", self.output_view_kind)
        for name in (
            "assertion_id_resolver",
            "assertion_type_resolver",
            "target_resolver",
            "fold",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"ProjectionDefinition.{name} must be callable")


class ProjectionRegistry:
    """Immutable additive registry of projection definitions.

    Registration is keyed by stable ``definition_id``. Replacing an existing semantic definition is
    intentionally absent here; version publication and stale-worker fencing belong to the lifecycle
    tranche rather than being smuggled into a simple vocabulary registry.
    """

    def __init__(self, definitions: Iterable[ProjectionDefinition] = ()) -> None:
        by_id: dict[str, ProjectionDefinition] = {}
        for definition in definitions:
            if not isinstance(definition, ProjectionDefinition):
                raise TypeError("ProjectionRegistry accepts ProjectionDefinition values")
            if definition.definition_id in by_id:
                raise ValueError(
                    f"duplicate projection definition_id {definition.definition_id!r}"
                )
            by_id[definition.definition_id] = definition
        self._by_id: Mapping[str, ProjectionDefinition] = MappingProxyType(by_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, definition_id: object) -> bool:
        return definition_id in self._by_id

    @property
    def definitions(self) -> tuple[ProjectionDefinition, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))

    def get(self, definition_id: str) -> ProjectionDefinition:
        return self._by_id[definition_id]

    def definitions_for_assertion_type(
        self,
        assertion_type: str,
    ) -> tuple[ProjectionDefinition, ...]:
        _require_token("assertion_type", assertion_type)
        return tuple(
            definition
            for definition in self.definitions
            if assertion_type in definition.input_assertion_types
        )

    def with_definition(self, definition: ProjectionDefinition) -> "ProjectionRegistry":
        if not isinstance(definition, ProjectionDefinition):
            raise TypeError("definition must be a ProjectionDefinition")
        if definition.definition_id in self._by_id:
            raise ValueError(
                f"projection definition {definition.definition_id!r} is already registered"
            )
        return ProjectionRegistry((*self.definitions, definition))



def _require_token(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")



def _validate_ids(name: str, values: tuple[str, ...], *, require_nonempty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if require_nonempty and not values:
        raise ValueError(f"{name} must not be empty")
    for value in values:
        _require_token(name, value)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")



def _validate_outcome(
    definition: ProjectionDefinition,
    target: ProjectionTarget,
    outcome: object,
    input_ids: frozenset[str],
) -> ProjectionOutcome:
    if not isinstance(
        outcome,
        (ProjectionMaterialization, ProjectionAbstention, ProjectionRetirement),
    ):
        raise TypeError("projection fold must return a ProjectionOutcome")
    if outcome.target != target:
        raise ValueError("projection fold returned an outcome for a different target")
    if isinstance(outcome, ProjectionMaterialization) and outcome.view_kind != definition.output_view_kind:
        raise ValueError(
            f"projection {definition.definition_id!r} returned view_kind {outcome.view_kind!r}; "
            f"registered output is {definition.output_view_kind!r}"
        )

    lineage = set(outcome.contributor_ids)
    if isinstance(outcome, ProjectionMaterialization):
        lineage.update(outcome.counterevidence_ids)
    unknown = lineage - input_ids
    if unknown:
        raise ValueError(
            "projection outcome cited assertion ids outside its evaluated target: "
            + ", ".join(sorted(unknown))
        )
    return outcome



def evaluate_projection(
    definition: ProjectionDefinition,
    assertions: Sequence[object],
    *,
    as_of: datetime | None = None,
) -> tuple[ProjectionOutcome, ...]:
    """Evaluate one definition deterministically over a mixed assertion collection.

    Core filters to the definition's accepted assertion types, requires unique assertion identities,
    derives extension-owned targets, orders targets and per-target inputs deterministically, invokes
    the extension fold once per target, and validates output kind/lineage. Assertions of unrelated
    types are ignored. ``None`` targets are intentionally not projected.

    This function describes desired outcomes only. It does not compare against stored Views or write,
    retire, schedule, or certify anything.
    """

    if not isinstance(definition, ProjectionDefinition):
        raise TypeError("definition must be a ProjectionDefinition")

    grouped: dict[ProjectionTarget, list[tuple[str, object]]] = {}
    seen_ids: set[str] = set()
    for assertion in assertions:
        assertion_type = definition.assertion_type_resolver(assertion)
        _require_token("resolved assertion type", assertion_type)
        if assertion_type not in definition.input_assertion_types:
            continue

        assertion_id = definition.assertion_id_resolver(assertion)
        _require_token("resolved assertion id", assertion_id)
        if assertion_id in seen_ids:
            raise ValueError(f"duplicate projection assertion id {assertion_id!r}")
        seen_ids.add(assertion_id)

        target = definition.target_resolver(assertion)
        if target is None:
            continue
        if not isinstance(target, ProjectionTarget):
            raise TypeError("projection target_resolver must return ProjectionTarget or None")
        grouped.setdefault(target, []).append((assertion_id, assertion))

    outcomes: list[ProjectionOutcome] = []
    for target in sorted(grouped, key=lambda item: item.sort_key):
        identified = sorted(grouped[target], key=lambda item: item[0])
        input_ids = frozenset(assertion_id for assertion_id, _assertion in identified)
        ordered_assertions = tuple(assertion for _assertion_id, assertion in identified)
        outcome = definition.fold(target, ordered_assertions, as_of)
        outcomes.append(_validate_outcome(definition, target, outcome, input_ids))
    return tuple(outcomes)
