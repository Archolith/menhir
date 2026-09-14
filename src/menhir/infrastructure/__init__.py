"""Infrastructure adapters for storage and LLM services.

Package attributes resolve lazily (PEP 562). Importing any ``menhir.infrastructure.*``
submodule used to execute this file's eager imports, which pulled in ``graphiti_core`` --
and ``graphiti_core.helpers`` calls ``load_dotenv()`` at import, reading whatever ``.env``
sits in the *current directory*. That leaked a bystander ``.env`` into the process before
the CLI had a chance to load the checkout's own. Resolving on first attribute access keeps
``from menhir.infrastructure import X`` working while leaving the import order to callers.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY: dict[str, str] = {
    "CircuitBreaker": ".circuit_breaker",
    "CircuitOpenError": ".circuit_breaker",
    "GraphitiClient": ".graphiti_client",
    "LLMAdapter": ".llm",
    "MemoryGraphAdapter": ".memory_graph_adapter",
    "PhaseOneSchemaResult": ".memory_graph_adapter",
    "PolicyStampResult": ".memory_graph_adapter",
    "Neo4jRepository": ".neo4j",
    "Neo4jTransaction": ".neo4j",
    "ProjectionCoverageRepository": ".projection_coverage_repository",
    "PROJECTION_LIFECYCLE_SCHEMA_QUERIES": ".projection_lifecycle_repository",
    "ProjectionLifecycleRepository": ".projection_lifecycle_repository",
    "projection_lifecycle_schema_queries": ".projection_lifecycle_repository",
    "ProviderConfig": ".providers",
    "ProviderKind": ".providers",
    "build_chat_backend": ".providers",
    "RealizationLifecycleRepository": ".realization_coverage_repository",
    "ScalarStateProjectionHashSource": ".realization_coverage_repository",
    "EDGE_LABELS": ".schema",
    "MEMORY_NODE_LABELS": ".schema",
    "PHASE_ONE_REQUIRED_CONSTRAINTS": ".schema",
    "PHASE_ONE_REQUIRED_INDEXES": ".schema",
    "get_phase1_bootstrap_queries": ".schema",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(target, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
