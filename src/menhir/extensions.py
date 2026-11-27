"""Experimental registration for trusted extensions passed explicitly by a host."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import isasyncgenfunction, iscoroutinefunction
from threading import Lock
from types import MappingProxyType

from menhir.domain.projection import ProjectionDefinition, ProjectionRegistry
from menhir.domain.projection_lifecycle import ProjectionWorkToken
from menhir.infrastructure.neo4j import Neo4jRepository, Neo4jTransaction
from menhir.infrastructure.view_repository import ViewKind, ViewRepository, build_view_repository
from menhir.services.projection_reconciliation import ProjectionWorkerRegistration
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION


@dataclass(frozen=True)
class ProjectionExtension:
    """One definition, its View kind, and a trusted synchronous materializer.

    The author must scope every read and write to the work token's target and
    namespace, and apply any caller access policy before creating that work.
    Namespaces organize memory. Authorization belongs to the host.
    """

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
            callback = extension.materialize
            if any(iscoroutinefunction(fn) or isasyncgenfunction(fn) for fn in (callback, callback.__call__)):
                raise TypeError("extension materializer must be synchronous")
            read_fields = getattr(extension.view_kind, "read_fields", None)
            if not isinstance(read_fields, str) or not read_fields.strip():
                raise ValueError("extension View kind requires non-blank read_fields")
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

    def __bool__(self) -> bool:
        return bool(self._extensions)

    def view_repository(self, graph: Neo4jRepository | Neo4jTransaction) -> ViewRepository:
        return build_view_repository(graph, extensions=self)

    def projection_workers(self) -> tuple[ProjectionWorkerRegistration, ...]:
        def bind(extension: ProjectionExtension) -> ProjectionWorkerRegistration:
            def materialize(tx: Neo4jTransaction, token: ProjectionWorkToken) -> str:
                return extension.materialize(self.view_repository(tx), token)

            return ProjectionWorkerRegistration(extension.definition, materialize)

        return tuple(bind(extension) for extension in self._extensions)


_UNBOUND = object()
_process_registry: RuntimeExtensions | None | object = _UNBOUND
_registry_lock = Lock()


def resolve_graph_extensions(
    extensions: RuntimeExtensions | None = None, *, bind: bool = False,
) -> RuntimeExtensions | None:
    """Resolve the one registry used by graph surfaces for the process lifetime."""
    if extensions is not None and not isinstance(extensions, RuntimeExtensions):
        raise TypeError("graph surfaces require a validated RuntimeExtensions registry")
    requested = extensions or None
    global _process_registry
    with _registry_lock:
        if _process_registry is _UNBOUND:
            if bind:
                _process_registry = requested
            return requested
        if requested is not None and requested is not _process_registry:
            raise ValueError("the process already has a different runtime extension registry")
        assert _process_registry is None or isinstance(_process_registry, RuntimeExtensions)
        return _process_registry
