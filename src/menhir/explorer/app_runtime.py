"""Settings, repository, and runtime-service accessors for explorer routes."""

from __future__ import annotations

from fastapi import Request

from menhir.config import MemorySettings
from menhir.infrastructure import Neo4jRepository


def _settings_from_env() -> MemorySettings:
    return MemorySettings.from_env()


def _repo_from_settings(settings: MemorySettings) -> Neo4jRepository:
    return Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )


def _runtime_recall_service(request: Request) -> object | None:
    """Return the canonical runtime RecallService, never a dashboard-owned copy."""
    direct = getattr(request.app.state, "recall_service", None)
    if direct is not None:
        return direct
    runtime_ctx = getattr(request.app.state, "runtime_ctx", None)
    built = getattr(runtime_ctx, "built", None)
    return getattr(built, "recall_service", None)


def _runtime_llm(request: Request) -> object | None:
    direct = getattr(request.app.state, "llm", None)
    if direct is not None:
        return direct
    runtime_ctx = getattr(request.app.state, "runtime_ctx", None)
    built = getattr(runtime_ctx, "built", None)
    return getattr(built, "llm", None)


def _require_explorer_readonly() -> None:
    """Router-level floor for every explorer route.

    The explorer mounts into the SAME FastAPI app as the API and sits behind the same bearer
    middleware, so a caller needed a valid token -- but tier was never consulted, so any
    authenticated token reached all 36 routes regardless of its rank. Reuses the API's own
    `_require_tier` so the two surfaces cannot drift apart in how they rank tiers or shape the
    403.
    """
    from menhir.api.routes_support import _require_tier

    _require_tier("readonly")


def _require_explorer_agent() -> None:
    """Floor for explorer routes that MUTATE or spend real resources.

    Applied in the route body rather than as a second router dependency, because it applies to
    six routes rather than to the router. Read the individual routes for why each one is here.
    """
    from menhir.api.routes_support import _require_tier

    _require_tier("agent")
