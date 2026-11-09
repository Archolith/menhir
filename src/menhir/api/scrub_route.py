"""Router-wide credential scrub for JSON REST responses (#577).

Op calls through ``_get_backend`` are already scrubbed, but several REST routes read the graph
adapter directly. ``ScrubbingAPIRoute`` is the choke point: every JSON response body is passed
through ``scrub_payload`` before it leaves, and a detector failure withholds the output.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Coroutine

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from menhir.domain.payload_scrub import WITHHELD_MESSAGE, PayloadScrubFailed, scrub_payload


def _scrub_response(response: Response) -> Response:
    media_type = getattr(response, "media_type", None) or ""
    body = getattr(response, "body", None)
    if "application/json" not in media_type or not isinstance(body, (bytes, bytearray)):
        return response
    try:
        original = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return response
    try:
        scrubbed = scrub_payload(original)
    except PayloadScrubFailed:
        return JSONResponse(status_code=500, content={"detail": WITHHELD_MESSAGE})
    if scrubbed == original:
        return response
    headers = {
        k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")
    }
    content: Any = scrubbed
    if not isinstance(response, JSONResponse):
        content = json.dumps(
            scrubbed, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    rebuilt = type(response)(
        content=content,
        status_code=response.status_code,
        headers=headers,
        background=response.background,
    )
    return rebuilt


class ScrubbingAPIRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def scrubbing_handler(request: Request) -> Response:
            response = await original_handler(request)
            return _scrub_response(response)

        return scrubbing_handler
