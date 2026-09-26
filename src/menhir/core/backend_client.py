"""HTTP-backed backend adapter."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import httpx

from menhir.config import MemorySettings
from menhir.core.backend_config import resolve_backend_auth_key
from menhir.core.backend_protocol import MemoryBackend
from menhir.domain.recall import InvalidQueryPresetError
from menhir.domain.session import MemorySession

from .backend_client_ops import BackendClientOpsMixin
from .backend_shared import _push_client_warning


@lru_cache(maxsize=1)
def client_user_agent() -> str:
    """The agent this client identifies itself as.

    Not cosmetic, and not for analytics. A Cloudflare zone with Browser Integrity Check enabled
    refuses some HTTP client signatures at the edge with a 403 (error 1010) -- the request never
    reaches the origin, so the failure is indistinguishable from the server being down. Measured
    against a BIC-enabled zone: `Python-urllib/3.12` is refused and **so is a request with no
    User-Agent header at all**; `python-httpx/...` (httpx's default, which this client sent
    before) currently passes.

    So this is insurance, not a bug fix. The default works today, but the blocked-signature list
    belongs to Cloudflare and can change without notice, and the empty case is already blocked --
    which is the one a future refactor could reintroduce for free. Naming ourselves costs nothing
    and removes the whole class of silent edge rejection.

    Imported inside the function, matching `api/mcp_remote.py`: `menhir/__init__` resolves the
    version through package metadata, and a module-level import here would pull that into every
    importer of the client.
    """
    from menhir import __version__

    return f"menhir/{__version__}"


class BackendClient(BackendClientOpsMixin, MemoryBackend):
    """HTTP-backed backend adapter."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
        settings: MemorySettings | None = None,
        caller_session: MemorySession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()
        self._settings = settings or MemorySettings.from_env()
        self._caller_session = caller_session

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
        # Unconditional: every other header here is conditional on configuration, and an agent
        # that appears only when some setting happens to be set is the case that gets refused.
        headers: dict[str, str] = {"User-Agent": client_user_agent()}
        if self._caller_session is not None and self._caller_session.session_id:
            headers["x-menhir-session-id"] = self._caller_session.session_id
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
        if response.status_code in (400, 403, 422):
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = None
            if isinstance(detail, str) and detail.startswith("Invalid preset "):
                raise InvalidQueryPresetError(detail)
            # #132. backend_invoke maps a backend ValueError to 400 and PermissionError to
            # 403 with the message as `detail`. Re-raise them as the same exception types the
            # in-process backend would have raised, so a tool's `except ValueError` sees the
            # refusal in HTTP mode too instead of an opaque HTTPStatusError.
            if isinstance(detail, str) and response.status_code == 400:
                raise ValueError(detail)
            if isinstance(detail, str) and response.status_code == 403:
                raise PermissionError(detail)
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
