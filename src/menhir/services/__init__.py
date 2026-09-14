"""Service abstractions for memory ingestion and policy phases.

Package attributes resolve lazily (PEP 562) for the same reason as ``menhir.infrastructure``:
an eager import here reached ``graphiti_core``, whose import-time ``load_dotenv()`` reads the
current directory's ``.env`` before the CLI loads the checkout's own.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY: dict[str, str] = {
    "CandidateService": ".candidate_service",
    "ContextBuilderService": ".context_builder",
    "IngestService": ".ingest_service",
    "LifecycleService": ".lifecycle_service",
    "MaintenanceScheduler": ".maintenance_scheduler",
    "ProjectionCoverageService": ".projection_coverage_service",
    "RealizationCoverageService": ".realization_coverage_service",
    "RecallService": ".recall_service",
    "SchedulerLeaseStore": ".scheduler_lease",
    "ScoringService": ".scoring_service",
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
