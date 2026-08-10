"""Thin HTTP client for context recall via the menhir backend."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import httpx


@dataclass
class BackendContextBuilder:
    """Proxies build_context() calls to the HTTP backend's /internal/backend/{op} endpoint."""

    backend_url: str
    api_key: str = ""

    async def build_context(
        self,
        query: str,
        *,
        max_tokens: int = 1500,
        preset: object | None = None,
    ) -> SimpleNamespace:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "query": query,
            "max_tokens": max_tokens,
        }
        if preset is not None:
            payload["preset"] = preset.value if hasattr(preset, "value") else str(preset)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.backend_url}/api/internal/backend/build_context",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        return SimpleNamespace(context=data.get("context", ""))
