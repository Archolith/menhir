"""HTTP-backed backend adapter."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from menhir.config import MemorySettings
from menhir.core.backend_config import resolve_backend_auth_key
from menhir.core.backend_protocol import MemoryBackend
from menhir.domain.recall import InvalidQueryPresetError

from .backend_client_ops import BackendClientOpsMixin
from .backend_shared import _push_client_warning


class BackendClient(BackendClientOpsMixin, MemoryBackend):
    """HTTP-backed backend adapter."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
        settings: MemorySettings | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()
        self._settings = settings or MemorySettings.from_env()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url, timeout=self.timeout_s
                )
        return self._client

    def _default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        key = resolve_backend_auth_key(self._settings)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        user_id = (self._settings.mcp_client_user_id or "").strip()
        if user_id:
            headers["x-menhir-user-id"] = user_id
        client_id = (self._settings.mcp_client_id or "").strip()
        if client_id:
            headers["x-menhir-client-id"] = client_id
        client_name = (self._settings.mcp_client_name or "").strip()
        if client_name:
            headers["x-menhir-client-name"] = client_name
        return headers

    async def aclose(self) -> None:
        if not self._owns_client or self._client is None:
            return
        client = self._client
        self._client = None
        await client.aclose()

    async def _request(
        self, operation: str, payload: dict[str, Any] | None = None
    ) -> Any:
        import json as _json

        client = await self._get_client()
        response = await client.post(
            f"/api/internal/backend/{operation}",
            json=payload or {},
            headers=self._default_headers(),
        )
        if response.status_code in (400, 422):
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = None
            if isinstance(detail, str) and detail.startswith("Invalid preset "):
                raise InvalidQueryPresetError(detail)
        response.raise_for_status()
        # x-yawn-bg-warnings is the deprecated spelling, still read so a new client
        # keeps working against a server that has not been upgraded yet.
        warnings_header = response.headers.get("x-menhir-bg-warnings", "") or response.headers.get(
            "x-yawn-bg-warnings", ""
        )
        if warnings_header:
            try:
                for w in _json.loads(warnings_header):
                    _push_client_warning(str(w))
            except Exception:
                pass
        if response.content:
            return response.json()
        return None
