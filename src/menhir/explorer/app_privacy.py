"""Privacy reveal/redaction helpers for explorer responses."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from menhir.config.settings import is_loopback_host
from menhir.privacy import redact_mapping, redact_rows


def _reveal(request: Request) -> bool:
    """Resolve whether memory content should be shown (True) or redacted (False).

    Baseline comes from ``settings.privacy_redact`` (redact when True). A per-browser
    ``menhir_reveal`` cookie can override: ``"1"`` reveals but ONLY on a loopback bind
    (privacy must not be defeatable by a cookie on a remote bind); ``"0"`` forces redaction.
    """
    settings = getattr(request.app.state, "settings", None)
    base_redact = bool(getattr(settings, "privacy_redact", False))
    reveal = not base_redact
    cookie = request.cookies.get("menhir_reveal")
    loopback = is_loopback_host(getattr(settings, "api_host", "127.0.0.1"))
    if cookie == "1" and loopback:
        reveal = True
    elif cookie == "0":
        reveal = False
    return reveal


def _redact_detail(detail: dict[str, Any] | None, *, reveal: bool) -> dict[str, Any] | None:
    """Redact a node-detail mapping, including nested neighbor/episode names."""
    if reveal or not detail:
        return detail
    out = redact_mapping(detail, reveal=False)
    if isinstance(out.get("neighbors"), list):
        out["neighbors"] = redact_rows(out["neighbors"], reveal=False)
    if isinstance(out.get("episodes"), list):
        out["episodes"] = redact_rows(out["episodes"], reveal=False)
    return out


def _redact_session(session: dict[str, Any] | None, *, reveal: bool) -> dict[str, Any] | None:
    """Redact a session-detail mapping's node names."""
    if reveal or not session:
        return session
    out = dict(session)
    if isinstance(out.get("nodes"), list):
        out["nodes"] = redact_rows(out["nodes"], reveal=False)
    return out


def _redact_graph_elements(
    elements: dict[str, list[dict[str, Any]]], *, reveal: bool
) -> dict[str, list[dict[str, Any]]]:
    """Mask node display labels in a cytoscape elements payload.

    Node ``data.label`` is the memory name (redacted). Edge ``data.label`` is the
    relationship type (structural) and is preserved so the graph stays readable.
    """
    if reveal:
        return elements
    from menhir.privacy import MASK

    for node in elements.get("nodes", []):
        data = node.get("data")
        if isinstance(data, dict) and isinstance(data.get("label"), str) and data["label"].strip():
            data["label"] = MASK
    return elements
