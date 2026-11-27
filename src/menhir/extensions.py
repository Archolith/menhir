"""Experimental registration for trusted extensions passed explicitly by a host."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType

from menhir.domain.projection import ProjectionDefinition, ProjectionRegistry
from menhir.domain.projection_lifecycle import ProjectionWorkToken
from menhir.infrastructure.neo4j import Neo4jRepository, Neo4jTransaction
from menhir.infrastructure.view_repository import ViewKind, ViewRepository
from menhir.services.projection_reconciliation import ProjectionWorkerRegistration
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION


@dataclass(frozen=True)
class ProjectionExtension:
    """One definition, its View kind, and a trusted transaction-scoped materializer."""

    definition: ProjectionDefinition
    view_kind: ViewKind
    materialize: Callable[[ViewRepository, ProjectionWorkToken], str]


class RuntimeExtensions:
    """Validate the complete batch before constructing the runtime."""

    def __init__(self, extensions: tuple[ProjectionExtension, ...]) -> None:
        kinds = dict(ViewRepository.KINDS)
        definitions = [SCALAR_STATE_PROJECTION]
        for extension in extensions:
            if not isinstance(extension, ProjectionExtension):
                raise TypeError("extensions must contain ProjectionExtension values")
            if not isinstance(extension.definition, ProjectionDefinition):
                raise TypeError("extension definition must be a ProjectionDefinition")
            if not isinstance(extension.view_kind, ViewKind) or not callable(extension.materialize):
                raise TypeError("extension requires a ViewKind and a callable materializer")
            name = getattr(extension.view_kind, "name", None)
            if name != extension.definition.output_view_kind:
                raise ValueError("extension View kind must match its projection output kind")
            if name in kinds:
                raise ValueError(f"View kind {name!r} is already registered")
            kinds[name] = extension.view_kind
            definitions.append(extension.definition)
        ProjectionRegistry(definitions)
        self.view_kinds = MappingProxyType(kinds)
        self._extensions = tuple(extensions)

    def view_repository(self, graph: Neo4jRepository | Neo4jTransaction) -> ViewRepository:
        return ViewRepository(graph, kinds=self.view_kinds)

    def projection_workers(self) -> tuple[ProjectionWorkerRegistration, ...]:
        def bind(extension: ProjectionExtension) -> ProjectionWorkerRegistration:
            def materialize(tx: Neo4jTransaction, token: ProjectionWorkToken) -> str:
                return extension.materialize(self.view_repository(tx), token)

            return ProjectionWorkerRegistration(extension.definition, materialize)

        return tuple(bind(extension) for extension in self._extensions)
