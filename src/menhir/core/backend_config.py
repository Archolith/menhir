"""Configuration helpers shared by backend transports and their adapters."""

from __future__ import annotations

from menhir.config import MemorySettings


def resolve_backend_auth_key(settings: MemorySettings | None = None) -> str:
    """Resolve the bearer key used for internal backend requests.

    The agent key takes precedence over the legacy API key. Whitespace-only
    values are treated as absent.
    """

    settings = settings or MemorySettings.from_env()
    agent = (settings.agent_key or "").strip()
    api = (settings.api_key or "").strip()
    return agent or api
