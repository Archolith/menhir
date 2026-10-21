"""Stdlib-only HTTP client for the Hook Center stale-anchor lane smoke.

Extracted verbatim from ``hook_center_stale_lane_smoke.py``; unit-testable by
monkeypatching ``urllib.request.urlopen``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from hook_center_stale_lane_smoke_constants import EVENT_HASH


# ---------------------------------------------------------------------------
# HTTP client (stdlib only) — unit-testable by monkeypatching urllib
# ---------------------------------------------------------------------------


class SmokeHTTPError(Exception):
    """Raised on a transport/protocol failure that should fail the smoke."""


def _resolve_base_url(explicit: str | None) -> str:
    base = explicit or os.environ.get("MENHIR_URL") or os.environ.get("MENHIR_TOOL_EVENTS_URL")
    return (base or "http://127.0.0.1:8099").rstrip("/")


def _headers(agent_key: str | None, readonly_key: str | None, *, write: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = (agent_key if write else (readonly_key or agent_key)) or ""
    key = key.strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _request(url: str, *, method: str, headers: dict[str, str], payload: dict | None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if hasattr(exc, "read") else ""
        status = exc.code
    except Exception as exc:  # transport failure -> caller decides
        raise SmokeHTTPError(f"{method} {url} failed: {exc}") from exc
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SmokeHTTPError(f"{method} {url} returned non-JSON body: {raw[:200]!r}") from exc
    return status, body


class HTTPClient:
    def __init__(self, base: str, agent_key: str | None, readonly_key: str | None) -> None:
        self.base = base
        self.agent_key = agent_key
        self.readonly_key = readonly_key

    def post_tool_event(self, project: str, path: str) -> dict:
        payload = {
            "event_type": "file_changed",
            "source_client": "smoke",
            "source_kind": "smoke",
            "project": project,
            "path": path,
            "operation": "edit",
            # NOTE: no file content, ever — only a synthetic provenance hash.
            "after_hash": EVENT_HASH,
            "metadata": {"smoke": True},
        }
        status, body = _request(f"{self.base}/api/tool-events", method="POST",
                                headers=_headers(self.agent_key, self.readonly_key, write=True),
                                payload=payload)
        if status != 200:
            raise SmokeHTTPError(f"POST /api/tool-events -> {status}: {body}")
        return body

    def get_dirty(self, project: str) -> dict:
        status, body = _request(f"{self.base}/api/tool-events/dirty?project={project}", method="GET",
                                headers=_headers(self.agent_key, self.readonly_key, write=False),
                                payload=None)
        if status != 200:
            raise SmokeHTTPError(f"GET /api/tool-events/dirty -> {status}: {body}")
        return body

    def get_stale(self, project: str) -> dict:
        status, body = _request(f"{self.base}/api/tool-events/stale?project={project}", method="GET",
                                headers=_headers(self.agent_key, self.readonly_key, write=False),
                                payload=None)
        if status != 200:
            raise SmokeHTTPError(f"GET /api/tool-events/stale -> {status}: {body}")
        return body

    def post_verification(self, payload: dict) -> tuple[int, dict]:
        return _request(f"{self.base}/api/tool-events/stale-verifications", method="POST",
                        headers=_headers(self.agent_key, self.readonly_key, write=True),
                        payload=payload)

    def get_verifications(self, memory_uuid: str) -> dict:
        status, body = _request(
            f"{self.base}/api/tool-events/stale-verifications?memory_uuid={memory_uuid}",
            method="GET",
            headers=_headers(self.agent_key, self.readonly_key, write=False),
            payload=None,
        )
        if status != 200:
            raise SmokeHTTPError(f"GET /api/tool-events/stale-verifications -> {status}: {body}")
        return body

    def wait_ready(self, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                status, _ = _request(f"{self.base}/api/tool-events/dirty", method="GET",
                                     headers=_headers(self.agent_key, self.readonly_key, write=False),
                                     payload=None)
                if status == 200:
                    return True
            except SmokeHTTPError:
                pass
            time.sleep(0.5)
        return False
